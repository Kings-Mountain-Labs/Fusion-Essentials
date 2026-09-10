"""Unit tests for ``cam_create_setup.py`` — create a CAM (Manufacture) setup on a part.

A freshly imported bare part has no CAM job; this tool creates the first setup so the other CAM
authoring tools have something to work in. Covers operation-type dispatch (milling/turning),
model selection (handles / names / all-bodies default), naming, and the no-design / no-bodies
guards. No live Fusion - fakes mimic adsk.cam.CAM.setups.
"""

import json
from types import SimpleNamespace

import adsk.cam
import adsk.fusion

from conftest import BRepBody, FakeSetup, FakeSetups, MakeComp, install, load_tool, make_design

cs = load_tool("cam_create_setup")


class _StubbornSetup(FakeSetup):
    """A setup whose name write is accepted and dropped - the rename that never took."""

    @property
    def name(self):
        return "Setup1"

    @name.setter
    def name(self, value):
        pass


def _install(monkeypatch, bodies=None, has_cam=True, setups=None):
    """Wire a design holding `bodies` and a CAM product whose setups collection is `setups`."""
    comp = MakeComp(bodies=[BRepBody("Body1")] if bodies is None else bodies)
    design = make_design(comp=comp)
    cam = SimpleNamespace(setups=setups if setups is not None else FakeSetups()) if has_cam else None

    # the models input discriminates a BRepBody by isinstance, so the shared fake IS the class
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(cs, "get_cam", lambda: ((cam, None) if cam else (None, "no CAM")))
    # models is a TargetRefList -> resolves through _common.design()/target_component()
    install(cs, design)
    return design, cam, comp


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


# ── operation type ───────────────────────────────────────────────────────────

class TestOperationType:
    def test_default_is_milling(self, monkeypatch):
        _, cam, _ = _install(monkeypatch)
        out = _payload(cs.handler())
        assert cam.setups._added[-1].operationType == adsk.cam.OperationTypes.MillingOperation
        assert out["created"] is True

    def test_turning(self, monkeypatch):
        _, cam, _ = _install(monkeypatch)
        _payload(cs.handler(operation_type="turning"))
        assert cam.setups._added[-1].operationType == adsk.cam.OperationTypes.TurningOperation

    def test_phantom_setup_that_never_lands_bites(self, monkeypatch):
        # add() returns a setup object but it never appears in the re-listed collection -> error
        _, cam, _ = _install(monkeypatch)
        cam.setups.add = lambda inp: FakeSetup("Setup1")   # returned, never appended
        res = cs.handler()
        assert res["isError"] is True
        assert "did not land" in res["message"]

    def test_unknown_type_errors(self, monkeypatch):
        _install(monkeypatch)
        res = cs.handler(operation_type="welding")
        assert res["isError"] is True and "operation_type" in res["message"]


# ── model selection ──────────────────────────────────────────────────────────

class TestModelSelection:
    def test_all_root_bodies_when_omitted(self, monkeypatch):
        _, cam, _ = _install(monkeypatch, bodies=[BRepBody("A"), BRepBody("B")])
        _payload(cs.handler())
        models = cam.setups._added[-1].models
        assert {m.name for m in models} == {"A", "B"}

    def test_default_set_is_every_root_body_including_a_surface_one(self, monkeypatch):
        # The default machining set is every root BRep body, solid AND surface - what the walk does
        # and what the tool now claims. An isSolid filter here would silently drop 'Skin'.
        _, cam, _ = _install(monkeypatch, bodies=[BRepBody("Plate"), BRepBody("Skin", is_solid=False)])
        _payload(cs.handler())
        assert {m.name for m in cam.setups._added[-1].models} == {"Plate", "Skin"}

    def test_named_body(self, monkeypatch):
        _, cam, _ = _install(monkeypatch, bodies=[BRepBody("Widget"), BRepBody("Other")])
        _payload(cs.handler(models="Widget"))
        models = cam.setups._added[-1].models
        assert [m.name for m in models] == ["Widget"]

    def test_body_by_handle(self, monkeypatch):
        design, cam, _ = _install(monkeypatch, bodies=[BRepBody("Body1")])
        h = "/v" + "Z" * 70
        design._tokens[h] = BRepBody("FromHandle")
        _payload(cs.handler(models=h))
        assert cam.setups._added[-1].models[0].name == "FromHandle"

    def test_missing_named_model_errors(self, monkeypatch):
        _install(monkeypatch, bodies=[BRepBody("Body1")])
        res = cs.handler(models="Nope")
        assert res["isError"] is True and "Nope" in res["message"]

    def test_no_bodies_at_all_errors(self, monkeypatch):
        _install(monkeypatch, bodies=[])
        res = cs.handler()
        assert res["isError"] is True and "body" in res["message"].lower()

    def test_the_refusal_describes_the_set_the_walk_actually_takes(self, monkeypatch):
        # The default set is every root BRep body, so a refusal claiming SOLID bodies would send a
        # caller looking for a filter the tool does not apply. It names the way out instead.
        _install(monkeypatch, bodies=[])
        msg = cs.handler()["message"]
        assert "solid" not in msg.lower()
        assert "'models'" in msg and "sub-component" in msg

    def test_the_description_claims_the_same_default_set_as_the_walk(self):
        # The wire claim and the walk are one fact - a wire string promising SOLID bodies while the
        # walk returns every BRep body is the mismatch a caller cannot see. The claim rides on the
        # 'models' input, the value it describes.
        assert "= every root-component body" in cs._MODELS.schema()["description"]
        assert "solid" not in cs._MODELS.schema()["description"].lower()
        assert "solid" not in cs.TOOL_DESCRIPTION.lower()


