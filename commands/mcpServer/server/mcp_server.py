# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.
#
# Adapted from Autodesk's Fusion MCP add-in sample (MIT-licensed).

"""A dependency-free MCP server over HTTP that runs inside Fusion's Python.

Implements the MCP JSON-RPC methods we need (initialize, tools/list, tools/call,
and the resources/* reads over a static catalog the caller hands in) by hand, so
no external packages are required.

Differences from the sample this was adapted from:
  - The MCP endpoint is served on the path **/mcp** (to match Fusion's built-in
    well-known endpoint) in addition to "/".
  - Logging goes through fusion360utils (futil), not raw app.log/print.
  - start_server() distinguishes a port-already-in-use bind failure (EADDRINUSE)
    from other errors and reports it via a structured result, so the caller can
    tell the user to disable Autodesk's built-in MCP server.
"""

import asyncio
import errno
import hashlib
import json
import math
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from ....lib import fusion360utils as futil
from ..mcp_primitives.item import Item
from ..version import __version__
from .task_manager import TaskManager
from . import drawing_jobs

# The MCP path served by Fusion's built-in server; we mirror it so clients
# configured for the well-known endpoint reach us unchanged.
MCP_PATH = '/mcp'

# A measurement switch: while this marker file exists, tools/list carries no description at any
# depth - names, types, enums and required flags only - so an eval can measure what the
# descriptions buy. Nothing else reads it.
BARE_WIRE_MARKER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                ".wire_bare")


def _without_descriptions(obj):
    """obj with every 'description' key removed at any depth."""
    if isinstance(obj, dict):
        return {k: _without_descriptions(v) for k, v in obj.items() if k != "description"}
    if isinstance(obj, list):
        return [_without_descriptions(v) for v in obj]
    return obj

# Every protocol revision this server actually understands (Streamable HTTP transport). On
# initialize we honor the client's requested protocolVersion ONLY if it appears here; otherwise
# we respond with our newest supported revision instead of echoing semantics we do not implement.
SUPPORTED_PROTOCOL_VERSIONS = ('2025-03-26',)
PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]    # the default we offer: newest supported

# Server-level instructions, returned on `initialize` (the MCP spec field). They reach the client
# before it fetches any tool schema; whether a host puts them in the model's context is the host's
# decision, which no server can enforce. When tools are deferred (names only until searched), a
# per-tool "call FIRST" instruction is invisible at cold start, so this routes an agent that does
# read them to the two orientation tools before it fishes blindly.
INSTRUCTIONS = (
    "Autodesk Fusion control. COLD START: before reaching for specific tools, call two orientation reads "
    "first - sys_capability_map (what this server can do: the tool families + each one's entry tool) "
    "and workspace_orient (what's in front of you: the active document, its health, contents, and "
    "pointers to the right deep tool). Then drill with the family's tools. Searching by keyword for an "
    "existing capability/input-kind: sys_find_tool. Most reads are RICH: a <domain>_get (cam_get, "
    "design_get, doc_get, data_get) gives a light default plus include=[...] for depth. Every write tool "
    "accepts expect_document (a doc name or URN from a prior read): if the active document changed since "
    "that read, the write is REFUSED as active_document_changed - switch back with doc_activate and retry. "
    "Design practice for building a part or an assembly, rather than running a fixed procedure, is "
    "discovered through sys_get_guidance - its index first, then one section or one recipe per call."
)

# Header names (Streamable HTTP transport).
SESSION_HEADER = 'Mcp-Session-Id'

# Our server's identifying name. Returned by GET /health and used by the
# post-start self-check to confirm WE are the server answering on the port
# (vs. Autodesk's built-in server, which reports "MCP HTTP Server").
SERVER_NAME = "Fusion-Essentials MCP Server"

# Seconds a main-thread tool task may run before _execute_on_main_thread reports "still running".
# Tools that legitimately run long (e.g. STEP export, cloud upload) opt out via enforce_timeout=False
# on their Item. This is a fixed budget, not per-request configurable.
MAIN_THREAD_TASK_TIMEOUT_S = 30

# Result codes returned by start_server() so entry.py can react appropriately.
START_OK = 'ok'
START_PORT_IN_USE = 'port_in_use'
START_ERROR = 'error'


