# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Settings persistence against owned temporary files with an isolated import."""

import contextlib
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def shared_state(tmp_path, monkeypatch):
    package_name = "isolated_shared_state_pkg"
    package = types.ModuleType(package_name)
    package.__path__ = [str(tmp_path)]
    config = types.ModuleType(f"{package_name}.config")
    config.COMPANY_NAME = "TestCompany"
    config.ADDIN_NAME = "TestAddin"
    lib = types.ModuleType(f"{package_name}.lib")
    lib.__path__ = []
    futil = types.ModuleType(f"{package_name}.lib.fusion360utils")
    adsk = types.ModuleType("adsk")
    adsk.core = types.ModuleType("adsk.core")
    modules = {
        package_name: package,
        config.__name__: config,
        lib.__name__: lib,
        futil.__name__: futil,
        "adsk": adsk,
        "adsk.core": adsk.core,
    }
    for name, value in modules.items():
        monkeypatch.setitem(sys.modules, name, value)
    source = Path(__file__).parents[2] / "shared_state.py"
    monkeypatch.setattr("os.path.expanduser", lambda value: str(tmp_path / "home"))
    spec = importlib.util.spec_from_file_location(f"{package_name}.shared_state", source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    settings_file = Path(module.SETTINGS_FILE)
    assert settings_file.is_relative_to(tmp_path)
    assert Path(module.settings_dir).is_relative_to(tmp_path)
    return module, settings_file


@pytest.mark.parametrize("caller", ["init", "save"])
def test_serialization_failure_preserves_prior_settings(shared_state, caller):
    module, settings_file = shared_state
    baseline = b'{"module": {"settings": {"value": 1}}}\r\n'
    settings_file.write_bytes(baseline)
    invalid = {"value": object()}

    with pytest.raises(TypeError):
        if caller == "init":
            module.load_settings_init("new", "New", invalid, None)
        else:
            module.save_settings("module", invalid)

    assert settings_file.read_bytes() == baseline
    assert list(settings_file.parent.glob(".FusionEssentials-*.tmp")) == []


def test_success_preserves_ascii_json_format_and_round_trips(shared_state):
    module, settings_file = shared_state
    settings_file.write_bytes(b'{"module": {"settings": {}}}')

    module.save_settings("module", {"label": "na\u00efve"})

    expected = '{\n    "module": {\n        "settings": {\n            "label": "na\\u00efve"\n        }\n    }\n}'
    assert settings_file.read_bytes() == expected.replace("\n", os.linesep).encode("ascii")
    assert module.load_settings("module") == {"label": "na\u00efve"}
    assert list(settings_file.parent.glob(".FusionEssentials-*.tmp")) == []


@pytest.mark.parametrize("stage", ["fdopen", "write", "flush", "fsync", "replace"])
def test_io_failure_preserves_prior_settings_and_removes_temp(shared_state, monkeypatch, stage):
    module, settings_file = shared_state
    baseline = b'{"module": {"settings": {"value": 1}}}'
    settings_file.write_bytes(baseline)
    failure = OSError(stage + " failed")
    real_fdopen = module.os.fdopen

    def fail(*args, **kwargs):
        raise failure

    @contextlib.contextmanager
    def failing_stream(fd, *args, **kwargs):
        with real_fdopen(fd, *args, **kwargs) as stream:
            def write(text):
                if stage == "write":
                    stream.write(text[:8])
                    raise failure
                return stream.write(text)

            def flush():
                stream.flush()
                if stage == "flush":
                    raise failure

            yield types.SimpleNamespace(write=write, flush=flush, fileno=stream.fileno)

    if stage in ("write", "flush"):
        monkeypatch.setattr(module.os, "fdopen", failing_stream)
    else:
        monkeypatch.setattr(module.os, stage, fail)

    with pytest.raises(OSError) as caught:
        module.save_settings("module", {"value": 2})

    assert caught.value is failure
    assert settings_file.read_bytes() == baseline
    assert list(settings_file.parent.glob(".FusionEssentials-*.tmp")) == []


def test_cleanup_failure_reports_residue_without_hiding_original_error(shared_state, monkeypatch):
    module, settings_file = shared_state
    baseline = b'{"module": {"settings": {"value": 1}}}'
    settings_file.write_bytes(baseline)
    failure = OSError("sync failed")

    def fail_sync(fd):
        raise failure

    def fail_cleanup(path):
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(module.os, "fsync", fail_sync)
    monkeypatch.setattr(module.os, "unlink", fail_cleanup)
    with pytest.raises(OSError) as caught:
        module.save_settings("module", {"value": 2})

    assert caught.value is failure
    assert settings_file.read_bytes() == baseline
    remaining = list(settings_file.parent.glob(".FusionEssentials-*.tmp"))
    assert len(remaining) == 1
    assert any(str(remaining[0]) in note and "cleanup denied" in note
               for note in caught.value.__notes__)


@pytest.mark.parametrize("caller", ["init", "save"])
def test_corrupt_json_refuses_writes_without_reset(shared_state, caller):
    module, settings_file = shared_state
    baseline = b"{broken"
    settings_file.write_bytes(baseline)

    with pytest.raises(json.JSONDecodeError):
        if caller == "init":
            module.load_settings_init("module", "Module", {"x": 1}, None)
        else:
            module.save_settings("module", {"x": 2})

    assert settings_file.read_bytes() == baseline
    assert list(settings_file.parent.glob(".FusionEssentials-*.tmp")) == []
