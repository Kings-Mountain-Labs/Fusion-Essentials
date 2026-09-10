"""Unit tests for ``cam_save_template.py`` - bundling operations into a NEW library template.

Covers ``_as_cam_template`` (createFromOperations' annotation says list[Operation] while its own
documentation says CAMTemplate, so both shapes are normalised), the input validation, and the
save's read-back: a template that does not load back from the url importTemplate returned is not
a saved template.
"""

import json
from types import SimpleNamespace

from conftest import FakeOperation, FakeSetup, _NamedCollection, load_tool, make_cam

ct = load_tool("cam_save_template")


class _Url:
    """A library URL: toString() plus the leafName the shared asset_leaf_keys reading takes - the
    last path segment, as adsk.core.URL answers it."""
    def __init__(self, s): self._s = s
    def toString(self): return self._s
    @property
    def leafName(self): return self._s.rstrip("/").rsplit("/", 1)[-1]


# ── _as_cam_template: normalise createFromOperations' contradictory return ──────────────────────
#
# The live API annotation is list[Operation] but the docstring claims a CAMTemplate. Save code
# that assumes a single CAMTemplate and sets .name on it / passes it to importTemplate breaks if
# the binding actually returns a list. These pin the normalisation for BOTH shapes so the save
# path is correct whichever the installed Fusion returns. (CAMTemplate.cast is the discriminator.)

class _FakeTemplate:
    def __init__(self, name="T"):
        self.name = name
        self.isValidTemplate = True


def _patch_cast(monkey_is_template):
    """Point adsk.cam.CAMTemplate.cast at a predicate: it returns the object iff it's a template."""
    import adsk.cam
    adsk.cam.CAMTemplate.cast = staticmethod(
        lambda x: x if monkey_is_template(x) else None)


class TestAsCamTemplate:
    def test_passthrough_when_already_a_template(self):
        t = _FakeTemplate("Slot Mill")
        _patch_cast(lambda x: isinstance(x, _FakeTemplate))
        assert ct._as_cam_template(t) is t

    def test_recovers_template_from_a_list_result(self):
        # the annotated list[Operation] shape, but containing the template object
        t = _FakeTemplate("Bundle")
        _patch_cast(lambda x: isinstance(x, _FakeTemplate))
        assert ct._as_cam_template([t]) is t

    def test_recovers_template_from_a_collection_result(self):
        t = _FakeTemplate("Bundle")
        _patch_cast(lambda x: isinstance(x, _FakeTemplate))
        assert ct._as_cam_template(_NamedCollection([t])) is t

    def test_returns_none_when_no_template_present(self):
        # a pure list of non-template operations -> None (caller reports it instead of crashing)
        _patch_cast(lambda x: isinstance(x, _FakeTemplate))
        assert ct._as_cam_template(["op1", "op2"]) is None
        assert ct._as_cam_template(object()) is None



# ── save_operations_as_template_handler: input validation + missing-op detection ────────────────────

def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _setup(name, op_names):
    """A setup whose allOperations answers the named operations."""
    return FakeSetup(name, ops=[FakeOperation(n) for n in op_names])


def _wire_save(monkeypatch, cam):
    monkeypatch.setattr(ct, "get_cam", lambda: (cam, None))
    monkeypatch.setattr(ct, "_template_library", lambda: (SimpleNamespace(), None))
    import adsk.cam
    adsk.cam.Operation.cast = staticmethod(lambda x: x)


class TestSaveOperationsValidation:
    def test_missing_template_name(self):
        res = ct.handler(template_name="", operations="a", setup="S")
        assert res["isError"] is True and "template_name" in res["message"]

    def test_missing_operations_list(self):
        res = ct.handler(template_name="T", operations="  ,  ", setup="S")
        assert res["isError"] is True and "operations" in res["message"]

    def test_setup_not_found_lists_available(self, monkeypatch):
        _wire_save(monkeypatch, make_cam(_setup("Roughing", ["a"])))
        res = ct.handler(
            template_name="T", operations="a", setup="Ghost")
        assert res["isError"] is True and "Roughing" in res["message"]

    def test_missing_operations_named_in_error(self, monkeypatch):
        _wire_save(monkeypatch, make_cam(_setup("S", ["Face1", "Contour"])))
        res = ct.handler(
            template_name="T", operations="Face1, Ghost, AlsoGone", setup="S")
        assert res["isError"] is True
        assert "Ghost" in res["message"] and "AlsoGone" in res["message"]
        assert "Face1" not in res["message"].split("Available")[0]   # Face1 was found, not missing


