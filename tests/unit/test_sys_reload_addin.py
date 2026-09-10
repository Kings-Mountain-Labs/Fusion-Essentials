"""Unit tests for ``sys_reload_addin._purge_addin_modules`` -- the sys.modules cache-bust that
lets a reload re-import EDITED files instead of handing back the stale cached objects. The only
pure logic here is which module names it purges vs keeps, by comparing each module's __file__
against the add-in's root folder; that decision is exercised directly against the real
``sys.modules`` dict via ``monkeypatch.setitem`` (auto-restored, so the test can't leak a fake
module entry into the rest of the suite).
"""

import os
import types

import pytest

from conftest import FakeApplication, load_tool

ra = load_tool("sys_reload_addin")

_FAKE_ROOT = os.path.abspath(os.path.join(os.sep, "FakeAddinRoot"))


def _module_at(path):
    m = types.ModuleType("fake")
    m.__file__ = path
    return m


def _install_root(monkeypatch, root=_FAKE_ROOT):
    monkeypatch.setattr(ra, "_addin_root_folder", lambda: root)


def _install_event(monkeypatch):
    """The state a successful install_reload_event() leaves: an event object with the handler on it.
    Without it the tool refuses to schedule, so every schedule test states the precondition."""
    monkeypatch.setattr(ra, "_reload_event", types.SimpleNamespace(add=lambda h: True))
    monkeypatch.setattr(ra, "_reload_handler", object())
    monkeypatch.setattr(ra, "_install_error", "")


class _LoggingApp(FakeApplication):
    """The session with a capturing log() - the channel a deferred reload reports through - and any
    further members a test hands it (registerCustomEvent)."""

    def __init__(self, lines, **members):
        FakeApplication.__init__(self)
        self._lines = lines
        for name, value in members.items():
            setattr(self, name, value)

    def log(self, text):
        self._lines.append(text)


def _fake_app(monkeypatch, **members):
    """ra.app with a capturing log(); returns the list of logged lines."""
    lines = []
    monkeypatch.setattr(ra, "app", _LoggingApp(lines, **members))
    return lines


class _Script:
    """The Script object a reload drives: stop()/run() answer whether they worked, which is the
    whole question the deferred worker has to report."""

    def __init__(self, stop=True, run=True):
        self._stop, self._run = stop, run
        self.calls = []

    def stop(self):
        self.calls.append("stop")
        return self._stop

    def run(self, arg):
        self.calls.append(("run", arg))
        return self._run


class TestPurgeAddinModules:
    def test_purges_a_module_whose_file_is_under_the_addin_root(self, monkeypatch):
        _install_root(monkeypatch)
        name = "fake_addin_tool_under_root"
        monkeypatch.setitem(ra.sys.modules, name,
                            _module_at(os.path.join(_FAKE_ROOT, "commands", "mcpServer", "tools", "foo.py")))
        ra._purge_addin_modules()
        assert name not in ra.sys.modules

    def test_keeps_a_module_whose_file_is_outside_the_addin_root(self, monkeypatch):
        _install_root(monkeypatch)
        name = "fake_unrelated_module"
        outside = os.path.abspath(os.path.join(os.sep, "SomewhereElse", "bar.py"))
        monkeypatch.setitem(ra.sys.modules, name, _module_at(outside))
        ra._purge_addin_modules()
        assert name in ra.sys.modules

    def test_keeps_a_module_with_no_file_attribute(self, monkeypatch):
        # built-ins / namespace packages have no __file__ - must never be touched.
        _install_root(monkeypatch)
        name = "fake_builtin_like_module"
        monkeypatch.setitem(ra.sys.modules, name, types.ModuleType("fake"))
        ra._purge_addin_modules()
        assert name in ra.sys.modules

    def test_skips_none_entries_without_raising(self, monkeypatch):
        # sys.modules can hold None as a placeholder for a failed/blocked import.
        _install_root(monkeypatch)
        name = "fake_none_placeholder"
        monkeypatch.setitem(ra.sys.modules, name, None)
        ra._purge_addin_modules()          # must not raise
        assert ra.sys.modules[name] is None

    def test_return_value_counts_only_the_purged_modules(self, monkeypatch):
        _install_root(monkeypatch)
        under_name = "fake_addin_tool_under_root_2"
        outside_name = "fake_unrelated_module_2"
        monkeypatch.setitem(ra.sys.modules, under_name,
                            _module_at(os.path.join(_FAKE_ROOT, "tools", "bar.py")))
        monkeypatch.setitem(ra.sys.modules, outside_name,
                            _module_at(os.path.abspath(os.path.join(os.sep, "Other", "baz.py"))))
        before = ra._purge_addin_modules()
        assert under_name not in ra.sys.modules
        assert outside_name in ra.sys.modules
        assert before >= 1