class _AfterSendResponse(dict):
    """A JSON response carrying one transport-local callback after its bytes flush."""

    def __init__(self, payload, after_send):
        super().__init__(payload)
        self.after_send = after_send


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request on its own daemon thread."""
    daemon_threads = True
    allow_reuse_address = False  # we WANT bind to fail loudly if 27182 is taken


# Handler kwargs DELIBERATELY absent from a tool's wire schema: the handler accepts the key and
# answers with a targeted redirect, which beats a generic unknown-argument error. The argument
# gate lets these through on otherwise-strict tools. Shrink-only; the reason lives as a comment
# at the tool's own registration site.
_SCHEMA_OMITTED_ARGS = {
    'joint_edit': frozenset({'rotation_deg'}),   # posing is joint_drive's job; handler redirects
}


def _refuse_json_constant(literal: str):
    """json.loads' hook for the three NON-STANDARD literals Python's decoder accepts by default -
    NaN, Infinity, -Infinity. None of them is a JSON number, and none is a length, a count or an
    index any tool can act on: NaN in particular passes every ``> 0`` / ``!= 0`` guard downstream
    (each comparison against it is False), so it is refused here at the door, naming the literal."""
    raise ValueError(f"'{literal}' is not a JSON number - NaN, Infinity and -Infinity are not "
                     "valid JSON. Send a finite number.")


def _refuse_json_float(literal: str):
    """Reject a JSON float token whose decoded value overflows to infinity."""
    value = float(literal)
    if not math.isfinite(value):
        raise ValueError(f"'{literal}' overflows to a non-finite number. Send a finite number.")
    return value


# What one resources/list row carries: the two fields the spec requires (uri, name) plus what a
# client shows and sizes a read against. 'text' is deliberately absent - a listing that carried the
# body would ship the whole document to every client that only enumerated.
_RESOURCE_LIST_FIELDS = ("uri", "name", "title", "description", "mimeType", "size")


def _wire_tool_rows(items):
    """The effective tools/list rows, including the configured bare-wire projection."""
    rows = [item.primitive.to_dict() for _, item in sorted(items.items())]
    return [_without_descriptions(row) for row in rows] if os.path.exists(BARE_WIRE_MARKER) else rows

def _schema_fingerprint(rows):
    """Hash the canonical actual tools/list schemas currently registered on this server."""
    body = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(body.encode("ascii")).hexdigest()


def _resource_row(resource: Dict[str, Any]) -> Dict[str, Any]:
    """One catalog entry as a listing row: the advertised fields it carries, and nothing else."""
    return {field: resource[field] for field in _RESOURCE_LIST_FIELDS if field in resource}


def _in_set(value: Any, allowed: frozenset) -> bool:
    """value in allowed, but an unhashable value (a list/dict where a string enum is expected) is
    simply not a member rather than a TypeError."""
    try:
        return value in allowed
    except TypeError:
        return False


def _enum_specs(schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Precompute the enum-constrained properties of one tool's input schema, so the per-call check is
    a dict lookup + set membership (never a re-walk). Returns {prop_name: {"values": [...], "set":
    frozenset, "array": bool}}. A property carries an enum either directly ({type:string, enum:[...]})
    or as array items ({type:array, items:{enum:[...]}}) - the latter validates each element."""
    specs: Dict[str, Dict[str, Any]] = {}
    props = (schema or {}).get("properties") or {}
    for pname, pschema in props.items():
        if not isinstance(pschema, dict):
            continue
        enum = pschema.get("enum")
        if isinstance(enum, list) and enum:
            specs[pname] = {"values": list(enum), "set": frozenset(enum), "array": False}
            continue
        if pschema.get("type") == "array":
            items = pschema.get("items")
            if isinstance(items, dict) and isinstance(items.get("enum"), list) and items["enum"]:
                specs[pname] = {"values": list(items["enum"]),
                                "set": frozenset(items["enum"]), "array": True}
    return specs


