# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP tool: reload the Fusion-Essentials add-in. The reload is DEFERRED - a timer thread fires a
custom event on the main thread after this call returns - because this server is part of it."""

import os
import sys
import threading

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error

# Returns a status SENTENCE as its content text rather than _common.ok()'s json.dumps'd payload;
# the refusal path uses _common.error unchanged.
app = adsk.core.Application.get()

# Dedicated custom event for the deferred reload (separate from TaskManager's).
RELOAD_EVENT_ID = 'GTF_Fusion-Essentials.MCP.ReloadAddinEvent'

# Delay before the reload fires, giving the HTTP response time to flush.
_RELOAD_DELAY_SECONDS = 0.5

# Kept at module scope so the handler/event survive until used.
_reload_event = None
_reload_handler = None
_install_error = ""      # quoted in the refusal - without the event a reload never happens


def _addin_root_folder() -> str:
    """Absolute path to the add-in root folder (where the .manifest lives) - four levels up."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, '..', '..', '..'))


def _find_self_script():
    """The Script object for this add-in, by folder path then by name, or None."""
    scripts = app.scripts
    root = _addin_root_folder()
    try:
        scr = scripts.itemByPath(root)
        if scr:
            return scr
    except Exception:
        pass
    # Fallback: match by add-in name (folder basename).
    name = os.path.basename(root)
    try:
        matches = scripts.itemsByName(name)
        if matches:
            return matches[0]
    except Exception:
        pass
    return None


def _purge_addin_modules() -> int:
    """Delete every loaded module whose __file__ lives under this add-in's root from sys.modules, so
    the next Script.run() re-imports them FRESH from disk; returns the count purged."""
    root = _addin_root_folder()
    root_cmp = os.path.normcase(root)
    doomed = []
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        f = getattr(mod, '__file__', None)
        if not f:
            continue
        try:
            if os.path.normcase(os.path.abspath(f)).startswith(root_cmp + os.sep):
                doomed.append(name)
        except Exception:
            continue
    # This module purges itself too; the live frame runs to completion and run() re-imports it.
    for name in doomed:
        try:
            del sys.modules[name]
        except Exception:
            pass
    return len(doomed)


def _perform_reload():
    """The deferred stop -> purge -> run. Every outcome goes to the Fusion log: this runs after the
    HTTP response flushed and the MCP server is gone, so the log is the only channel left."""
    try:
        app.log('Fusion-Essentials MCP: performing deferred add-in reload')
        script = _find_self_script()
        if not script:
            app.log('Fusion-Essentials MCP reload: could not locate own Script object')
            return
        # A false stop() means run() below re-enters an add-in that never stopped.
        if not script.stop():
            app.log('Fusion-Essentials MCP reload: Script.stop() returned False - the add-in '
                    'did not stop, so the re-run below may load nothing')
        # The cache is busted BEFORE run(), or run() re-imports the stale modules and only
        # brand-new files appear.
        try:
            purged = _purge_addin_modules()
            app.log(f'Fusion-Essentials MCP reload: purged {purged} cached add-in module(s)')
        except Exception as e:
            app.log(f'Fusion-Essentials MCP reload: module purge failed (continuing): {e}')
        # run() now re-imports the add-in fresh from disk.
        if not script.run(False):
            app.log('Fusion-Essentials MCP reload: Script.run(False) returned False - the add-in '
                    'did NOT restart and the MCP server is down; start it from the Scripts and '
                    'Add-Ins dialog (Shift+S)')
    except Exception as e:
        app.log(f'Fusion-Essentials MCP reload failed: {e}')


class _ReloadEventHandler(adsk.core.CustomEventHandler):
    """Fires the deferred reload on the main thread, outside any MCP call. See _perform_reload."""

    def notify(self, args):
        _perform_reload()


def install_reload_event():
    """Register the reload custom event + handler. Called at server start."""
    global _reload_event, _reload_handler, _install_error
    # Cleared first, so a failed install cannot leave the previous run's event for handler() to fire.
    _reload_event, _reload_handler, _install_error = None, None, ''
    try:
        try:
            app.unregisterCustomEvent(RELOAD_EVENT_ID)
        except Exception:
            pass
        _reload_event = app.registerCustomEvent(RELOAD_EVENT_ID)
        if not _reload_event:
            _install_error = f'app.registerCustomEvent({RELOAD_EVENT_ID}) returned nothing'
            app.log(f'Fusion-Essentials MCP: {_install_error}')
            return
        _reload_handler = _ReloadEventHandler()
        _reload_event.add(_reload_handler)
    except Exception as e:
        _install_error = str(e)
        app.log(f'Fusion-Essentials MCP: failed to install reload event: {e}')


def uninstall_reload_event():
    """Remove the reload custom event + handler. Called at server stop."""
    global _reload_event, _reload_handler
    try:
        if _reload_event and _reload_handler:
            _reload_event.remove(_reload_handler)
        try:
            app.unregisterCustomEvent(RELOAD_EVENT_ID)
        except Exception:
            pass
    except Exception:
        pass
    finally:
        _reload_event = None
        _reload_handler = None


def handler() -> dict:
    """Schedule a deferred reload and return immediately (does NOT reload inline)."""
    # With no event registered, fireCustomEvent reaches nothing and the reload never happens.
    if _reload_event is None or _reload_handler is None:
        why = _install_error or 'the event is not registered on this server'
        return error("Reload NOT scheduled: the deferred-reload event is not installed (" + why
                     + "), so firing it would reach nothing and the add-in would keep running the "
                     "code already in memory. Reload it from Fusion's Scripts and Add-Ins dialog "
                     "(Shift+S) instead - stop the add-in, then run it.")

    def _fire():
        try:
            app.fireCustomEvent(RELOAD_EVENT_ID)
        except Exception as e:
            app.log(f'Fusion-Essentials MCP: failed to fire reload event: {e}')

    # Delayed so this handler returns and the HTTP response flushes before the teardown.
    threading.Timer(_RELOAD_DELAY_SECONDS, _fire).start()

    return {
    "content": [{
            "type": "text",
        "text": (
                "Reload scheduled. Wait ~3 seconds, then reconnect or refresh this server's tool "
                "list before further calls. Call sys_capability_map and compare its "
                "schema_fingerprint with the value read before reload. That identifies the server "
                "schema, but only exercising a changed input proves the client consumed it. Do not "
                "poll /health from a shell. A stale client schema can comma-mangle a json-array "
                "argument whose property it lacks while a scalar still passes."
            ),
        }],
    "isError": False,
    "next": "sys_capability_map",
    }


TOOL_DESCRIPTION = (
    "Reload the add-in to pick up code changes. This restarts the MCP server: re-fetch the tool "
    "list before further calls."
)

tool = Tool.create_simple(name="sys_reload_addin", description=TOOL_DESCRIPTION).strict_schema()

# Runs on the main thread only to start a timer; the reload itself fires on the custom event.
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(kind="external",
                              evidence_receipt="tests/live/VERIFIED_TOOLS.md#sys_reload_addin"))


def register_tool():
    """Register this tool. Called from entry.py when assembling the tool set."""
    register(item)