class TestStaleCachedSchemaWarning:
    """One warning sentence about a stale CLIENT schema corrupting json-array arguments
    (scalars still pass), delivered on the reload response - the moment the caller decides what to
    do while the server restarts, and live-proven."""

    def test_the_reload_response_warns_about_a_stale_client_schema(self, monkeypatch):
        _install_event(monkeypatch)
        monkeypatch.setattr(ra.threading, "Timer",
                            lambda *a, **k: types.SimpleNamespace(start=lambda: None))
        text = ra.handler()["content"][0]["text"]
        assert "json-array" in text
        assert "comma-mangle" in text
        assert "reconnect" in text.lower()
        # the claim distinguishes scalar (safe) from array (unsafe) - not a blanket "reconnect always".
        assert "scalar" in text.lower()


class TestReloadResponseTeachesReconnect:
    """The reload response tells the caller how to refresh its schema and observe the new server."""

    def test_note_teaches_schema_refresh_and_not_health_polling(self, monkeypatch):
        _install_event(monkeypatch)
        monkeypatch.setattr(ra.threading, "Timer",
                            lambda *a, **k: types.SimpleNamespace(start=lambda: None))
        res = ra.handler()
        assert res["isError"] is False
        text = res["content"][0]["text"]
        assert "reconnect or refresh" in text
        assert "sys_capability_map" in text and "schema_fingerprint" in text
        assert "only exercising a changed input proves the client consumed it" in text
        assert "reconnects automatically" not in text
        assert "Do not poll /health" in text
        # machine-readable pointer for clients that read fields, not prose
        assert res["next"] == "sys_capability_map"


class TestScheduleRefusedWithoutTheEvent:
    """The timer fires a CUSTOM EVENT. With no event registered, fireCustomEvent reaches nothing at
    all: the reload never happens, and "Reload scheduled." sends the caller on to test code that was
    never loaded."""

    def _no_timer(self, monkeypatch):
        armed = []
        monkeypatch.setattr(ra.threading, "Timer",
                            lambda *a, **k: armed.append(a) or types.SimpleNamespace(
                                start=lambda: None))
        return armed

    def test_a_missing_event_refuses_and_quotes_the_install_failure(self, monkeypatch):
        armed = self._no_timer(monkeypatch)
        monkeypatch.setattr(ra, "_reload_event", None)
        monkeypatch.setattr(ra, "_reload_handler", None)
        monkeypatch.setattr(ra, "_install_error", "3 : the event name is already registered")
        res = ra.handler()
        assert res["isError"] is True
        assert "3 : the event name is already registered" in res["message"]
        assert "Scripts and Add-Ins" in res["message"]
        assert armed == []              # nothing was scheduled, so nothing may claim it was

    def test_a_missing_handler_refuses_too(self, monkeypatch):
        # the event exists but carries no handler: firing it still reaches nothing
        armed = self._no_timer(monkeypatch)
        monkeypatch.setattr(ra, "_reload_event", types.SimpleNamespace(add=lambda h: True))
        monkeypatch.setattr(ra, "_reload_handler", None)
        monkeypatch.setattr(ra, "_install_error", "")
        res = ra.handler()
        assert res["isError"] is True
        assert "not registered on this server" in res["message"]
        assert armed == []

    def test_the_refusal_is_the_shared_error_envelope(self, monkeypatch):
        # this tool builds its SUCCESS result by hand (a status sentence, not a json payload), and
        # a hand-built refusal beside it is how one of the three keys drifts. The refusal is
        # _common.error's - text content, isError, and a message MIRRORING that same text.
        self._no_timer(monkeypatch)
        monkeypatch.setattr(ra, "_reload_event", None)
        monkeypatch.setattr(ra, "_reload_handler", None)
        monkeypatch.setattr(ra, "_install_error", "3 : the event name is already registered")
        res = ra.handler()
        assert res == ra.error(res["message"])
        assert res["content"][0]["text"] == res["message"]

    def test_an_installed_event_schedules(self, monkeypatch):
        armed = self._no_timer(monkeypatch)
        _install_event(monkeypatch)
        res = ra.handler()
        assert res["isError"] is False
        assert len(armed) == 1

    def test_a_failed_install_is_recorded_and_the_next_reload_quotes_it(self, monkeypatch):
        # a stale event object from an earlier install must not survive a failed one, or the
        # refusal never fires and the schedule goes to an event this session never registered
        monkeypatch.setattr(ra, "_reload_event", types.SimpleNamespace(add=lambda h: True))
        monkeypatch.setattr(ra, "_reload_handler", object())
        monkeypatch.setattr(ra, "_install_error", "")
        self._no_timer(monkeypatch)

        def _boom(_event_id):
            raise RuntimeError("3 : the event name is already registered")

        _fake_app(monkeypatch, registerCustomEvent=_boom,
                  unregisterCustomEvent=lambda _event_id: True)
        ra.install_reload_event()
        assert "already registered" in ra._install_error
        assert ra._reload_event is None and ra._reload_handler is None
        assert "already registered" in ra.handler()["message"]

    def test_an_event_that_registers_as_nothing_is_recorded(self, monkeypatch):
        monkeypatch.setattr(ra, "_reload_event", None)
        monkeypatch.setattr(ra, "_reload_handler", None)
        monkeypatch.setattr(ra, "_install_error", "")
        logged = _fake_app(monkeypatch, registerCustomEvent=lambda _event_id: None,
                           unregisterCustomEvent=lambda _event_id: True)
        ra.install_reload_event()
        assert "returned nothing" in ra._install_error
        assert any("returned nothing" in line for line in logged)


