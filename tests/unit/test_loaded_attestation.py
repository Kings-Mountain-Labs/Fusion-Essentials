"""Offline proof for the bounded loaded-code capture used by /health."""

import importlib
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import struct
import sys
import types

import pytest

from lib import loaded_attestation as attestation


def _source(tmp_path, name, text):
    path = tmp_path / (name + ".py")
    path.write_text(text, encoding="utf-8")
    return path


def _forget(name):
    sys.modules.pop(name, None)


def test_capture_restores_the_previous_profiler_and_records_module_code(tmp_path):
    name = "loaded_attestation_profile_case"
    path = _source(tmp_path, name, "VALUE = 'first'\n")
    prior = sys.getprofile()
    try:
        attestation.begin(str(tmp_path))
        module = types.ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        attestation.finish()
        result = attestation.attest()
        assert sys.getprofile() is prior
        assert result["complete"] is True and result["executed_module_count"] == 1
    finally:
        attestation.finish()
        _forget(name)


def test_stale_same_size_source_or_pyc_code_is_refused(tmp_path):
    name = "loaded_attestation_stale_case"
    path = _source(tmp_path, name, "VALUE = 'old'\n")
    old_time = path.stat().st_mtime
    try:
        attestation.begin(str(tmp_path))
        module = types.ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        attestation.finish()
        path.write_text("VALUE = 'new'\n", encoding="utf-8")
        os.utime(path, (old_time, old_time))
        result = attestation.attest()
        assert result["loaded_matches_source"] is False
        assert any("loaded code differs" in p for p in result["problems"])
    finally:
        attestation.finish()
        _forget(name)


def test_same_path_module_replacement_is_refused(tmp_path):
    name = "loaded_attestation_replaced_case"
    path = _source(tmp_path, name, "VALUE = 'one'\n")
    try:
        attestation.begin(str(tmp_path))
        module = types.ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        attestation.finish()
        replacement = types.ModuleType(name)
        replacement.__file__ = str(path)
        sys.modules[name] = replacement
        result = attestation.attest()
        assert result["complete"] is False
        assert any("was replaced" in p for p in result["problems"])
    finally:
        attestation.finish()
        _forget(name)


def test_nested_and_displaced_capture_windows_fail_closed_without_clobbering_profiler(tmp_path):
    prior = sys.getprofile()
    replacement = lambda frame, event, arg: None
    try:
        attestation.begin(str(tmp_path))
        attestation.begin(str(tmp_path))
        attestation.finish()
        nested = attestation.attest()
        assert any("another capture" in problem for problem in nested["problems"])

        attestation.begin(str(tmp_path))
        sys.setprofile(replacement)
        attestation.finish()
        displaced = attestation.attest()
        assert sys.getprofile() is replacement
        assert any("profiler was replaced" in problem for problem in displaced["problems"])
    finally:
        sys.setprofile(prior)


def test_module_loaded_after_the_capture_window_is_unproven(tmp_path):
    first = "loaded_attestation_window_first"
    second = "loaded_attestation_window_second"
    first_path = _source(tmp_path, first, "VALUE = 1\n")
    second_path = _source(tmp_path, second, "VALUE = 2\n")
    try:
        attestation.begin(str(tmp_path))
        module = types.ModuleType(first)
        module.__file__ = str(first_path)
        sys.modules[first] = module
        exec(compile(first_path.read_bytes(), str(first_path), "exec"), module.__dict__)
        attestation.finish()
        late = types.ModuleType(second)
        late.__file__ = str(second_path)
        sys.modules[second] = late
        exec(compile(second_path.read_bytes(), str(second_path), "exec"), late.__dict__)
        result = attestation.attest()
        assert result["complete"] is False
        assert any("uncaptured loaded module" in problem for problem in result["problems"])
    finally:
        attestation.finish()
        _forget(first)
        _forget(second)


def test_attest_during_capture_is_read_only(tmp_path):
    prior = sys.getprofile()
    try:
        attestation.begin(str(tmp_path))
        installed = sys.getprofile()
        result = attestation.attest()
        assert result["complete"] is False
        assert result["problems"] == ["capture window is still active"]
        assert sys.getprofile() is installed
        attestation.finish()
        assert sys.getprofile() is prior
    finally:
        attestation.finish()


