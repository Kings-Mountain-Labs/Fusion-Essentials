# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Tests for what entry.start() hands the server, and for its teardown guard: a start that does not
leave a server running must leave no started TaskManager behind.

TaskManager.start() registers a Fusion custom event and arms the pending-task table; entry.start()
calls it FIRST, so every exit that does not end with a running server has to stop it again. The
handled failure paths did that explicitly, but the blanket `except Exception` that keeps a broken
MCP module from breaking the rest of the add-in skipped it - so any raise inside start() (a tool
module blowing up during registration, a bind error) left the event registered with nothing serving
it until the next add-in stop().

entry.py also OWNS the static MCP resource catalog: it builds it from the shipped guidance package
and hands it to start_server, so the transport never reaches into product content. A document that
does not load must leave that catalog empty - the server then advertises no resource capability at
all - which is checked here against the real packaged document.

entry.py cannot be imported here: its module body calls shared_state.load_settings_init(), which
reads and rewrites the real user settings file, and it needs the live add-in package host. So the
function under test is compiled OUT of entry.py's AST and executed against fakes - the actual
shipped source, with no module import side effects.
"""

import ast
import os
import sys

import pytest

from conftest import COMMANDS_DIR, load_mcp_server

if COMMANDS_DIR not in sys.path:                   # the flat package root the add-in code lives in
    sys.path.insert(0, COMMANDS_DIR)
from mcpServer.guidance import loader as guidance_loader        # noqa: E402
from mcpServer.guidance import resources as guidance_resources  # noqa: E402

ENTRY_PATH = os.path.join(COMMANDS_DIR, "mcpServer", "entry.py")

# The package entry.py's own relative imports resolve against (`from .guidance import resources`),
# which is what lets one function be executed out of its AST and still reach the shipped package.
ENTRY_PACKAGE = "mcpServer"
_ENTRY_START_ERROR_LEVEL = object()


class _FakeTaskManager:
    """Counts start/stop. It stands in for the add-in's own TaskManager, not for any adsk type, so
    conftest's shared adsk fakes have nothing to reuse here."""

    def __init__(self, start_result=True):
        self.started = 0
        self.stopped = 0
        self.start_result = start_result

    def start(self):
        self.started += 1
        return self.start_result

    def stop(self):
        self.stopped += 1
        return True

    @property
    def running(self):
        """True while a start has not been matched by a stop - the leak this file guards."""
        return self.started > self.stopped


class _FakeServerModule:
    def __init__(self, result, real):
        self.START_OK = real.START_OK
        self.START_PORT_IN_USE = real.START_PORT_IN_USE
        self._result = result
        self.seen = {}          # what start() actually handed the transport

    def start_server(self, host, port, items=None, resources=None, attestation=None):
        self.seen = {"host": host, "port": port, "items": items, "resources": resources,
                     "attestation": attestation}
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _load_function(name, namespace):
    """One entry.py function, compiled from its own source into `namespace`."""
    with open(ENTRY_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=ENTRY_PATH)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(fns) == 1, f"entry.py must define exactly one {name}(); found {len(fns)}"
    module = ast.Module(body=fns, type_ignores=[])
    exec(compile(module, ENTRY_PATH, "exec"), namespace)
    return namespace[name]


@pytest.fixture
def real_server_module():
    return load_mcp_server()


def _run_start(real, *, result, start_result=True, collect_raises=None, ownership_raises=None,
               catalog=("resource",)):
    """Execute entry.start() against fakes; return the fake TaskManager and the collected log."""
    tm = _FakeTaskManager(start_result=start_result)
    log = []

    def _collect_items():
        if collect_raises is not None:
            raise collect_raises
        return ["item"]

    def _start_ownership_check():
        if ownership_raises is not None:
            raise ownership_raises

    server_module = _FakeServerModule(result, real)
    fake_levels = type("LogLevels", (), {"ErrorLogLevel": _ENTRY_START_ERROR_LEVEL})
    fake_adsk = type("Adsk", (), {"core": type("Core", (), {"LogLevels": fake_levels})})

    def _log(message, level=None):
        log.append(("log", message))
        if level is not None:
            log.append(("log_level", level))

    capture = type("Capture", (), {
        "resume": staticmethod(lambda: log.append(("capture", "resume")) or True),
        "finish": staticmethod(lambda: log.append(("capture", "finish"))),
        "attest": staticmethod(lambda: {"complete": True})})()
    ns = {
        "TaskManager": tm,
        "mcp_server": server_module,
        "_collect_items": _collect_items,
        "_resource_catalog": lambda: list(catalog),
        "loaded_attestation": capture,
        "_start_ownership_check": _start_ownership_check,
        "_warn_port_conflict": lambda reason: log.append(("warn", reason)),
        "adsk": fake_adsk,
        "futil": type("F", (), {"log": staticmethod(_log),
                                "handle_error": staticmethod(
                                    lambda m: log.append(("error", m)))})(),
        "CMD_NAME": "MCP Server",
        "HOST": "127.0.0.1",
        "PORT": 27182,
        "_http_server": None,
        "_server_thread": None,
        "_mcp": None,
    }
    _load_function("start", ns)()
    return tm, log, server_module


def _ok_result(real):
    return {"status": real.START_OK, "mcp": object(), "http_server": object(), "thread": object()}


