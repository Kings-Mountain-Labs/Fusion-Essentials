"""Unit tests for ``arrange.py`` — pack shapes within a sketch-profile boundary (Arrange feature).

The Arrange feature nests component occurrences inside a 2D envelope (a sketch profile / planar
face). Pinned here without a live Fusion: solver-type resolution (true_shape/rectangular), boundary
resolution (a named sketch -> its profile), shape resolution (occurrence names -> occurrences and
into ArrangeComponents.add), spacing -> cm, and the orchestration (createInput -> setProfileOrFace
Envelope -> add each component -> add feature). The actual solve is a live side-effect.
"""

from types import SimpleNamespace

import adsk.core
import adsk.fusion
import pytest

from conftest import (
    MakeComp, assert_no_active_design, install, load_tool, make_design, make_occurrence,
    make_sketch, payload as _payload,
)

ar = load_tool("model_arrange")

# Measured solver enum (seeded from live_api_facts) - fakes and assertions speak these.
_TRUE = adsk.fusion.ArrangeSolverTypes.Arrange2DTrueShapeSolverType
_RECT = adsk.fusion.ArrangeSolverTypes.Arrange2DRectangularSolverType


# ── fakes ───────────────────────────────────────────────────────────────────

def _sketch(name, tag="profile", profile_count=1, compute_deferred=False):
    """A boundary sketch whose profiles say WHICH sketch they came from."""
    return make_sketch(name=name, is_compute_deferred=compute_deferred,
                       profiles=[(tag, i) for i in range(profile_count)])


def _vec(x=0.0, y=0.0, z=0.0):
    return SimpleNamespace(x=x, y=y, z=z)


def _occ(path):
    return make_occurrence(path=path, component=SimpleNamespace(name=path.split(":")[0]),
                           transform2=SimpleNamespace(translation=_vec()))


class FakeArrangeComponents:
    def __init__(self):
        self.added = []
    def add(self, occ_or_face):
        self.added.append(occ_or_face)
        return ("ac", occ_or_face)


class FakeEnvelope:
    def __init__(self, profiles):
        self.profiles = list(profiles)
        self.objectSpacing = None


class FakeArrangeInput:
    def __init__(self, solver):
        self.solver = solver
        self.envelope = None          # the FakeEnvelope returned by setProfileOrFaceEnvelope
        self.arrangeComponents = FakeArrangeComponents()
    def setProfileOrFaceEnvelope(self, profiles_or_faces):
        self.envelope = FakeEnvelope(profiles_or_faces)
        return self.envelope


class FakeArrangeFeatures:
    """add() imitates the measured solver behavior: the named occurrences stay where they are and
    envelope COPIES land as new occurrences - the effect read the handler verifies against."""
    def __init__(self):
        self.last_input = None
        self.added = False
        self.design = None
    def createInput(self, solver):
        self.last_input = FakeArrangeInput(solver)
        return self.last_input
    def add(self, inp):
        self.added = True
        if self.design is not None:
            for shape in inp.arrangeComponents.added:
                nm = getattr(shape, "name", "X")
                self.design.rootComponent.allOccurrences.append(
                    _occ(f"Arrange1:1+Envelope1(Qty: 1):1+{nm}"))
        return type("F", (), {"name": "Arrange1", "deleteMe": lambda self: True})()


def _component(name, sketches, occurrences=(), af=None):
    """A component holding `sketches`, and the arrangeFeatures collection when it is the root."""
    comp = MakeComp(name=name, sketches=list(sketches), occurrences=list(occurrences))
    if af is not None:
        comp.features = SimpleNamespace(arrangeFeatures=af)
    return comp


def _wire(design, af):
    """Point the tool at `design` (both seams) and model the ValueInput factories it calls."""
    af.design = design
    install(ar, design)
    adsk.core.ValueInput.createByReal = staticmethod(lambda v: ("real", v))
    adsk.core.ValueInput.createByString = staticmethod(lambda s: ("str", s))
    return design, af