class TestDeferredWorkerReportsWhatHappened:
    """The worker runs after the HTTP response, with the server it tears down already gone - the
    Fusion log is the only channel left, so a stop()/run() that answered False lands there instead
    of being discarded."""

    def test_a_false_stop_is_logged(self, monkeypatch):
        script = _Script(stop=False)
        monkeypatch.setattr(ra, "_find_self_script", lambda: script)
        monkeypatch.setattr(ra, "_purge_addin_modules", lambda: 3)
        logged = _fake_app(monkeypatch)
        ra._perform_reload()
        assert any("Script.stop() returned False" in line for line in logged)
        assert ("run", False) in script.calls        # the reload still attempts the re-run

    def test_a_false_run_is_logged_with_the_recovery(self, monkeypatch):
        script = _Script(run=False)
        monkeypatch.setattr(ra, "_find_self_script", lambda: script)
        monkeypatch.setattr(ra, "_purge_addin_modules", lambda: 3)
        logged = _fake_app(monkeypatch)
        ra._perform_reload()
        blob = "\n".join(logged)
        assert "Script.run(False) returned False" in blob
        assert "Scripts and Add-Ins" in blob

    def test_a_clean_reload_logs_no_failure(self, monkeypatch):
        script = _Script()
        monkeypatch.setattr(ra, "_find_self_script", lambda: script)
        monkeypatch.setattr(ra, "_purge_addin_modules", lambda: 3)
        logged = _fake_app(monkeypatch)
        ra._perform_reload()
        assert script.calls == ["stop", ("run", False)]
        assert not any("returned False" in line for line in logged)
        assert any("purged 3" in line for line in logged)

    def test_the_module_cache_is_busted_between_stop_and_run(self, monkeypatch):
        # run() before the purge re-imports the STALE modules, which is the whole defect this tool
        # exists to avoid - the order is the contract.
        order = []
        script = _Script()
        monkeypatch.setattr(script, "stop", lambda: order.append("stop") or True)
        monkeypatch.setattr(script, "run", lambda arg: order.append("run") or True)
        monkeypatch.setattr(ra, "_find_self_script", lambda: script)
        monkeypatch.setattr(ra, "_purge_addin_modules", lambda: order.append("purge") or 1)
        _fake_app(monkeypatch)
        ra._perform_reload()
        assert order == ["stop", "purge", "run"]

    def test_a_missing_script_object_stops_before_touching_anything(self, monkeypatch):
        monkeypatch.setattr(ra, "_find_self_script", lambda: None)
        monkeypatch.setattr(ra, "_purge_addin_modules",
                            lambda: pytest.fail("purged with no script to re-run"))
        logged = _fake_app(monkeypatch)
        ra._perform_reload()
        assert any("could not locate own Script object" in line for line in logged)