class TestStartStopsTheTaskManagerWhenNoServerRuns:
    def test_a_raise_inside_start_stops_the_task_manager(self, real_server_module):
        tm, log, _ = _run_start(real_server_module, result=_ok_result(real_server_module),
                                collect_raises=RuntimeError("a tool module blew up"))
        assert tm.started == 1
        assert tm.stopped == 1, "the blanket except swallowed the failure and leaked the TaskManager"
        assert tm.running is False
        assert [value for kind, value in log if kind == "capture"] == ["resume", "finish"]
        assert ("error", "MCP Server.start") in log, "the failure must still be reported"

    def test_a_raise_from_start_server_stops_the_task_manager(self, real_server_module):
        tm, _log, _srv = _run_start(real_server_module, result=OSError("bind failed"))
        assert (tm.started, tm.stopped) == (1, 1)

    def test_port_in_use_stops_the_task_manager_exactly_once(self, real_server_module):
        tm, log, _ = _run_start(real_server_module,
                                result={"status": real_server_module.START_PORT_IN_USE})
        assert tm.stopped == 1, "the port-conflict path must stop the TaskManager, and only once"
        assert any(kind == "warn" for kind, _ in log), "the user must still be warned"

    def test_failed_task_manager_start_stops_without_collecting_or_binding(self, real_server_module):
        tm, log, server = _run_start(
            real_server_module, result=_ok_result(real_server_module), start_result=False,
            collect_raises=RuntimeError("collection must not run after TaskManager start failure"))
        assert (tm.started, tm.stopped) == (1, 1) and tm.running is False
        assert server.seen == {}
        assert not any(kind == "capture" for kind, _ in log)
        assert ("error", "MCP Server.start") not in log
        assert any("phase=task_manager_start" in message for kind, message in log if kind == "log")
        assert [level for kind, level in log if kind == "log_level"] == [
            _ENTRY_START_ERROR_LEVEL]

    def test_an_unknown_failure_status_stops_the_task_manager(self, real_server_module):
        tm, _log, _srv = _run_start(real_server_module, result={"status": "something-else",
                                                                "message": "no port"})
        assert (tm.started, tm.stopped) == (1, 1)

    def test_a_successful_start_leaves_the_task_manager_running(self, real_server_module):
        tm, _log, _srv = _run_start(real_server_module, result=_ok_result(real_server_module))
        assert tm.stopped == 0, "the running server needs the TaskManager to marshal main-thread work"
        assert tm.running is True

    def test_a_raise_after_the_server_is_up_leaves_the_task_manager_running(self, real_server_module):
        # the boundary: the ownership probe raises AFTER the server is serving, so the TaskManager
        # is still in use - stopping it here would break the live server the guard is protecting.
        tm, log, _ = _run_start(real_server_module, result=_ok_result(real_server_module),
                                ownership_raises=RuntimeError("probe thread refused to spawn"))
        assert tm.stopped == 0
        assert ("error", "MCP Server.start") in log


class TestStartHandsTheServerItsResourceCatalog:
    """The transport publishes what entry.py built for it; it opens no product file itself."""

    def test_the_catalog_start_built_reaches_start_server(self, real_server_module):
        _tm, _log, server = _run_start(real_server_module, result=_ok_result(real_server_module),
                                       catalog=[{"uri": "u", "text": "t"}])
        assert server.seen["resources"] == [{"uri": "u", "text": "t"}]
        assert server.seen["items"] == ["item"]
        assert callable(server.seen["attestation"])
        assert server.seen["attestation"]() == {"complete": True}

    def test_an_empty_catalog_is_still_passed_explicitly(self, real_server_module):
        # the server decides on the VALUE it is handed (an empty list advertises nothing), so an
        # empty catalog must still arrive rather than being dropped from the call.
        _tm, _log, server = _run_start(real_server_module, result=_ok_result(real_server_module),
                                       catalog=[])
        assert server.seen["resources"] == []


class TestTheResourceCatalogEntryBuilds:
    """entry._resource_catalog() itself, run out of entry.py's AST against the REAL packaged
    guidance - the same import the add-in performs at startup."""

    def _catalog(self, monkeypatch, guidance_path=None):
        log = []
        ns = {
            "__package__": ENTRY_PACKAGE,
            "futil": type("F", (), {"log": staticmethod(lambda m: log.append(m))})(),
            "CMD_NAME": "MCP Server",
        }
        if guidance_path is not None:
            monkeypatch.setattr(guidance_loader, "GUIDANCE_PATH", guidance_path)
        return _load_function("_resource_catalog", ns)(), log

    def test_it_publishes_the_packaged_guidance(self, monkeypatch):
        catalog, log = self._catalog(monkeypatch)
        whole = guidance_resources.uri_for("parametric-cad-design")
        assert [r["uri"] for r in catalog] == [whole] + [
            guidance_resources.section_uri_for("parametric-cad-design", s)
            for s in guidance_loader.SECTION_IDS if s != guidance_loader.KERNEL]
        assert catalog[0]["text"].startswith("# ")
        assert log == []

    def test_a_document_that_does_not_load_publishes_nothing_and_says_why(self, monkeypatch,
                                                                          tmp_path):
        catalog, log = self._catalog(monkeypatch, guidance_path=str(tmp_path / "gone.json"))
        assert catalog == [], "a missing document must publish no address at all"
        assert any("gone.json" in message for message in log), log