class SimpleMCPServer:
    """Routes MCP JSON-RPC requests to registered tool handlers, and serves a static resource
    catalog the caller built."""

    def __init__(self, name: str = SERVER_NAME, resources=None, job_store=None,
                 attestation=None):
        self.name = name
        # Session id assigned at initialize and echoed back to the client on every
        # response. Generated lazily so each server instance has a stable id.
        self.session_id = uuid.uuid4().hex
        self.tools: Dict[str, Item] = {}
        # tool name -> {prop: enum spec}, precomputed at registration so enum validation is O(1) per arg.
        self._enum_specs: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.server_info = {"name": name, "version": __version__}
        self._attestation = attestation
        self._job_store = job_store
        self._job_store_lock = threading.Lock()
        # Application-controlled content published over resources/*, built by the caller (entry.py)
        # and handed in whole: this transport serves what it was given and opens no product file of
        # its own. An entry with no address or no body cannot be served, so it is dropped here
        # rather than listed - an advertised resource whose read then fails is the one outcome to
        # avoid. A NAME is required on the same terms: the spec's listing row carries uri + name,
        # the projection below copies a present-but-null name straight onto the wire, and a row
        # naming nothing is a resource a client can only show as blank.
        self.resources = []
        for resource in (resources or []):
            if (isinstance(resource, dict) and isinstance(resource.get("uri"), str)
                    and resource.get("uri") and isinstance(resource.get("name"), str)
                    and resource.get("name") and isinstance(resource.get("text"), str)):
                self.resources.append(dict(resource))
            else:
                uri = resource.get("uri") if isinstance(resource, dict) else None
                futil.log("MCP resource skipped (needs a 'uri', a 'name' and a 'text' to serve): "
                          f"{uri!r}")
        self._resources_by_uri = {r["uri"]: r for r in self.resources}

    def schema_fingerprint(self):
        """The canonical fingerprint of the tools/list rows this instance currently serves."""
        return _schema_fingerprint(_wire_tool_rows(self.tools))

    def _with_schema_fingerprint(self, result):
        """Add this serving instance's schema identity to a valid capability-map result."""
        if not isinstance(result, dict) or result.get("isError") is not False:
            return result
        content = result.get("content")
        if (not isinstance(content, list) or not content
                or not isinstance(content[0], dict)
                or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str)):
            return result
        try:
            payload = json.loads(content[0]["text"])
        except (TypeError, ValueError):
            return result
        if not isinstance(payload, dict):
            return result
        payload["schema_fingerprint"] = self.schema_fingerprint()
        disclosed = dict(result)
        disclosed["content"] = list(content)
        disclosed["content"][0] = dict(content[0])
        disclosed["content"][0]["text"] = json.dumps(payload, indent=2)
        return disclosed

    def health(self):
        """The server health row with its current session and registered-schema identity."""
        try:
            observed = self._attestation() if callable(self._attestation) else self._attestation
            attestation = dict(observed or {})
        except Exception as exc:
            attestation = {"complete": False, "loaded_matches_source": False,
                           "problems": ["loaded attestation failed: " + type(exc).__name__]}
        attestation["schema_fingerprint"] = self.schema_fingerprint()
        return {"status": "healthy", "server": self.name,
                "version": self.server_info["version"], "session_id": self.session_id,
                "attestation": attestation}

    def register(self, item: Item):
        if not isinstance(item, Item):
            raise ValueError("Can only register Item instances")
        item_type = item.get_type()
        if item_type != "tool":
            raise ValueError(f"Only Tool items can be registered, got type: {item_type}")
        self.tools[item.primitive.name] = item
        self._enum_specs[item.primitive.name] = _enum_specs(item.primitive.input_schema)
        futil.log(f"MCP tool registered: {item.primitive.name}")

    async def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        # A JSON-RPC request MUST be a single object. A LIST body is a batch (this server does not
        # implement batching); any other non-object body is malformed. Either way return ONE
        # well-formed error object (id:null) instead of raising - the .get() calls below assume a
        # dict, and a raw raise here would surface as an opaque HTTP 500 (a list raised twice: once
        # here, again in the except handler's request.get).
        if not isinstance(request, dict):
            if isinstance(request, list):
                message = ("This server does not support JSON-RPC batching (an array of requests). "
                           "Send one request object per HTTP POST.")
            else:
                message = "Invalid Request: expected a single JSON-RPC request object."
            return self._error(None, -32600, message)
        try:
            method = request.get("method")
            request_id = request.get("id")
            params = request.get("params", {})

            # Notifications (no "id") get no response body; caller returns 202.
            is_notification = "id" not in request
            if is_notification:
                # e.g. notifications/initialized, notifications/cancelled - accept silently.
                return None

            if method == "initialize":
                return self._handle_initialize(request_id, params)
            elif method == "ping":
                return {"jsonrpc": "2.0", "id": request_id, "result": {}}
            elif method == "tools/list":
                return self._handle_tools_list(request_id)
            elif method == "tools/call":
                return await self._handle_tools_call(request_id, params)
            elif method == "resources/list":
                return self._handle_resources_list(request_id, params)
            elif method == "resources/read":
                return self._handle_resources_read(request_id, params)
            elif method == "resources/templates/list":
                return self._handle_resource_templates_list(request_id, params)
            else:
                return self._error(request_id, -32601, f"Method not found: {method}")
        except Exception as e:
            # Defensive: only a dict body has an id to echo (the entry guard above already rejects a
            # non-dict, but never call .get on something that might not be a dict).
            request_id = request.get("id") if isinstance(request, dict) else None
            return self._error(request_id, -32603, str(e))

    def _handle_initialize(self, request_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        # Honor the client's requested protocol version only if we actually implement it;
        # otherwise respond with our own version rather than claiming support we don't have.
        client_version = (params or {}).get("protocolVersion")
        if client_version in SUPPORTED_PROTOCOL_VERSIONS:
            protocol_version = client_version
        else:
            protocol_version = PROTOCOL_VERSION
        capabilities: Dict[str, Any] = {"tools": {}}
        if self.resources:
            # Advertised only when there is a servable resource in hand: a client that sees this
            # capability may call resources/list and read what it names. No 'subscribe' and no
            # 'listChanged' - the catalog is fixed for the server's lifetime, so there is nothing
            # to subscribe to and nothing to notify about.
            capabilities["resources"] = {}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol_version,
                "capabilities": capabilities,
                "serverInfo": self.server_info,
                # Sent before any tool schema is fetched; the host decides whether the model sees
                # it (it routes a contextless agent to sys_capability_map / workspace_orient first).
                "instructions": INSTRUCTIONS,
            },
        }

    def _handle_tools_list(self, request_id: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id,
                "result": {"tools": _wire_tool_rows(self.tools)}}

    async def _handle_tools_call(self, request_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if tool_name not in self.tools:
            # -32602 (Invalid params) is the spec's example code for an unknown tool name.
            return self._error(request_id, -32602, f"Tool not found: {tool_name}")
        item = self.tools[tool_name]

        # Validate BEFORE dispatch: an unknown/missing argument is a doomed call, so reject it
        # here rather than posting a main-thread task (or raising a raw TypeError from **kwargs).
        validation_error = self._validate_tool_arguments(tool_name, item, arguments)
        if validation_error is not None:
            return {"jsonrpc": "2.0", "id": request_id, "result": validation_error}

        execution_arguments = dict(arguments)
        if item.deferred_capable:
            deferred = execution_arguments.pop("deferred", False)
            request_key = execution_arguments.pop("request_key", None)
            if not isinstance(deferred, bool):
                result = self._tool_error_result("'deferred' must be true or false.")
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            if request_key is not None and not deferred:
                result = self._tool_error_result(
                    "'request_key' is used only with deferred=true.")
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            if deferred:
                return self._accept_deferred(request_id, tool_name, item, request_key,
                                             execution_arguments)

        futil.log(f"MCP calling tool: {tool_name}")
        try:
            if item.run_on_main_thread:
                result = await self._execute_on_main_thread(
                    item.handler, execution_arguments,
                    enforce_timeout=item.enforce_timeout)
            else:
                result = item.handler(**execution_arguments)
            if tool_name == "sys_capability_map":
                result = self._with_schema_fingerprint(result)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as e:
            # A tool EXECUTION failure (including the curated timeout messages from
            # _execute_on_main_thread) is reported inside the result with isError=true, per the MCP
            # spec, so the calling agent can read it and self-correct. It is NOT a JSON-RPC protocol
            # error (-32603 is reserved for protocol-level failures) and carries no Python traceback.
            futil.handle_error(f"MCP tool '{tool_name}'")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": self._tool_error_result(f"Tool '{tool_name}' failed: {e}"),
            }

    def _jobs(self):
        with self._job_store_lock:
            if self._job_store is None:
                self._job_store = drawing_jobs.get_store()
            return self._job_store

    @staticmethod
    def _tool_ok_result(payload):
        return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                "isError": False}

    def _accept_deferred(self, request_id, tool_name, item, request_key, arguments):
        expected = arguments.get("expect_document")
        if not isinstance(expected, str) or not expected.startswith("session:"):
            result = self._tool_error_result(
                "deferred drawing work requires expect_document as a session: handle from doc_get.")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        record, created, refusal = self._jobs().accept(request_key, tool_name, arguments)
        if refusal:
            result = self._tool_error_result(refusal)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        payload = {
            "accepted": record["status"] in ("accepted", "queued", "running"),
            "request_key": record["request_key"],
            "job_id": record["job_id"], "status": record["status"],
            "poll": {"tool": "drawing_get_status",
                     "request_key": record["request_key"]},
            "note": ("Poll drawing_get_status with the same request_key; retries with identical "
                     "arguments reuse this job."),
        }
        response = {"jsonrpc": "2.0", "id": request_id,
                    "result": self._tool_ok_result(payload)}
        if record["status"] != "accepted":
            return response
        return _AfterSendResponse(
            response, lambda: self._dispatch_deferred(record["request_key"], tool_name,
                                                       item, arguments))

    def _dispatch_deferred(self, request_key, tool_name, item, arguments):
        store = self._jobs()
        if not store.claim_dispatch(request_key):
            return

        def callback(_data):
            if not store.mark_running(request_key):
                store.fail_unclaimed(request_key, "running_state_could_not_be_persisted")
                return
            try:
                result = item.handler(**arguments)
            except Exception as ex:
                result = self._tool_error_result(f"Tool '{tool_name}' failed: {ex}")
            store.finish(request_key, result)

        try:
            if not TaskManager.is_running() and not TaskManager.start():
                store.fail_unclaimed(request_key, "task_manager_start_failed")
                return
            task_id = TaskManager.post(
                command="deferred_" + tool_name, callback=callback, data={},
                on_drop=lambda reason: store.fail_unclaimed(request_key, reason))
            if not task_id:
                store.fail_unclaimed(request_key, "task_post_failed")
        except Exception:
            store.fail_unclaimed(request_key, "task_dispatch_failed")

    @staticmethod
    def _tool_error_result(message: str) -> Dict[str, Any]:
        return {
            "content": [{"type": "text", "text": message}],
            "isError": True,
            "message": message,
        }

    def _validate_tool_arguments(self, tool_name: str, item: Item,
                                 arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Check `arguments` against item.primitive.input_schema before dispatch.

        Argument NAMES are checked here, and any argument whose schema property declares an `enum`
        is checked against it: a permissive client that sends an out-of-enum value (e.g.
        find_geometry kind="faces") would otherwise get silent wrong behavior (match_count:0) instead
        of a correction. Unknown keys are rejected ONLY when the tool's wire schema declares
        additionalProperties=false - a lenient schema stays lenient, so the schema never promises what
        the server then refuses. Keys a handler deliberately accepts off-schema pass through
        (_SCHEMA_OMITTED_ARGS). Failures come back as isError TOOL results rather than JSON-RPC
        protocol errors - a deliberate deviation from the spec's invalid-params bucket: an in-band
        result is what a calling agent can actually read and self-correct from.
        """
        schema = item.primitive.input_schema or {}
        properties = schema.get("properties") or {}
        required = schema.get("required") or []

        # strictness is declared on the primitive (strict_schema() sets additional_properties=False;
        # to_dict() renders it as the wire schema's additionalProperties) - read the same source.
        if item.primitive.additional_properties is False:
            allowed_extra = _SCHEMA_OMITTED_ARGS.get(tool_name, frozenset())
            unknown = sorted(key for key in arguments
                             if key not in properties and key not in allowed_extra)
            if unknown:
                keys = ", ".join(f"'{key}'" for key in unknown)
                return self._tool_error_result(
                    f"Unknown argument for tool '{tool_name}': {keys}. "
                    f"Expected arguments: {sorted(properties.keys())}. Remove it and retry.")

        for name in required:
            if name not in arguments:
                return self._tool_error_result(
                    f"Missing required argument for tool '{tool_name}': '{name}'. "
                    f"Required: {required}.")

        enum_error = self._validate_enums(tool_name, arguments)
        if enum_error is not None:
            return enum_error

        return None

    def _validate_enums(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Reject an out-of-enum argument value against the precomputed enum sets. A scalar enum prop
        must equal one of its values; an array-of-enum prop must have every element in the set. The
        error names the property, the offending value, and the valid values so the agent can correct."""
        for pname, spec in self._enum_specs.get(tool_name, {}).items():
            if pname not in arguments:
                continue
            value = arguments[pname]
            if value is None:
                continue                          # absent/unset - the handler's default applies
            if spec["array"]:
                if not isinstance(value, list):
                    continue                      # a non-list for an array prop is a shape error, not ours
                bad = [v for v in value if not _in_set(v, spec["set"])]
                if bad:
                    return self._tool_error_result(
                        f"Invalid value(s) for '{pname}' in tool '{tool_name}': "
                        f"{', '.join(repr(v) for v in bad)}. Valid values: {spec['values']}.")
            elif not _in_set(value, spec["set"]):
                return self._tool_error_result(
                    f"Invalid value for '{pname}' in tool '{tool_name}': {value!r}. "
                    f"Valid values: {spec['values']}.")
        return None

    async def _execute_on_main_thread(self, handler_func, arguments: Dict[str, Any],
                                      enforce_timeout: bool = True) -> Any:
        """Run handler_func(**arguments) on Fusion's main thread via TaskManager."""
        import time

        result_holder = {'result': None, 'exception': None, 'completed': False}
        result_lock = threading.Lock()

        def callback(data):
            try:
                result = handler_func(**data['arguments'])
                with result_lock:
                    result_holder['result'] = result
                    result_holder['completed'] = True
            except Exception as e:
                with result_lock:
                    result_holder['exception'] = e
                    result_holder['completed'] = True

        def dropped(reason):
            with result_lock:
                if not result_holder['completed']:
                    result_holder['exception'] = Exception(
                        f"Handler execution was dropped before it started ({reason}). "
                        "No change was made - safe to retry.")
                    result_holder['completed'] = True

        if not TaskManager.is_running():
            TaskManager.start()

        task_id = TaskManager.post(command="execute_handler", callback=callback,
                                   data={"arguments": arguments}, on_drop=dropped)
        if not task_id:
            raise Exception("Failed to post task to TaskManager")

        timeout = MAIN_THREAD_TASK_TIMEOUT_S
        start_time = time.time()
        while enforce_timeout is False or (time.time() - start_time < timeout):
            with result_lock:
                if result_holder['completed']:
                    if result_holder['exception'] is not None:
                        raise result_holder['exception']
                    return result_holder['result']
            await asyncio.sleep(0.01)

        # Timed out. Try to cancel the still-pending task so it never runs after we've given up.
        # cancel() returns True ONLY if it removed a task that had NOT yet started - in that case
        # the operation truly never ran. If it returns False the callback was already CLAIMED by
        # the main thread: it is running (or finished) and CANNOT be interrupted, so its side
        # effect (e.g. a cloud write or a committed design edit) may already be applying. We must
        # not lie that it was "cancelled before running" - that invites a blind retry -> double-apply.
        cancelled = TaskManager.cancel(task_id)
        with result_lock:
            if result_holder['completed']:
                # Finished in the cancel window - honor the real result, don't fake a timeout.
                if result_holder['exception'] is not None:
                    raise result_holder['exception']
                return result_holder['result']
        if cancelled:
            raise Exception(
                f"Handler execution timed out ({timeout}s); the operation was cancelled before it "
                "started running. No change was made - safe to retry.")
        raise Exception(
            f"Handler is still running after {timeout}s and could NOT be cancelled (an in-flight "
            "main-thread operation cannot be interrupted). It may still COMMIT its result. Do NOT "
            "blindly retry - re-check the design/document state first, then retry only if the "
            "change did not take effect. (For long operations, prefer a fire-and-poll tool.)")

    # ---- resources/*: the static catalog handed in at construction ----

    @staticmethod
    def _object_params(params: Any):
        """(params as a dict, error message). Absent params are an empty object; anything that is
        not a JSON object is invalid - saying so beats the -32603 the following .get would raise."""
        if params is None:
            return {}, None
        if isinstance(params, dict):
            return params, None
        return {}, "Invalid params: 'params' must be a JSON object."

    def _listing_refusal(self, method: str, params: Any) -> Optional[str]:
        """Why either resources listing cannot answer `params`, or None to proceed. The params
        member must be a JSON object, and a 'cursor' is one this server never issued - both
        listings answer in one page and neither result carries a nextCursor, so answering a page
        for a cursor would tell a paginating client it had resumed something. Asked in ONE place,
        so the two listings cannot refuse the same call differently."""
        params, invalid = self._object_params(params)
        if invalid:
            return invalid
        if params.get("cursor") is not None:
            return (f"Invalid params: {method} answers in one page and issues no cursor to "
                    "continue. Retry without 'cursor'.")
        return None

    def _published(self) -> str:
        """The addresses this server actually serves - what a client that guessed one needs next."""
        if not self.resources:
            return "This server publishes no resources."
        return "This server publishes: " + ", ".join(sorted(self._resources_by_uri))

    def _handle_resources_list(self, request_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        invalid = self._listing_refusal("resources/list", params)
        if invalid:
            return self._error(request_id, -32602, invalid)
        return {"jsonrpc": "2.0", "id": request_id,
                "result": {"resources": [_resource_row(r) for r in self.resources]}}

    def _handle_resources_read(self, request_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params, invalid = self._object_params(params)
        if invalid:
            return self._error(request_id, -32602, invalid)
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.strip():
            return self._error(request_id, -32602,
                               "Invalid params: resources/read needs 'uri', the address of one "
                               f"resource. {self._published()}")
        resource = self._resources_by_uri.get(uri)
        if resource is None:
            # -32002 is the spec's resource-not-found code, and it is a PROTOCOL error: the client
            # asked for something this server does not publish, which is not a tool result.
            return self._error(request_id, -32002,
                               f"Resource not found: {uri}. {self._published()}")
        content = {"uri": resource["uri"], "text": resource["text"]}
        if resource.get("mimeType"):
            content["mimeType"] = resource["mimeType"]
        return {"jsonrpc": "2.0", "id": request_id, "result": {"contents": [content]}}

    def _handle_resource_templates_list(self, request_id: Any,
                                        params: Dict[str, Any]) -> Dict[str, Any]:
        # Every published resource is a fixed address, so there is no URI template to expand: a
        # well-formed probe gets the empty list, which is the true answer rather than an error the
        # client would have to special-case. A malformed one is refused on the same terms the other
        # listing uses - one server answering one bad call two ways is what a client cannot code
        # against.
        invalid = self._listing_refusal("resources/templates/list", params)
        if invalid:
            return self._error(request_id, -32602, invalid)
        return {"jsonrpc": "2.0", "id": request_id, "result": {"resourceTemplates": []}}

    def _error(self, request_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# The only origins a browser may present. Matched against the PARSED hostname of the Origin
# header, so a host that merely contains a loopback label (localhost.evil.com) is not a member.
# '::1' is what urlsplit().hostname yields for http://[::1]:port - it strips the brackets.
_ALLOWED_ORIGIN_SCHEMES = frozenset({'http', 'https'})
_ALLOWED_ORIGIN_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})


class MCPHandler(BaseHTTPRequestHandler):
    """HTTP request handler bridging HTTP <-> the MCP JSON-RPC server."""

    mcp_server: Optional[SimpleMCPServer] = None  # set on the subclass per server

    # ---- Streamable HTTP transport (MCP 2025-03-26) ----

    def _origin_ok(self) -> bool:
        """Reject cross-origin (DNS-rebinding) requests; allow no-Origin local tools.

        Per the spec security note, validate Origin. Local CLI clients (and our own
        probes) typically send no Origin header, which we allow; browsers send one,
        which must PARSE to a loopback origin.
        """
        origin = self.headers.get('Origin')
        if not origin:
            return True
        try:
            parts = urlsplit(origin)
            hostname, _port = parts.hostname, parts.port   # .port raises on a bad port
        except ValueError:
            return False        # unparseable Origin (bad IPv6 literal / port) - refuse, never allow
        # Compare the parsed scheme + host, never the raw string: substring containment would
        # admit http://localhost.evil.com and http://127.0.0.1.evil.com, which resolve to an
        # attacker's server and are exactly the DNS-rebinding case this guard exists for.
        # Origin: null is REFUSED - it is what a sandboxed (attacker-controlled) iframe sends, so
        # allowing it reopens the hole. A local client that legitimately sends null can send no
        # Origin header at all instead, which is allowed above.
        return parts.scheme in _ALLOWED_ORIGIN_SCHEMES and hostname in _ALLOWED_ORIGIN_HOSTS

    def do_POST(self):
        # MCP endpoint on /mcp (well-known) and "/" (convenience).
        if self.path not in (MCP_PATH, '/'):
            self.send_error(404, "Not Found")
            return
        if not self._origin_ok():
            self.send_error(403, "Origin not allowed")
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            request_data = json.loads(post_data.decode('utf-8'),
                                      parse_constant=_refuse_json_constant,
                                      parse_float=_refuse_json_float)
        except (ValueError, json.JSONDecodeError) as e:
            # The parse failure's own reason travels to the caller: "Invalid JSON" alone cannot tell
            # a truncated body from a refused NaN literal, and a client can only correct what it is
            # told. Whitespace-collapsed, because this rides in the HTTP status line.
            detail = " ".join(str(e).split()) or "the body could not be parsed"
            self.send_error(400, f"Invalid JSON: {detail}")
            return

        try:
            response = asyncio.run(self.mcp_server.handle_request(request_data))
        except Exception as e:
            self.send_error(500, str(e))
            return

        # Notifications/responses (no id) -> 202 Accepted, no body (spec rule 4).
        if response is None:
            self.send_response(202)
            self.send_header(SESSION_HEADER, self.mcp_server.session_id)
            origin = self.headers.get('Origin')
            if origin and self._origin_ok():
                self.send_header('Access-Control-Allow-Origin', origin)
            self.end_headers()
            return

        # Requests -> single JSON object (we use application/json, not SSE; spec rule 5).
        self._send_json(response)
        after_send = getattr(response, 'after_send', None)
        if after_send is not None:
            after_send()

    def do_GET(self):
        if not self._origin_ok():
            self.send_error(403, "Origin not allowed")
            return
        # Convenience/diagnostic endpoints (not part of the MCP transport).
        if self.path == '/health':
            self._send_json(self.mcp_server.health())
            return
        if self.path == '/tools':
            self._send_json(self.mcp_server._handle_tools_list(1))
            return
        # GET on the MCP endpoint = client asking to open a server->client SSE stream.
        # We don't offer one, so 405 (spec-compliant; the client falls back to POST).
        if self.path in ('/', MCP_PATH):
            self.send_response(405, "Method Not Allowed")
            self.send_header('Allow', 'POST')
            self.end_headers()
            return
        self.send_error(404, "Not Found")

    def do_DELETE(self):
        # Client requesting explicit session termination; we don't support it -> 405.
        if self.path in ('/', MCP_PATH):
            self.send_response(405, "Method Not Allowed")
            self.send_header('Allow', 'POST')
            self.end_headers()
            return
        self.send_error(404, "Not Found")

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header(SESSION_HEADER, self.mcp_server.session_id)
        # No permissive CORS header: this is a loopback-only server, not a browser
        # API, and the Origin check already restricts who may call it. Echoing the
        # caller's loopback Origin keeps legitimate same-machine browser clients
        # working without opening it to arbitrary sites.
        origin = self.headers.get('Origin')
        if origin and self._origin_ok():
            self.send_header('Access-Control-Allow-Origin', origin)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, *args):
        pass  # silence default stderr logging


def start_server(host: str, port: int, items=None, resources=None, job_store=None,
                 attestation=None):
    """Start the MCP HTTP server on host:port in a background thread.

    Returns a dict:
        {"status": START_OK, "mcp": ..., "http_server": ..., "thread": ...}
        {"status": START_PORT_IN_USE, "port": port}     # bind hit EADDRINUSE
        {"status": START_ERROR, "message": "..."}        # any other failure

    `resources` is the static MCP Resource catalog the caller built (entry.py) -
    entries of {uri, name, ..., text}. The transport publishes what it is handed
    and loads no content itself; an empty catalog means the resources capability
    is never advertised.

    The caller (entry.start) is responsible for surfacing the port-in-use case to
    the user (likely Autodesk's built-in MCP server holding 27182).
    """
    try:
        mcp = SimpleMCPServer(resources=resources, job_store=job_store,
                              attestation=attestation)
        for item in (items or []):
            mcp.register(item)

        # Per-server handler subclass carrying its own mcp instance.
        handler_cls = type('BoundMCPHandler', (MCPHandler,), {'mcp_server': mcp})

        try:
            http_server = ThreadedHTTPServer((host, port), handler_cls)
        except OSError as e:
            if e.errno in (errno.EADDRINUSE, errno.EACCES) or getattr(e, 'winerror', None) == 10048:
                futil.log(f"MCP server: port {port} already in use (likely Fusion's built-in MCP server)")
                return {"status": START_PORT_IN_USE, "port": port}
            raise

        try:
            current_store = drawing_jobs.start_server_store(store=job_store)
            mcp._job_store = current_store
        except Exception:
            http_server.server_close()
            raise

        thread = threading.Thread(
            target=http_server.serve_forever,
            daemon=True,
            name=f"FE-MCP-Server-{host}:{port}",
        )
        thread.start()
        futil.log(f"MCP server started on http://{host}:{port}{MCP_PATH}")
        return {"status": START_OK, "mcp": mcp, "http_server": http_server, "thread": thread}
    except Exception:
        futil.handle_error('mcp_server.start_server')
        return {"status": START_ERROR, "message": "Failed to start MCP server (see Text Commands log)"}


def verify_ownership(host: str, port: int, timeout: float = 2.0):
    """Probe GET http://host:port/health and check who is answering.

    Layer-2 collision check: even after a successful bind, confirm the
    server replying on the port is actually ours and not, say, Autodesk's built-in
    server that won an earlier race. Returns one of:
        "ours"      -> /health reports our SERVER_NAME (all good)
        "foreign"   -> something else answered (e.g. Autodesk's "MCP HTTP Server")
        "unreachable" -> nothing answered / error (treat as inconclusive)

    Runs from entry.start() on the main thread; our own server answers on its
    background thread, so this self-request does not deadlock. Kept short-timeout
    and fully defensive so it can never hang Fusion startup.
    """
    import json as _json
    import urllib.request

    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        return "ours" if data.get("server") == SERVER_NAME else "foreign"
    except Exception:
        return "unreachable"


def stop_server(http_server, thread, timeout: float = 5) -> bool:
    """Shut down the HTTP server and join its thread. Safe to call with None."""
    try:
        if http_server:
            http_server.shutdown()
            http_server.server_close()
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
            return not thread.is_alive()
        return True
    except Exception:
        futil.handle_error('mcp_server.stop_server')
        return False