def _install(sketches=(), occ_names=()):
    af = FakeArrangeFeatures()
    root = _component("Root", sketches, [_occ(n) for n in occ_names], af)
    return _wire(make_design(comp=root), af)


# ── solver type ──────────────────────────────────────────────────────────────

class TestSolverType:
    def test_true_shape_default(self):
        _, af = _install([_sketch("Boundary")], ["A:1"])
        _payload(ar.handler(boundary_sketch="Boundary", shapes="A:1"))
        assert af.last_input.solver == _TRUE

    def test_rectangular(self):
        _, af = _install([_sketch("Boundary")], ["A:1"])
        _payload(ar.handler(boundary_sketch="Boundary", shapes="A:1", solver="rectangular"))
        assert af.last_input.solver == _RECT

    def test_unknown_solver_errors(self):
        _install([_sketch("Boundary")], ["A:1"])
        res = ar.handler(boundary_sketch="Boundary", shapes="A:1", solver="hexagonal")
        assert res["isError"] is True and "solver" in res["message"].lower()

    def test_rect_alias_resolves_to_rectangular(self):
        _, af = _install([_sketch("B")], ["A:1"])
        out = _payload(ar.handler(boundary_sketch="B", shapes="A:1", solver="rect"))
        assert af.last_input.solver == _RECT
        # payload's solver field is normalized off the resolved solver class name
        assert out["solver"] == "rectangular"

    def test_true_alias_normalizes_in_payload(self):
        _, af = _install([_sketch("B")], ["A:1"])
        out = _payload(ar.handler(boundary_sketch="B", shapes="A:1", solver="trueshape"))
        assert af.last_input.solver == _TRUE
        assert out["solver"] == "true_shape"

    def test_solver_case_insensitive(self):
        _, af = _install([_sketch("B")], ["A:1"])
        _payload(ar.handler(boundary_sketch="B", shapes="A:1", solver="RECTANGULAR"))
        assert af.last_input.solver == _RECT


# ── boundary resolution ──────────────────────────────────────────────────────

class TestBoundary:
    def test_named_sketch_profile_used_as_envelope(self):
        _, af = _install([_sketch("Boundary")], ["A:1"])
        _payload(ar.handler(boundary_sketch="Boundary", shapes="A:1"))
        assert af.last_input.envelope.profiles == [("profile", 0)]

    def test_a_deferred_boundary_sketch_is_refused(self):
        # profiles.item(0) is a blind index off the sketch's own collection, so a deferred sketch
        # would hand the envelope whichever region was first before the deferral.
        _install([_sketch("Boundary", profile_count=2, compute_deferred=True)], ["A:1"])
        res = ar.handler(boundary_sketch="Boundary", shapes="A:1")
        assert res["isError"] is True
        assert "isComputeDeferred=true" in res["message"] and "'Boundary'" in res["message"]
        assert "'boundary_sketch'" in res["message"]

    def test_missing_boundary_errors(self):
        _install([_sketch("Other")], ["A:1"])
        res = ar.handler(boundary_sketch="Nope", shapes="A:1")
        assert res["isError"] is True and "Nope" in res["message"]

    def test_a_shared_boundary_sketch_name_is_refused_with_its_owners(self, monkeypatch):
        # Two components can each hold a "Boundary". The refusal names them and no arrange feature
        # is built; calling it "No sketch named 'Boundary'" says the opposite of what the walk read.
        _design, af = _install([_sketch("Boundary")], ["A:1"])
        refusal = "2 sketches are named 'Boundary' ('Boundary' in Root, 'Boundary' in Frame)"
        monkeypatch.setattr(ar._common, "find_sketch",
                            lambda design, name, remedy=None: (None, refusal))
        res = ar.handler(boundary_sketch="Boundary", shapes="A:1")
        assert res["isError"] is True
        assert res["message"] == refusal and "No sketch named" not in res["message"]
        assert af.last_input is None and af.added is False     # nothing was created

    def test_boundary_with_no_profile_errors(self):
        _install([_sketch("Empty", profile_count=0)], ["A:1"])
        res = ar.handler(boundary_sketch="Empty", shapes="A:1")
        assert res["isError"] is True and "profile" in res["message"].lower()


