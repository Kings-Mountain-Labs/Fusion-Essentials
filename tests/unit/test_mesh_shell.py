"""Unit tests for ``mesh_shell.py`` - the MeshShell feature over a MeshBody.

No live Fusion. The fakes model the slice of the API the tool touches: MeshShellFeatures
(createInput(mesh) -> input -> add(input) -> feature or None), a MeshShellFeatureInput whose
thickness accepts ONLY a ValueInput (its declared type), and a MeshBody the shell re-triangulates
and hollows IN PLACE (same body, same name).

Pinned (the DoD):
  - thickness crosses the wire as a ValueInput carrying internal cm, scaled from the call's units.
  - a zero / negative / non-numeric thickness is refused before any mutation.
  - the effect is judged on the body: triangle and vertex counts plus the enclosed volume, and a
    shell that moved none of them is an ERROR.
  - a DIRECT design returns no feature while the hollow LANDS - reported as success off the body
    census; a PARAMETRIC no-feature return stays an honest error.
  - the thickness that landed is read back off the feature's ModelParameter, never echoed.
  - a mesh nothing can be read off afterwards is reported UNVERIFIED, not as success.
"""

import types

import adsk.fusion
import pytest

from conftest import (BRepBody, FakeFeatures, FakeValueInput as _ValueInput, MakeComp, MakeDesign,
                      MeshBody, install, load_tool, payload, error_message)

ms = load_tool("mesh_shell")


# ── fakes ────────────────────────────────────────────────────────────────────────────────────────

class _MeshComp(MakeComp):
    """A component whose meshBodies read RAISES once `dead` is set - the census host that stops
    answering mid-flight."""
    def __init__(self, *args, **kwargs):
        self.dead = False
        super().__init__(*args, **kwargs)

    @property
    def meshBodies(self):
        if self.dead:
            raise RuntimeError("3 : object is no longer valid")
        return self._mesh_bodies

    @meshBodies.setter
    def meshBodies(self, value):
        self._mesh_bodies = value


class _ShellInput:
    """MeshShellFeatureInput. thickness is declared core.ValueInput, so a raw number is refused
    here exactly as the live typed property refuses it."""
    def __init__(self, mesh):
        self.mesh = mesh
        self.targetBaseFeature = None
        self._thickness = None

    @property
    def thickness(self):
        return self._thickness

    @thickness.setter
    def thickness(self, value):
        if not isinstance(value, _ValueInput):
            raise TypeError("thickness expects a ValueInput, got " + type(value).__name__)
        self._thickness = value


class _ShellFeatures:
    """comp.features.meshShellFeatures. `on_add` is the in-place hollow the shell performs;
    `none_feature` is the measured DIRECT-mode return (None WITH the effect landed)."""
    def __init__(self, on_add=None, raise_on_add=False, none_feature=False,
                 create_input_returns_none=False, feature_thickness=None):
        self._on_add = on_add
        self.raise_on_add = raise_on_add
        self.none_feature = none_feature
        self._create_input_returns_none = create_input_returns_none
        self._feature_thickness = feature_thickness
        self.last_input = None
        self.create_arg = None
        self.add_called = False

    def createInput(self, mesh):
        self.create_arg = mesh
        if self._create_input_returns_none:
            return None
        self.last_input = _ShellInput(mesh)
        return self.last_input

    def add(self, inp):
        self.add_called = True
        if self.raise_on_add:
            raise RuntimeError("shell failed")
        if self._on_add is not None:
            self._on_add()
        if self.none_feature:
            return None
        landed = (self._feature_thickness if self._feature_thickness is not None
                  else getattr(getattr(self.last_input, "thickness", None), "realValue", None))
        feat = types.SimpleNamespace(name="MeshShell1")
        if landed is not None:
            feat.thickness = types.SimpleNamespace(value=float(landed))
        return feat


class _Features(FakeFeatures):
    """comp.features plus the mesh-shell collection this tool reaches through."""
    def __init__(self, shell=None):
        super().__init__()
        self.meshShellFeatures = shell


# ── rig ──────────────────────────────────────────────────────────────────────────────────────────