# ── naming + guards ──────────────────────────────────────────────────────────

class TestNamingAndGuards:
    def test_custom_name(self, monkeypatch):
        _, cam, _ = _install(monkeypatch)
        out = _payload(cs.handler(name="Op10 Mill"))
        assert cam.setups.item(0).name == "Op10 Mill"
        assert out["setup_name"] == "Op10 Mill"

    def test_blank_name_not_assigned(self, monkeypatch):
        # a whitespace-only name is no rename at all, so the setup keeps the auto-name the add
        # gave it ("Setup1") - it is NOT set to "   ".
        _, cam, _ = _install(monkeypatch)
        out = _payload(cs.handler(name="   "))
        assert cam.setups.item(0).name == "Setup1"
        assert out["setup_name"] == "Setup1"

    def test_a_name_a_setup_already_answers_to_is_refused_before_the_add(self, monkeypatch):
        # Measured: Setup.name dedupes like Operation.name - 'LegSetup' with one taken landed
        # 'LegSetup1'. The name is refused before the add so the caller never gets a setup under a
        # name it did not ask for.
        _, cam, _ = _install(monkeypatch)
        _payload(cs.handler(name="LegSetup"))                 # the first one takes the name
        res = cs.handler(name="LegSetup")
        assert res["isError"] is True
        assert "already answer to 'LegSetup'" in res["message"]
        assert "dedupes rather than refusing" in res["message"]
        assert cam.setups.count == 1                          # the second one was never added

    def test_a_free_setup_name_still_creates(self, monkeypatch):
        # the other side of the clash gate: a name no setup carries is not refused
        _, cam, _ = _install(monkeypatch)
        out = _payload(cs.handler(name="LegSetup"))
        assert out["setup_name"] == "LegSetup" and cam.setups.count == 1

    def test_a_declined_name_is_disclosed_not_published_as_requested(self, monkeypatch):
        # The setup LANDED, so a name the platform declines (or dedupes) is a disclosure, not a
        # failed create - and the payload publishes the name Setup.name reads back.
        _install(monkeypatch, setups=FakeSetups(new_setup=_StubbornSetup("Setup1")))
        out = _payload(cs.handler(name="Op10 Mill"))
        assert out["setup_name"] == "Setup1"
        assert "Op10 Mill" in out["rename_warning"] and "did not take" in out["rename_warning"]

    def test_no_cam_product_errors(self, monkeypatch):
        _install(monkeypatch, has_cam=False)
        res = cs.handler()
        assert res["isError"] is True and "CAM" in res["message"]


# ── output fields ────────────────────────────────────────────────────────────

class TestOutputFields:
    def test_model_count_and_names_reported(self, monkeypatch):
        _install(monkeypatch, bodies=[BRepBody("A"), BRepBody("B"), BRepBody("C")])
        out = _payload(cs.handler())
        assert out["model_count"] == 3
        assert set(out["models"]) == {"A", "B", "C"}
        assert out["operation_count"] == 0          # fresh setup has no operations
        assert out["operation_type"] == "milling"

    def test_single_body_model_count_one(self, monkeypatch):
        _install(monkeypatch, bodies=[BRepBody("Solo")])
        out = _payload(cs.handler())
        assert out["model_count"] == 1
        assert out["models"] == ["Solo"]

    def test_setup_creation_failure_reported(self, monkeypatch):
        _, cam, _ = _install(monkeypatch)
        def boom(_inp):
            raise RuntimeError("kaboom")
        cam.setups.add = boom
        res = cs.handler()
        assert res["isError"] is True
        assert "kaboom" in res["message"] and "milling" in res["message"]