# ── the 'boundary_component' SCOPE ───────────────────────────────────────────
# Fusion numbers sketches per component from 1, so two components each holding a "Boundary" is the
# norm. The design-wide walk REFUSES that name and points at this input: a rename is no remedy for
# a component that arrived inside a referenced document. The scope is spelled for the BOUNDARY
# because that is the only sketch this tool resolves by name.

@pytest.fixture
def multi():
    """Root plus further named components, each with its OWN sketches. allComponents lives on the
    DESIGN, which is the collection the shared by-name walk asks."""
    def _do(pairs, occ_names=("A:1",), extra_components=()):
        af = FakeArrangeFeatures()
        occs = [_occ(n) for n in occ_names]
        comps = [_component(pairs[0][0], pairs[0][1], occs, af)]
        comps += [_component(n, s) for n, s in pairs[1:]]
        design = make_design(comp=comps[0], all_components=comps + list(extra_components))
        return _wire(design, af)
    return _do


class TestBoundaryComponentScope:
    def _shared(self, multi):
        """ONE name across TWO components, with different profile counts (Alpha 2, Beta 1) so the
        envelope that answered is readable rather than assumed from a shared name."""
        alpha_sk = _sketch("Boundary", tag="alpha", profile_count=2)
        beta_sk = _sketch("Boundary", tag="beta", profile_count=1)
        design, af = multi([("Alpha", [alpha_sk]), ("Beta", [beta_sk])])
        return design, af, alpha_sk, beta_sk

    def test_the_unscoped_shared_name_refuses_and_names_the_scope_input(self, multi):
        _design, af, _a, _b = self._shared(multi)
        res = ar.handler(boundary_sketch="Boundary", shapes="A:1")
        assert res["isError"] is True
        assert "2 sketches are named 'Boundary'" in res["message"]
        assert "'boundary_component'" in res["message"] and "Rename one" not in res["message"]
        assert af.last_input is None and af.added is False

    def test_the_scope_uses_THAT_components_boundary_profile(self, multi):
        _design, af, alpha_sk, beta_sk = self._shared(multi)
        _payload(ar.handler(boundary_sketch="Boundary", boundary_component="Beta", shapes="A:1"))
        assert af.last_input.envelope.profiles == [("beta", 0)]

    def test_the_sibling_component_is_reachable_by_the_same_call(self, multi):
        _design, af, alpha_sk, _b = self._shared(multi)
        _payload(ar.handler(boundary_sketch="Boundary", boundary_component="Alpha", shapes="A:1"))
        assert af.last_input.envelope.profiles == [("alpha", 0)]

    def test_an_unknown_component_is_refused_before_the_arrange(self, multi):
        _design, af, _a, _b = self._shared(multi)
        res = ar.handler(boundary_sketch="Boundary", boundary_component="Gamma", shapes="A:1")
        assert res["isError"] is True and "No component named 'Gamma'" in res["message"]
        assert af.last_input is None and af.added is False

    def test_a_wrong_component_is_refused_even_when_the_name_is_UNIQUE(self, multi):
        # The scope is VALIDATED: a dropped one nests against Alpha's envelope on a call that
        # named Beta, and nothing tells the caller which boundary was used.
        _design, af = multi([("Alpha", [_sketch("OnlyOne")]), ("Beta", [])])
        res = ar.handler(boundary_sketch="OnlyOne", boundary_component="Beta", shapes="A:1")
        assert res["isError"] is True and "'Beta'" in res["message"]
        assert af.last_input is None and af.added is False

    def test_the_scoped_MISS_names_boundary_component_never_the_bare_component(self, multi):
        # This tool declares a STRICT schema and carries NO 'component' input, so a refusal naming
        # 'component' hands the caller a retry its own schema rejects.
        _design, af = multi([("Alpha", [_sketch("Boundary")]), ("Beta", [_sketch("Other")])])
        res = ar.handler(boundary_sketch="Boundary", boundary_component="Beta", shapes="A:1")
        assert res["isError"] is True
        assert "holds no sketch named 'Boundary'" in res["message"]
        assert "'boundary_component'" in res["message"]
        assert "'component'" not in res["message"]
        assert af.last_input is None and af.added is False

    def test_an_AMBIGUOUS_boundary_component_names_boundary_component(self, multi):
        # The occurrence-path remedy names the input this tool actually accepts.
        a = _component("Frame", [_sketch("Boundary")])
        b = _component("Frame", [_sketch("Boundary")])
        design, af = multi([("Root", [])], extra_components=(a, b))
        design.rootComponent.allOccurrences += [
            make_occurrence(path="P2-Gimbal:1+Frame:1", component=a),
            make_occurrence(path="P3-Gimbal:1+Frame:1", component=b)]
        res = ar.handler(boundary_sketch="Boundary", boundary_component="Frame", shapes="A:1")
        assert res["isError"] is True
        assert "2 components match 'Frame'" in res["message"]
        assert "'boundary_component' also takes an occurrence fullPathName" in res["message"]
        assert "'component'" not in res["message"]
        assert af.last_input is None and af.added is False