def _rig(monkeypatch, mesh=None, on_add=None, design_type=1, **feat_kw):
    """Wire one mesh + a meshShellFeatures collection into the tool. Returns (mesh, comp, feats)."""
    mesh = mesh if mesh is not None else MeshBody()
    comp = _MeshComp("Comp", mesh_bodies=[mesh])
    mesh.parentComponent = comp
    feats = _ShellFeatures(on_add=on_add, **feat_kw)
    comp.features = _Features(shell=feats)
    install(ms, MakeDesign(comp=comp, design_type=design_type))
    monkeypatch.setattr(ms._MESH, "resolve", lambda raw: (mesh, None))
    monkeypatch.setattr(ms.adsk.core.ValueInput, "createByReal", _ValueInput)
    return mesh, comp, feats


def _hollow(mesh, tri=30370, nodes=15189, volume=0.1159, close=None):
    """The measured in-place hollow: the body is re-triangulated and its volume drops. `close=False`
    leaves the body reading OPEN afterwards - no mechanism is claimed for that, only the flag; an
    open body's volume then reads 0.0 whatever volume says here, because it encloses nothing."""
    def _apply():
        mesh.displayMesh.triangleCount = tri
        mesh.displayMesh.nodeCount = nodes
        mesh._volume_cm3 = volume
        if close is not None:
            mesh._is_closed = close
    return _apply


@pytest.fixture
def rig(monkeypatch):
    """A shell that takes a 12-triangle closed box to 30,370 triangles and 11.6% of its volume."""
    mesh = MeshBody(tri=12, nodes=8, is_closed=True, volume=1.0)
    mesh_, comp, feats = _rig(monkeypatch, mesh=mesh, on_add=_hollow(mesh))
    return types.SimpleNamespace(mesh=mesh, comp=comp, feats=feats, monkeypatch=monkeypatch)


# ── the ValueInput-typed thickness ───────────────────────────────────────────────────────────────

class TestThicknessInput:
    def test_thickness_crosses_as_a_value_input_in_internal_cm(self, rig):
        payload(ms.handler(mesh="H", thickness=2.0, units="mm"))
        assert rig.feats.last_input.thickness.realValue == pytest.approx(0.2)

    def test_thickness_in_inches_is_scaled_by_2_54(self, rig):
        payload(ms.handler(mesh="H", thickness=1.0, units="in"))
        assert rig.feats.last_input.thickness.realValue == pytest.approx(2.54)

    def test_thickness_in_cm_passes_through_unscaled(self, rig):
        payload(ms.handler(mesh="H", thickness=0.5, units="cm"))
        assert rig.feats.last_input.thickness.realValue == pytest.approx(0.5)

    def test_a_raw_number_on_thickness_would_be_refused_by_the_api(self, rig):
        # the fake input rejects a non-ValueInput exactly as the live typed property does
        rig.monkeypatch.setattr(ms.adsk.core.ValueInput, "createByReal", lambda v: v)
        assert "thickness" in error_message(ms.handler(mesh="H", thickness=2.0))


# ── input guards (all BEFORE any mutation) ───────────────────────────────────────────────────────