class _RaiseOnName:
    """A template whose .name setter raises - simulates a read-only or locked property."""
    isValidTemplate = True
    description = ""

    def __getattribute__(self, item):
        if item == "name":
            return "original"
        return super().__getattribute__(item)

    def __setattr__(self, key, value):
        if key == "name":
            raise AttributeError("name is read-only")
        super().__setattr__(key, value)


class TestSaveTemplateRename:
    """Rename/description mutations must raise rather than silently save under the original name."""

    def _wire_full_save(self, monkeypatch, template_obj):
        cam = make_cam(_setup("S", ["Face1"]))
        monkeypatch.setattr(ct, "get_cam", lambda: (cam, None))
        # The asset loads back the template that was stored in it - what a template library does, and
        # what lets the save's name read-back mean anything.
        held = {}

        def _import(t, dest):
            held["template"] = t
            return _Url("root://T1.f3dhsm-template")

        lib = SimpleNamespace(urlByLocation=lambda loc: "root://", childFolderURLs=lambda u: [],
                              importTemplate=_import,
                              templateAtURL=lambda u: held.get("template"))
        monkeypatch.setattr(ct, "_template_library", lambda: (lib, None))
        import adsk.cam
        adsk.cam.Operation.cast = staticmethod(lambda x: x)
        adsk.cam.CAMTemplate.createFromOperations = staticmethod(lambda ops: template_obj)
        adsk.cam.CAMTemplate.cast = staticmethod(
            lambda x: x if isinstance(x, _FakeTemplate) or isinstance(x, _RaiseOnName) else None)
        return lib

    def test_rename_failure_propagates(self, monkeypatch):
        # A read-only .name assignment must raise and propagate out of the handler - a silent
        # no-op would save under the wrong name while reporting saved=True.
        import pytest
        t = _RaiseOnName()
        self._wire_full_save(monkeypatch, t)
        with pytest.raises(AttributeError, match="name is read-only"):
            ct.handler(
                template_name="My Template", operations="Face1", setup="S")

    def test_rename_succeeds_reports_correct_name(self, monkeypatch):
        t = _FakeTemplate("old")
        self._wire_full_save(monkeypatch, t)
        out = _payload(ct.handler(
            template_name="New Name", operations="Face1", setup="S"))
        assert out["saved"] is True and t.name == "New Name"

    def test_saved_template_that_does_not_load_back_bites(self, monkeypatch):
        # importTemplate returned a URL but nothing loads back from it -> error, not saved=True
        t = _FakeTemplate("old")
        lib = self._wire_full_save(monkeypatch, t)
        lib.templateAtURL = lambda u: None
        res = ct.handler(
            template_name="Ghost", operations="Face1", setup="S")
        assert res["isError"] is True
        assert "did not land" in res["message"]

    def test_a_stored_template_under_another_name_is_an_error(self, monkeypatch):
        # THE BITE: something loads back from the url, so the load-back gate above passes - but it
        # is not the template this call named, and 'template' would publish the name that was WRITTEN.
        t = _FakeTemplate("old")
        lib = self._wire_full_save(monkeypatch, t)
        lib.templateAtURL = lambda u: _FakeTemplate("Somebody Else")
        res = ct.handler(template_name="Slot Mill", operations="Face1", setup="S")
        assert res["isError"] is True
        assert "'Somebody Else'" in res["message"] and "'Slot Mill'" in res["message"]

    def test_the_published_name_is_the_one_the_stored_template_reads(self, monkeypatch):
        # A case-only difference is the SAME template, and what is published is the stored spelling.
        t = _FakeTemplate("old")
        lib = self._wire_full_save(monkeypatch, t)
        lib.templateAtURL = lambda u: _FakeTemplate("Slot MILL")
        out = _payload(ct.handler(template_name="slot mill", operations="Face1", setup="S"))
        assert out["template"] == "Slot MILL"