@pytest.mark.parametrize("commands_raise", [False, True])
def test_bootstrap_restores_the_prior_profiler_on_every_import_exit(tmp_path, commands_raise):
    package_name = "loaded_attestation_bootstrap_case"
    package = types.ModuleType(package_name)
    package.__path__ = [str(tmp_path)]
    lib = types.ModuleType(package_name + ".lib")
    lib.__path__ = []
    futil = types.ModuleType(package_name + ".lib.fusion360utils")
    names = [package_name, package_name + ".lib",
             package_name + ".lib.loaded_attestation",
             package_name + ".lib.fusion360utils",
             package_name + ".commands", package_name + ".bootstrap"]
    sys.modules[package_name] = package
    sys.modules[package_name + ".lib"] = lib
    sys.modules[package_name + ".lib.loaded_attestation"] = attestation
    sys.modules[package_name + ".lib.fusion360utils"] = futil
    if commands_raise:
        commands = tmp_path / "commands"
        commands.mkdir()
        (commands / "__init__.py").write_text("raise RuntimeError('commands failed')\n",
                                               encoding="utf-8")
    else:
        sys.modules[package_name + ".commands"] = types.ModuleType(package_name + ".commands")
    original = sys.getprofile()
    prior = lambda frame, event, arg: None
    sys.setprofile(prior)
    source = Path(__file__).parents[2] / "Fusion-Essentials.py"
    spec = importlib.util.spec_from_file_location(package_name + ".bootstrap", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        if commands_raise:
            with pytest.raises(RuntimeError, match="commands failed"):
                spec.loader.exec_module(module)
        else:
            spec.loader.exec_module(module)
        assert sys.getprofile() is prior
    finally:
        attestation.finish()
        sys.setprofile(original)
        for name in names:
            sys.modules.pop(name, None)


def test_code_digest_is_stable_when_code_members_gain_references():
    code = compile("x = 1\ndef f():\n    return x\n", "probe.py", "exec")
    before = attestation._code_digest(code)
    held = [code.co_consts, code.co_names, code.co_code]
    held.extend(code.co_consts)
    assert held
    assert attestation._code_digest(code) == before


def test_code_digest_supports_slice_and_order_independent_frozenset_constants():
    base = compile("value = (1, 2)\n", "probe.py", "exec")
    left = base.replace(co_consts=base.co_consts + (
        slice(None, 20, None), frozenset(("one", 1))))
    right = base.replace(co_consts=base.co_consts + (
        slice(None, 20, None), frozenset((1, "one"))))
    changed = base.replace(co_consts=base.co_consts + (
        slice(None, 21, None), frozenset((1, "one"))))
    assert attestation._code_digest(left) == attestation._code_digest(right)
    assert attestation._code_digest(left) != attestation._code_digest(changed)
    nan_one = struct.unpack("!d", bytes.fromhex("7ff8000000000001"))[0]
    nan_two = struct.unpack("!d", bytes.fromhex("7ff8000000000002"))[0]
    assert attestation._constant_row(0.0) != attestation._constant_row(-0.0)
    assert attestation._constant_row(nan_one) != attestation._constant_row(nan_two)


def test_capture_digest_failure_does_not_abort_module_import(tmp_path, monkeypatch):
    name = "loaded_attestation_capture_failure"
    path = _source(tmp_path, name, "VALUE = 1\n")
    monkeypatch.setattr(attestation, "_code_digest",
                        lambda code: (_ for _ in ()).throw(ValueError("unsupported")))
    sys.path.insert(0, str(tmp_path))
    try:
        attestation.begin(str(tmp_path))
        module = importlib.import_module(name)
        attestation.finish()
        result = attestation.attest()
        assert module.VALUE == 1
        assert result["complete"] is False
        assert any("module code capture failed" in problem for problem in result["problems"])
    finally:
        attestation.finish()
        _forget(name)
        sys.path.remove(str(tmp_path))


def test_source_and_pyc_loader_graphs_match_a_fresh_compile(tmp_path, monkeypatch):
    first = "loaded_attestation_graph_first"
    second = "loaded_attestation_graph_second"
    _source(tmp_path, second,
            "import sys\nLABEL = 'attestation value with spaces'\n"
            "sys.intern(LABEL)\ndef count(value=LABEL):\n    return value.count('a')\n")
    _source(tmp_path, first,
            "import loaded_attestation_graph_second as sibling\n"
            "LABEL = 'alpha'\ndef value(arg=LABEL):\n    return sibling.count(arg)\n")
    sys.path.insert(0, str(tmp_path))
    try:
        attestation.begin(str(tmp_path))
        importlib.import_module(first)
        attestation.finish()
        source_result = attestation.attest()
        assert source_result["complete"] is True
        assert source_result["executed_module_count"] == 2
        _forget(first)
        _forget(second)

        def source_compile_would_be_a_test_failure(*args, **kwargs):
            raise AssertionError("valid pyc should avoid loader source compilation")

        monkeypatch.setattr(importlib.machinery.SourceFileLoader, "source_to_code",
                            source_compile_would_be_a_test_failure)
        importlib.invalidate_caches()
        attestation.begin(str(tmp_path))
        importlib.import_module(first)
        attestation.finish()
        pyc_result = attestation.attest()
        assert pyc_result["complete"] is True
        assert pyc_result["executed_module_count"] == 2
    finally:
        attestation.finish()
        _forget(first)
        _forget(second)
        sys.path.remove(str(tmp_path))