class TestInputGuards:
    def test_a_missing_thickness_is_refused(self, rig):
        assert "thickness" in error_message(ms.handler(mesh="H"))
        assert rig.feats.add_called is False

    def test_a_zero_thickness_is_refused(self, rig):
        msg = error_message(ms.handler(mesh="H", thickness=0))
        assert "non-zero" in msg and "thickness" in msg
        assert rig.feats.add_called is False

    def test_a_negative_thickness_is_refused_naming_the_value(self, rig):
        msg = error_message(ms.handler(mesh="H", thickness=-2.0))
        assert "positive" in msg and "-2.0" in msg
        assert rig.feats.add_called is False

    def test_a_non_numeric_thickness_is_refused(self, rig):
        assert "number" in error_message(ms.handler(mesh="H", thickness="thick"))
        assert rig.feats.add_called is False

    def test_unknown_units_are_refused(self, rig):
        assert "units" in error_message(ms.handler(mesh="H", thickness=2.0,
                                                   units="furlong")).lower()
        assert rig.feats.add_called is False

    def test_no_active_design_is_an_error(self, rig):
        rig.monkeypatch.setattr(ms._common, "design", lambda: None)
        assert "No active design" in error_message(ms.handler(mesh="H", thickness=2.0))

    def test_a_missing_shell_features_collection_is_an_error(self, monkeypatch):
        _mesh, comp, _feats = _rig(monkeypatch)
        comp.features = _Features(shell=None)
        assert "meshShellFeatures" in error_message(ms.handler(mesh="H", thickness=2.0))

    def test_create_input_returning_none_is_an_error(self, monkeypatch):
        _rig(monkeypatch, create_input_returns_none=True)
        assert "returned nothing" in error_message(ms.handler(mesh="H", thickness=2.0))

    def test_an_add_failure_surfaces_and_is_not_swallowed(self, monkeypatch):
        _rig(monkeypatch, raise_on_add=True)
        assert "meshShellFeatures.add raised" in error_message(ms.handler(mesh="H", thickness=2.0))

    def test_a_brep_body_is_redirected_to_the_mesh_tools(self, monkeypatch):
        _mesh, comp, feats = _rig(monkeypatch)
        monkeypatch.setattr(adsk.fusion, "MeshBody", MeshBody)
        monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody)
        brep = BRepBody(name="Body1", entity_token="BTOK::Body1")
        monkeypatch.setattr(ms._MESH, "resolve", ms._inputs.MeshBodyRef("mesh").resolve)
        install(ms, MakeDesign(comp=comp, design_type=1, tokens={"BTOK::Body1": brep}))
        msg = error_message(ms.handler(mesh="BTOK::Body1", thickness=2.0))
        assert "MESH body" in msg and "SOLID" in msg
        assert feats.add_called is False


# ── verify-the-effect ────────────────────────────────────────────────────────────────────────────