# ── shapes ───────────────────────────────────────────────────────────────────

class TestShapes:
    def test_each_shape_added_as_component(self):
        _, af = _install([_sketch("B")], ["A:1", "B:1", "C:1"])
        out = _payload(ar.handler(boundary_sketch="B", shapes="A:1, B:1, C:1"))
        assert len(af.last_input.arrangeComponents.added) == 3
        assert out["arranged_count"] == 3

    def test_missing_shape_reported(self):
        _install([_sketch("B")], ["A:1"])
        res = ar.handler(boundary_sketch="B", shapes="A:1, Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_no_shapes_errors(self):
        _install([_sketch("B")], ["A:1"])
        res = ar.handler(boundary_sketch="B", shapes="")
        assert res["isError"] is True and "shapes" in res["message"].lower()

    def test_feature_created(self):
        _, af = _install([_sketch("B")], ["A:1"])
        out = _payload(ar.handler(boundary_sketch="B", shapes="A:1"))
        assert af.added is True
        assert out["arranged"] is True


# ── spacing ──────────────────────────────────────────────────────────────────

class TestSpacing:
    def test_spacing_scaled_to_cm(self):
        _, af = _install([_sketch("B")], ["A:1"])
        _payload(ar.handler(boundary_sketch="B", shapes="A:1", spacing=5, units="mm"))
        # objectSpacing set on the ENVELOPE input as a cm ValueInput (5mm -> 0.5cm)
        assert af.last_input.envelope.objectSpacing == ("real", 0.5)

    def test_spacing_inches_scaled_to_cm(self):
        _, af = _install([_sketch("B")], ["A:1"])
        out = _payload(ar.handler(boundary_sketch="B", shapes="A:1", spacing=2, units="in"))
        # 2in -> 5.08cm
        assert af.last_input.envelope.objectSpacing == ("real", 5.08)
        assert out["spacing"] == 2.0
        assert out["units"] == "in"

    def test_zero_spacing_not_set_and_reported_zero(self):
        _, af = _install([_sketch("B")], ["A:1"])
        out = _payload(ar.handler(boundary_sketch="B", shapes="A:1", spacing=0))
        # falsy spacing skips the setattr branch -> envelope keeps its default
        assert af.last_input.envelope.objectSpacing is None
        assert out["spacing"] == 0.0

    def test_unknown_units_errors(self):
        _install([_sketch("B")], ["A:1"])
        res = ar.handler(boundary_sketch="B", shapes="A:1", units="furlong")
        assert res["isError"] is True and "units" in res["message"].lower()

    def test_spacing_setter_raise_surfaces_as_error(self):
        # An objectSpacing setter failure must propagate out of the handler (as Arrange failed: ...)
        # - swallowing it would report the spacing as applied when it wasn't.
        class _ReadOnlyEnvelope(FakeEnvelope):
            @property
            def objectSpacing(self):
                return None
            @objectSpacing.setter
            def objectSpacing(self, v):
                raise AttributeError("objectSpacing is read-only on this API version")

        class _RaisingInput(FakeArrangeInput):
            def setProfileOrFaceEnvelope(self, profiles_or_faces):
                self.envelope = _ReadOnlyEnvelope(profiles_or_faces)
                return self.envelope

        _, af = _install([_sketch("B")], ["A:1"])
        af.createInput = lambda solver: _RaisingInput(solver)
        res = ar.handler(boundary_sketch="B", shapes="A:1", spacing=5)
        assert res["isError"] is True
        assert "Arrange failed: objectSpacing is read-only" in res["message"]


# ── honesty: failed/absent mutation must surface as isError, never a false ok ─
# (the paths test_model_mirror.py / test_model_shell.py treat as mandatory)

class TestHonesty:
    def test_add_returning_none_is_error(self):
        _, af = _install([_sketch("B")], ["A:1"])
        af.add = lambda inp: None
        res = ar.handler(boundary_sketch="B", shapes="A:1")
        assert res["isError"] is True and "no feature" in res["message"].lower()

    def test_add_raising_surfaces_as_error(self):
        _, af = _install([_sketch("B")], ["A:1"])

        def _boom(inp):
            raise RuntimeError("solver crashed")

        af.add = _boom
        res = ar.handler(boundary_sketch="B", shapes="A:1")
        assert res["isError"] is True
        assert "Arrange failed" in res["message"] and "solver crashed" in res["message"]

    def test_no_active_design(self):
        _install([_sketch("B")], ["A:1"])
        assert_no_active_design(ar, ar.handler, boundary_sketch="B", shapes="A:1")

    def test_copies_without_movement_are_disclosed(self):
        # The solver leaves the inputs unmoved and mints envelope copies - the payload must say so
        # instead of implying the named occurrences were packed.
        _install([_sketch("B")], ["A:1"])
        out = _payload(ar.handler(boundary_sketch="B", shapes="A:1"))
        assert out["moved"] == []
        assert out["new_occurrence_count"] == 1
        assert "Arrange1:1+Envelope1(Qty: 1):1+A:1" in out["new_occurrences"]
        assert "COPIES" in out["note"] and "did NOT move" in out["note"]

    def test_nothing_happened_is_an_error_and_rolls_back(self):
        # No input moved AND no occurrence appeared = the arrange did nothing; success would be a lie.
        _, af = _install([_sketch("B")], ["A:1"])
        deleted = []
        def _inert_add(inp):
            af.added = True
            return type("F", (), {"name": "Arrange1",
                                  "deleteMe": lambda self: deleted.append(True) or True})()
        af.add = _inert_add
        res = ar.handler(boundary_sketch="B", shapes="A:1")
        assert res["isError"] is True and "NOTHING happened" in res["message"]
        assert deleted == [True]

    def test_a_moved_input_is_named_in_moved(self):
        # A solver that really repositions the input reports it in 'moved'.
        design, af = _install([_sketch("B")], ["A:1"])
        real_add = af.add
        def _moving_add(inp):
            design.rootComponent.allOccurrences[0].transform2.translation = _vec(5.0, 0.0, 0.0)
            return real_add(inp)
        af.add = _moving_add
        out = _payload(ar.handler(boundary_sketch="B", shapes="A:1"))
        assert out["moved"] == ["A:1"]
