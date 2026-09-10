"""Unit tests for ``view_list_workspaces.py`` - the workspace listing and its active-doc flag."""

import json

import pytest

from conftest import FakeApplication, FakeUserInterface, load_tool

vw = load_tool("view_list_workspaces")
vc = load_tool("_view_common")


class _WS:
    """A workspace. Bespoke: adsk.core.Workspace carries no shape dump, so there is no shared fake
    to stand for it."""

    def __init__(self, id, name, is_active=False, product_type="Design"):
        self.id = id
        self.name = name
        self.isActive = is_active
        self.productType = product_type


@pytest.fixture
def ui(monkeypatch):
    """Factory: install an Application whose userInterface holds `workspaces`, the active one being
    whichever reads isActive. Returns the UserInterface."""
    def _install(workspaces):
        ws = list(workspaces)
        interface = FakeUserInterface(
            workspaces=ws, active_workspace=next((w for w in ws if w.isActive), None))
        monkeypatch.setattr(vc, "app", FakeApplication(user_interface=interface))
        return interface
    return _install


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class TestList:
    def test_lists_all_and_flags_active(self, ui):
        ui([_WS("FusionSolidEnvironment", "Design", is_active=True),
            _WS("CAMEnvironment", "Manufacture")])
        out = _payload(vw.handler())
        assert out["workspace_count"] == 2
        assert out["active_workspace"] == "Design"
        ids = {w["id"] for w in out["workspaces"]}
        assert ids == {"FusionSolidEnvironment", "CAMEnvironment"}

    def test_none_active(self, ui):
        ui([_WS("A", "Alpha"), _WS("B", "Beta")])
        out = _payload(vw.handler())
        assert out["active_workspace"] is None