class TestVerification:
    def test_a_successful_shell_reports_the_counts_and_the_volume_drop(self, rig):
        out = payload(ms.handler(mesh="H", thickness=2.0, units="mm"))
        assert out["hollowed"] is True
        assert out["before"] == {"triangle_count": 12, "vertex_count": 8}
        assert out["after"] == {"triangle_count": 30370, "vertex_count": 15189}
        assert out["changed"] == ["triangle_count", "vertex_count", "volume"]
        # 1.0 cm3 -> 0.1159 cm3 is a drop of 0.8841 cm3 = 884.1 mm3
        assert out["volume_change"] == pytest.approx(-884.1)
        assert out["units"] == "mm"
        assert out["feature"] == "MeshShell1"

    def test_a_shell_that_changed_nothing_is_an_error(self, monkeypatch):
        _rig(monkeypatch)   # add() fires no side effect at all
        msg = error_message(ms.handler(mesh="H", thickness=2.0))
        assert "unchanged" in msg and "12 triangles, 8 vertices, the same volume" in msg

    def test_the_no_effect_error_omits_a_volume_it_could_not_read(self, monkeypatch):
        # a volume neither end could report would be a claim about a number nobody measured. An
        # OPEN mesh is not that case - it reads 0.0 - so the unreadable half is modelled directly.
        _rig(monkeypatch, mesh=MeshBody(tri=12, nodes=8, volume_readable=False))
        msg = error_message(ms.handler(mesh="H", thickness=2.0))
        assert "12 triangles, 8 vertices)" in msg
        assert "volume" not in msg

    def test_the_no_effect_error_does_name_an_open_meshs_volume_it_did_read(self, monkeypatch):
        # the mirror of the clause above: an open mesh's 0.0 IS a reading taken at both ends, so
        # "the same volume" is a measured claim here and belongs in the sentence.
        _rig(monkeypatch, mesh=MeshBody(tri=12, nodes=8, is_closed=False))
        msg = error_message(ms.handler(mesh="H", thickness=2.0))
        assert "12 triangles, 8 vertices, the same volume" in msg

    def test_the_no_effect_error_omits_counts_it_could_not_read(self, monkeypatch):
        # the mirror case: the display mesh is unreachable, so "None triangles" must not appear
        _rig(monkeypatch, mesh=MeshBody(volume=1.0, counts_readable=False))
        msg = error_message(ms.handler(mesh="H", thickness=2.0))
        assert "(the same volume)" in msg
        assert "triangles" not in msg and "None" not in msg

    def test_a_volume_that_did_not_drop_is_reported_as_unconfirmed_not_as_a_hollow(self, monkeypatch):
        # triangles moved, so something happened - but a shell whose volume held is not a hollow,
        # and neither the flag nor the note may claim one.
        mesh = MeshBody(tri=12, nodes=8, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_hollow(mesh, volume=1.0))
        out = payload(ms.handler(mesh="H", thickness=2.0))
        assert out["hollowed"] is False
        assert out["changed"] == ["triangle_count", "vertex_count"]
        assert "did not drop" in out["note"]

    def test_an_open_mesh_reports_a_measured_zero_volume_change(self, monkeypatch):
        # an open mesh reads 0.0 at BOTH ends, so the change is a measured zero rather than an
        # unreadable field - and a body that was never closed cannot have been hollowed either.
        mesh = MeshBody(tri=12, nodes=8, is_closed=False)
        _rig(monkeypatch, mesh=mesh, on_add=_hollow(mesh))
        out = payload(ms.handler(mesh="H", thickness=2.0))
        assert out["volume_change"] == 0.0
        assert out["hollowed"] is False
        assert out["watertight"] is False
        assert out["changed"] == ["triangle_count", "vertex_count"]
        assert "does not report itself watertight" in out["note"]

    def test_a_shell_that_broke_the_closure_is_not_reported_as_a_hollow(self, monkeypatch):
        # the trap the measured 0.0 opens: a body that stops reading watertight reports volume 0.0
        # because it encloses nothing, so the delta is the full -1.0 cm3 - the LARGEST drop this
        # tool can see. Read on the number alone that is the deepest hollow; read with the closure
        # flag it is the worst outcome the operation has.
        mesh = MeshBody(tri=12, nodes=8, is_closed=True, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_hollow(mesh, close=False))
        out = payload(ms.handler(mesh="H", thickness=2.0, units="mm"))
        assert out["volume_change"] == pytest.approx(-1000.0)   # the full magnitude came out
        assert out["hollowed"] is False
        assert out["watertight"] is False
        assert "NO LONGER watertight" in out["note"] and "NOT a hollow" in out["note"]
        # the number in the sentence is the one the payload published, never a constant
        assert "-1000.0 mm3" in out["note"] and "reads 0.0" not in out["note"]

    def test_a_lost_closure_whose_volume_is_unreadable_claims_no_number(self, monkeypatch):
        # the closure went, and the volume could not be read at both ends - so there is no change
        # to explain and the note must not assert the 0.0 an unreadable field never reported.
        mesh = MeshBody(tri=12, nodes=8, is_closed=True, volume=1.0)

        def _lose_closure_and_the_volume():
            mesh.displayMesh.triangleCount = 30370
            mesh._is_closed = False
            mesh._volume_readable = False
        _rig(monkeypatch, mesh=mesh, on_add=_lose_closure_and_the_volume)
        out = payload(ms.handler(mesh="H", thickness=2.0, units="mm"))
        assert out["volume_change"] is None
        assert out["hollowed"] is False
        assert out["watertight"] is False
        assert "NO LONGER watertight" in out["note"]
        assert "volume could not be read" in out["note"]
        assert "0.0" not in out["note"] and "None" not in out["note"]

    def test_a_closure_flag_readable_before_but_not_after_is_not_a_lost_closure(self, monkeypatch):
        # closure_lost needs the after-flag to SAY false. A flag that stopped reading is not a
        # false one: it leaves the hollow unconfirmed, and claiming the closure was lost would
        # invent a reading nobody got.
        mesh = MeshBody(tri=12, nodes=8, is_closed=True, volume=1.0)

        def _hollow_then_lose_the_flag():
            mesh.displayMesh.triangleCount = 30370
            mesh._volume_cm3 = 0.1159
            mesh._closed_readable = False
        _rig(monkeypatch, mesh=mesh, on_add=_hollow_then_lose_the_flag)
        out = payload(ms.handler(mesh="H", thickness=2.0, units="mm"))
        assert out["watertight"] is None
        assert out["hollowed"] is False
        assert "does not report itself watertight" in out["note"]
        assert "NO LONGER watertight" not in out["note"]

    def test_a_volume_that_ROSE_is_not_a_hollow(self, monkeypatch):
        # a hollow is a DROP. A shell that left the body enclosing MORE than it started with did
        # not take material out, and a gate keyed on "the volume moved" would call it a hollow.
        mesh = MeshBody(tri=12, nodes=8, is_closed=True, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_hollow(mesh, volume=1.5))
        out = payload(ms.handler(mesh="H", thickness=2.0, units="mm"))
        assert out["volume_change"] == pytest.approx(500.0)
        assert out["hollowed"] is False
        assert "did not drop" in out["note"]

    def test_an_unreadable_closure_flag_cannot_confirm_a_hollow(self, monkeypatch):
        # the flag has to SAY closed: an unreadable one leaves the drop unexplained, so it must not
        # be waved through as a hollow the way a False would be reported as a closure loss.
        mesh = MeshBody(tri=12, nodes=8, is_closed=True, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_hollow(mesh))
        mesh._closed_readable = False
        out = payload(ms.handler(mesh="H", thickness=2.0, units="mm"))
        assert out["watertight"] is None
        assert out["hollowed"] is False
        assert "does not report itself watertight" in out["note"]

    def test_an_unreadable_volume_reports_null_and_says_so(self, monkeypatch):
        # the only shape that makes volume_change null - never the 0.0 an open mesh reports.
        mesh = MeshBody(tri=12, nodes=8, volume_readable=False)
        _rig(monkeypatch, mesh=mesh, on_add=_hollow(mesh))
        out = payload(ms.handler(mesh="H", thickness=2.0))
        assert out["volume_change"] is None
        assert out["hollowed"] is False
        assert out["changed"] == ["triangle_count", "vertex_count"]
        assert "could not be read" in out["note"]

    def test_an_unreadable_mesh_is_reported_as_unverified(self, monkeypatch):
        mesh = MeshBody()
        _m, comp, feats = _rig(monkeypatch, mesh=mesh)

        def _wipe():
            mesh._dead = True
            comp.dead = True
        feats._on_add = _wipe
        assert "UNVERIFIED" in error_message(ms.handler(mesh="H", thickness=2.0))


