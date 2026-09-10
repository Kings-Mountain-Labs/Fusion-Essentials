# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The harness imports current source and records pristine tool seams."""

import json
import os
import py_compile
import subprocess
import sys

import pytest

import conftest

# A tool module re-imported here on purpose - the vehicle for the import ORDER, not itself under
# test. Any module carrying a module-level seam serves: this one binds `target_component` from
# _common and calls no register() at import, so a second execution mints its own Tool/Item and
# changes nothing outside its new module object.
_MODULE = "model_hole"
_FULL = f"mcpServer.tools.{_MODULE}"

_PATCHED = object()


def _seams(mod):
    """The seam attributes `mod` actually carries - what a restore acts on, and what a pristine
    record has to hold."""
    return {a: getattr(mod, a) for a in conftest._SEAM_ATTRS if hasattr(mod, a)}


@pytest.fixture
def freshly_imported(monkeypatch):
    """The tool module imported for the first time INSIDE a test - the order the autouse fixture's
    setup sweep cannot reach, since that sweep has already run by the time a test body executes.

    The session's canonical module object and its recorded seams are dropped through monkeypatch, so
    both are put back afterwards and no other test meets this second copy."""
    conftest.load_tool(_MODULE)                                    # something to restore afterwards
    monkeypatch.delitem(sys.modules, _FULL)
    monkeypatch.delitem(conftest._PRISTINE_SEAMS, _FULL, raising=False)
    return conftest.load_tool(_MODULE)


class TestPristineSeamCapture:
    def test_a_module_imported_inside_a_test_is_recorded_at_load(self, freshly_imported):
        assert _FULL in conftest._PRISTINE_SEAMS, (
            "load_tool recorded no pristine seams for a module it just executed - the first "
            "recording then happens at TEARDOWN, after the body has had its chance to patch them")
        assert conftest._PRISTINE_SEAMS[_FULL] == _seams(freshly_imported)

    def test_the_recorded_seam_set_is_not_empty(self, freshly_imported):
        # the equality above passes vacuously against a module carrying no seam at all, so the
        # vehicle's own seams are pinned here rather than assumed.
        assert conftest._PRISTINE_SEAMS[_FULL], f"{_MODULE} carries no seam - pick another vehicle"

    def test_a_test_body_patch_never_becomes_the_pristine_value(self, freshly_imported):
        # the imperative patch shape the autouse restore exists to undo (tests/CLAUDE.md), applied
        # to a module this very test imported: the recorded value must still be the imported one, or
        # the restore hands the patch to every test that follows.
        mod = freshly_imported
        seam = sorted(_seams(mod))[0]
        as_imported = getattr(mod, seam)
        setattr(mod, seam, _PATCHED)
        assert conftest._pristine_seam(_FULL, mod)[seam] is as_imported


@pytest.fixture
def source_import(tmp_path):
    def run(mode):
        tool = tmp_path / "_fresh_source_probe.py"
        subject = tmp_path / "_fresh_source_helper.py" if mode == "helper" else tool
        subject.write_bytes(b'VALUE = "OLD"\n')
        stamp = subject.stat()
        py_compile.compile(str(subject), doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
        subject.write_bytes(b'VALUE = "NEW"\n')
        os.utime(subject, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        if mode == "helper":
            tool.write_text("from ._fresh_source_helper import VALUE\n", encoding="utf-8")
        driver = r"""
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import conftest
conftest.install_mock_adsk()
conftest.TOOLS_DIR = sys.argv[2]
before_path, before_flag = list(sys.meta_path), sys.dont_write_bytecode
if sys.argv[3] == "failure":
    pathlib.Path(sys.argv[2], "_fresh_source_probe.py").write_text(
        "raise RuntimeError('source failure')\n", encoding="utf-8")
    try:
        conftest.load_tool("_fresh_source_probe")
    except RuntimeError as exc:
        answer = {"error": str(exc),
                  "cached_target": "mcpServer.tools._fresh_source_probe" in sys.modules}
else:
    loaded = conftest.load_tool("_fresh_source_probe")
    answer = {"value": loaded.VALUE,
              "canonical": conftest.load_tool("_fresh_source_probe") is loaded}
answer.update(meta_path_restored=sys.meta_path == before_path,
              bytecode_flag_restored=sys.dont_write_bytecode == before_flag)
print(json.dumps(answer))
"""
        result = subprocess.run(
            [sys.executable, "-c", driver, os.path.dirname(conftest.__file__), str(tmp_path), mode],
            capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    return run


@pytest.mark.parametrize("mode", ["direct", "helper"])
def test_first_import_ignores_stale_same_stamp_bytecode(source_import, mode):
    assert source_import(mode) == {"value": "NEW", "canonical": True,
                                   "meta_path_restored": True, "bytecode_flag_restored": True}


def test_failed_source_import_removes_its_partial_module(source_import):
    assert source_import("failure") == {"error": "source failure", "cached_target": False,
                                        "meta_path_restored": True, "bytecode_flag_restored": True}
