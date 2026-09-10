# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP Server command module for Fusion-Essentials.

Follows the Fusion-Essentials command convention (module-level CMD_ID / CMD_NAME
plus start()/stop()) so it participates in the settings-driven enablement loop in
commands/settings/entry.py. The enable checkbox for this module defaults to False
(see commands/__init__.py), so start() only runs when the user opts in and reloads.

start() hosts a local MCP server on 127.0.0.1:27182 path /mcp -- the same
well-known endpoint Fusion's built-in MCP server uses -- but only when that
built-in server is OFF (otherwise the port is taken and we report it). 27182 is
Fusion's preferred default port; whichever server binds first wins, and if ours
loses we detect it and guide the user (see _start_ownership_check / start()).
"""

import importlib
import pkgutil

import adsk.core

from ...lib import fusion360utils as futil
from ...lib import loaded_attestation
from ... import config
from ... import shared_state
from .server import mcp_server
from .server.task_manager import TaskManager
from .mcp_primitives import registry
from .mcp_primitives import GATEABLE_FAMILIES, GATED_TOOLS, family_of, unregister

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_MCP_Server'
CMD_NAME = 'MCP Server'
CMD_Description = (
    'Host a local Model Context Protocol (MCP) server so AI agents (e.g. Claude) '
    'can interact with your Fusion session. Off by default; loopback only.'
)

# Loopback only, on Fusion's well-known MCP port/path so clients configured for the
# standard Fusion MCP endpoint reach us unchanged.
HOST = '127.0.0.1'
PORT = 27182

# This module's own settings group (separate from the FEATURE_ENABLEMENT checkbox
# that gates the whole module). Gives the MCP server its own Settings tab where the
# high-risk execute_api_script tool can be toggled independently.
SETTINGS_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_MCP'
_ALLOW_EXECUTE_KEY = 'allow_execute_api_script'

# Tool modules that are NOT auto-registered by the pkgutil sweep in _collect_items(): each is
# registered explicitly, only when the user opts in, elsewhere in this file. Derived from
# mcp_primitives.GATED_TOOLS (the single source of truth also read by sys_capability_map) rather
# than a second hand-typed set here.
_GATED_TOOL_MODULES = frozenset(GATED_TOOLS)

DEFAULT_SETTINGS = {
    _ALLOW_EXECUTE_KEY: {
        "type": "checkbox",
        # High-risk: lets a connected AI run arbitrary Python in the live session.
        # The "security risk" label IS the consent signal (no separate dialog). Sourced from
        # GATED_TOOLS so sys_capability_map's gated-tool report can never quote a stale label.
        "label": GATED_TOOLS["sys_execute_script"],
        "default": False,
    },
    # One checkbox per GATEABLE_FAMILIES member, default True so an existing user's tool surface is
    # unchanged until they opt out. shared_state.load_settings_init merge-adds these keys for users
    # who already have a settings file (see shared_state.merge_settings), so upgrading is silent.
    "family_enabled_appearance": {
        "type": "checkbox",
        "label": "Enable appearance MCP tools",
        "default": True,
    },
    "family_enabled_cam": {
        "type": "checkbox",
        "label": "Enable cam MCP tools",
        "default": True,
    },
    "family_enabled_data": {
        "type": "checkbox",
        "label": "Enable data MCP tools",
        "default": True,
    },
    "family_enabled_drawing": {
        "type": "checkbox",
        "label": "Enable drawing MCP tools",
        "default": True,
    },
    "family_enabled_mesh": {
        "type": "checkbox",
        "label": "Enable mesh MCP tools",
        "default": True,
    },
    "family_enabled_save": {
        "type": "checkbox",
        "label": "Enable save MCP tools",
        "default": True,
    },
    "family_enabled_surface": {
        "type": "checkbox",
        "label": "Enable surface MCP tools",
        "default": True,
    },
}

# Register this module's settings group so it appears as a Settings tab.
shared_state.load_settings_init(SETTINGS_ID, 'MCP Server', DEFAULT_SETTINGS, None)


def _execute_api_script_allowed() -> bool:
    """Read the gating setting for the arbitrary-script-execution tool."""
    try:
        settings = shared_state.load_settings(SETTINGS_ID)
        return bool(settings.get(_ALLOW_EXECUTE_KEY, {}).get("default", False))
    except Exception:
        return False


def _disabled_families() -> set:
    """Read the family_enabled_<fam> checkboxes: the set of GATEABLE_FAMILIES the user turned OFF.

    Same defensive read style as _execute_api_script_allowed(): a missing settings file, a missing
    key, or any read error all mean ENABLED - a family is only disabled by an explicit False, never
    by a settings read failure.
    """
    disabled = set()
    try:
        settings = shared_state.load_settings(SETTINGS_ID)
    except Exception:
        return disabled
    for fam in GATEABLE_FAMILIES:
        try:
            enabled = bool(settings.get(f"family_enabled_{fam}", {}).get("default", True))
        except Exception:
            enabled = True
        if not enabled:
            disabled.add(fam)
    return disabled

# Module-level handles to the running server, torn down in stop().
_http_server = None
_server_thread = None
_mcp = None


def _collect_items():
    """Reset the registry and AUTO-DISCOVER every tool, registering each freshly.

    Tools live one-per-module under ``tools/`` and expose a ``register_tool()`` (a few self-register on
    import; ``register_tool()`` is then a harmless no-op). Rather than hand-maintain a parallel list of
    ~64 ``tools.X.register_tool()`` calls here AND a parallel import list in ``tools/__init__.py`` - two
    registries that drift whenever a tool is added - we sweep the package with ``pkgutil`` and call each
    module's ``register_tool()``. The registry's name-collision guard makes a double-register loud, and
    a module without ``register_tool()`` (a ``_``-prefixed helper) is skipped.

    Two cases stay explicit: the GATED arbitrary-script tool registers only when the user opts in, and
    ``sys_reload_addin`` also installs its dedicated reload custom event (a side-effect beyond
    registration).
    """
    registry.reset_registry()
    from . import tools  # the package whose modules we sweep

    registered = []
    for mod_info in pkgutil.iter_modules(tools.__path__):
        name = mod_info.name
        if name.startswith('_') or name in _GATED_TOOL_MODULES:
            continue
        try:
            mod = importlib.import_module(f'{tools.__name__}.{name}')
        except Exception as e:
            futil.log(f'{CMD_NAME}: tool module {name!r} failed to import: {e}')
            continue
        reg = getattr(mod, 'register_tool', None)
        if callable(reg):
            try:
                reg()
                registered.append(name)
            except Exception as e:
                futil.log(f'{CMD_NAME}: {name}.register_tool() failed: {e}')

    # reload_addin is registered by the sweep above; it ALSO needs its dedicated deferred-reload custom
    # event installed (a side-effect beyond registration). Import it explicitly (don't rely on it being
    # a bound attribute of the tools package) and install the event.
    try:
        from .tools import sys_reload_addin
        sys_reload_addin.install_reload_event()
    except Exception as e:
        futil.log(f'{CMD_NAME}: sys_reload_addin.install_reload_event() failed: {e}')

    # The high-risk arbitrary-script tool is gated and SKIPPED by the sweep - import it explicitly and
    # register it ONLY when the user has opted in.
    if _execute_api_script_allowed():
        try:
            from .tools import sys_execute_script
            sys_execute_script.register_tool()
            futil.log(f'{CMD_NAME}: execute_api_script ENABLED (user opted in)')
        except Exception as e:
            futil.log(f'{CMD_NAME}: sys_execute_script.register_tool() failed: {e}')

    # FAMILY GATING: purge by TOOL NAME, after the sweep - a module's filename prefix does not always
    # match its registered tools' families (mesh_export.py registers save_as_mesh, family "save"), so
    # skipping modules by filename would miss that. Walk the now-fully-populated registry instead and
    # drop anything whose family the user disabled.
    disabled = _disabled_families()
    if disabled:
        purged = 0
        for item in list(registry.get_tools()):
            if family_of(item.get_name()) in disabled:
                if unregister(item.get_name()):
                    purged += 1
        futil.log(f'{CMD_NAME}: disabled families ({", ".join(sorted(disabled))}) - purged {purged} tool(s)')

    futil.log(f'{CMD_NAME}: registered {len(registered)} tool modules (auto-discovered)')
    return registry.get_tools()


def _resource_catalog():
    """The static MCP Resources this server publishes: the packaged guidance, rendered whole.

    Built HERE and handed to the server, so the transport never reaches into product content. A
    document that does not load leaves the catalog EMPTY and says why in the log - the server then
    advertises no resources capability at all, rather than an address that answers with an error.
    """
    try:
        from .guidance import resources
        return resources.catalog()
    except Exception as e:
        futil.log(f'{CMD_NAME}: no MCP resource published ({e})')
        return []


def start():
    """Called when the module is enabled and the add-in starts."""
    global _http_server, _server_thread, _mcp
    # The TaskManager is started first and owns a registered custom event plus the pending-task
    # table, so every path that leaves without a running server must stop it again. One guard in
    # finally covers all of them - including an unexpected raise, which the blanket except below
    # otherwise reports and swallows, leaking the started TaskManager until the next add-in stop().
    server_running = False
    try:
        if TaskManager.start() is not True:
            futil.log(f'{CMD_NAME}: phase=task_manager_start server not started',
                      adsk.core.LogLevels.ErrorLogLevel)
            return

        loaded_attestation.resume()
        try:
            items = _collect_items()
            resources = _resource_catalog()
        finally:
            loaded_attestation.finish()
        result = mcp_server.start_server(HOST, PORT, items=items, resources=resources,
                                         attestation=loaded_attestation.attest)
        status = result.get("status")

        if status == mcp_server.START_OK:
            _mcp = result["mcp"]
            _http_server = result["http_server"]
            _server_thread = result["thread"]
            server_running = True
            futil.log(
                f'{CMD_NAME}: running on http://{HOST}:{PORT}/mcp '
                f'({len(items)} item(s) registered)'
            )

            # Layer-2 self-check: confirm WE are the server answering on the port.
            # If a foreign server replies, Autodesk's built-in server won the race
            # for 27182 and ours is effectively shadowed -> guide the user to fix it.
            #
            # The probe is a blocking HTTP GET, so it must NOT run here on the main
            # (UI) thread or it would stall Fusion startup. Run it on a short-lived
            # background thread; if it finds a foreign server, marshal the user-facing
            # warning back to the main thread via TaskManager (UI calls must be on it).
            _start_ownership_check()

        elif status == mcp_server.START_PORT_IN_USE:
            # Autodesk's built-in MCP server already holds the port; our bind failed.
            _warn_port_conflict(
                f'Fusion-Essentials MCP server could not start: port {PORT} is already in use.'
            )
        else:
            # Some other startup failure; details already logged.
            futil.log(f'{CMD_NAME}: failed to start ({result.get("message", "unknown error")})')
    except Exception:
        # A failure here must never break the rest of the add-in.
        futil.handle_error(f'{CMD_NAME}.start')
    finally:
        if not server_running:
            TaskManager.stop()


def _start_ownership_check():
    """Probe the port on a background thread; warn (on the main thread) if foreign.

    Runs off the UI thread so the blocking HTTP probe never stalls Fusion startup.
    If a different MCP server answers, the warning touches the UI, so it is posted
    back to the main thread via TaskManager rather than shown from the worker thread.
    """
    import threading

    def _probe():
        try:
            ownership = mcp_server.verify_ownership(HOST, PORT)
        except Exception:
            return
        if ownership != "foreign":
            # "ours" -> all good; "unreachable" -> inconclusive, leave server running.
            return
        futil.log(f'{CMD_NAME}: port {PORT} answered by a different MCP server')

        def _warn_on_main(_data):
            _warn_port_conflict(
                f'Another MCP server is already answering on port {PORT} '
                "(most likely Fusion's built-in MCP server)."
            )

        if TaskManager.is_running():
            TaskManager.post(command="mcp_port_conflict_warning", callback=_warn_on_main, data={})
        else:
            # No way to safely reach the UI thread; at least it's logged above.
            futil.log(f'{CMD_NAME}: could not show port-conflict warning (TaskManager down)')

    threading.Thread(target=_probe, daemon=True, name='FE-MCP-OwnershipCheck').start()


def _warn_port_conflict(reason: str):
    """Tell the user Fusion's built-in MCP server is conflicting, and open Preferences.

    We cannot toggle Autodesk's built-in MCP setting via the API (no such API
    exists), so we explain what to do and open the Preferences dialog so the user
    lands where they can uncheck it.
    """
    msg = (
        f'{reason}\n\n'
        "To use the Fusion-Essentials MCP server instead, turn OFF Fusion's "
        'built-in "Fusion MCP Server" setting in Preferences, then reload '
        'Fusion-Essentials (Utilities -> Add-Ins -> Stop, then Run).\n\n'
        'Opening Preferences now...'
    )
    futil.log(msg)
    if ui:
        ui.messageBox(msg, CMD_NAME)
        _open_preferences()


def _open_preferences():
    """Open Fusion's Preferences dialog (best-effort)."""
    try:
        pref_cmd = ui.commandDefinitions.itemById('PreferencesCommand')
        if pref_cmd:
            pref_cmd.execute()
    except Exception:
        # Non-fatal: the message box already told the user where to go.
        futil.handle_error(f'{CMD_NAME}._open_preferences')


def stop():
    """Called when the add-in stops (or the module is disabled on next load).

    Tear-down order: stop HTTP server -> join thread -> stop TaskManager.
    """
    global _http_server, _server_thread, _mcp
    try:
        # Remove the deferred-reload custom event (a fresh start() reinstalls it).
        try:
            from .tools import sys_reload_addin
            sys_reload_addin.uninstall_reload_event()
        except Exception:
            futil.handle_error(f'{CMD_NAME}.stop.uninstall_reload_event')
        if _http_server is not None or _server_thread is not None:
            mcp_server.stop_server(_http_server, _server_thread)
        TaskManager.stop()
        _http_server = None
        _server_thread = None
        _mcp = None
        futil.log(f'{CMD_NAME}: stopped')
    except Exception:
        futil.handle_error(f'{CMD_NAME}.stop')