# ── the thickness read-back (never an echo) ──────────────────────────────────────────────────────

class TestThicknessReadBack:
    def test_the_landed_thickness_is_reported_in_the_call_units(self, rig):
        out = payload(ms.handler(mesh="H", thickness=2.0, units="mm"))
        assert out["thickness"] == pytest.approx(2.0)

    def test_a_thickness_the_api_did_not_take_is_an_error_not_an_echo(self, monkeypatch):
        mesh = MeshBody(tri=12, nodes=8, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_hollow(mesh), feature_thickness=0.05)
        msg = error_message(ms.handler(mesh="H", thickness=2.0, units="mm"))
        assert "0.5 mm" in msg and "2.0 mm was" in msg

    def test_an_unreadable_thickness_is_reported_as_unverified_not_as_the_request(self, monkeypatch):
        mesh = MeshBody(tri=12, nodes=8, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_hollow(mesh), design_type=0, none_feature=True)
        out = payload(ms.handler(mesh="H", thickness=2.0))
        assert out["thickness"] is None
        assert "thickness_unverified" in out


# ── mode routing (the measured direct-mode None return) ──────────────────────────────────────────

class TestModeRouting:
    def test_direct_mode_returns_no_feature_yet_the_landed_hollow_is_success(self, monkeypatch):
        mesh = MeshBody(tri=12, nodes=8, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_hollow(mesh), design_type=0, none_feature=True)
        out = payload(ms.handler(mesh="H", thickness=2.0))
        assert out["hollowed"] is True
        assert out["no_timeline_feature"] is True
        assert "feature" not in out
        assert "DIRECT mode" in out["note"]

    def test_direct_mode_with_no_effect_is_still_an_error(self, monkeypatch):
        # a None return is never itself proof of an effect - the census decides
        _rig(monkeypatch, design_type=0, none_feature=True)
        assert "unchanged" in error_message(ms.handler(mesh="H", thickness=2.0))

    def test_parametric_no_feature_return_stays_an_honest_error(self, monkeypatch):
        mesh = MeshBody(tri=12, nodes=8, volume=1.0)
        _rig(monkeypatch, mesh=mesh, on_add=_hollow(mesh), design_type=1, none_feature=True)
        assert "returned no feature" in error_message(ms.handler(mesh="H", thickness=2.0))

    def test_no_base_feature_scope_is_opened(self, rig):
        # measured: meshShellFeatures.add returns a real feature at PLAIN parametric scope
        payload(ms.handler(mesh="H", thickness=2.0))
        assert rig.feats.last_input.targetBaseFeature is None
