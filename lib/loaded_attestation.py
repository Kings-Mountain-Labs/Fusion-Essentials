# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Capture the code objects actually imported for one bounded source root."""
import hashlib
import json
import os
import struct
import sys
import types
import uuid

_STATE = None

def _path(path):
    """Canonical absolute path, or None when path is not a source file."""
    try:
        return os.path.normcase(os.path.abspath(path)) if path else None
    except (TypeError, ValueError):
        return None

def _inside(path, root):
    """Whether canonical path is a Python source file below canonical root."""
    return bool(path and path.endswith(".py") and path.startswith(root + os.sep))

def _digest(value):
    """SHA-256 of one byte sequence."""
    return hashlib.sha256(value).hexdigest()

def _constant_row(value):
    """A typed, reference-independent representation of one immutable code constant."""
    kind = type(value)
    if value is None:
        return ["none"]
    if value is Ellipsis:
        return ["ellipsis"]
    if kind is bool:
        return ["bool", value]
    if kind is int:
        return ["int", str(value)]
    if kind is float:
        return ["float", struct.pack("!d", value).hex()]
    if kind is complex:
        return ["complex", struct.pack("!d", value.real).hex(),
                struct.pack("!d", value.imag).hex()]
    if kind is str:
        return ["str", value]
    if kind is bytes:
        return ["bytes", value.hex()]
    if kind is tuple:
        return ["tuple", [_constant_row(item) for item in value]]
    if kind is frozenset:
        rows = [_constant_row(item) for item in value]
        rows.sort(key=lambda row: json.dumps(
            row, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
        return ["frozenset", rows]
    if kind is slice:
        return ["slice", _constant_row(value.start), _constant_row(value.stop),
                _constant_row(value.step)]
    if isinstance(value, types.CodeType):
        return _code_row(value)
    raise TypeError("unsupported code constant: " + kind.__name__)


def _code_row(code):
    """A typed representation of the executable and source-location fields of one code object."""
    return [
        "code", code.co_argcount, getattr(code, "co_posonlyargcount", 0),
        code.co_kwonlyargcount, code.co_nlocals, code.co_stacksize, code.co_flags,
        code.co_code.hex(), [_constant_row(item) for item in code.co_consts],
        list(code.co_names), list(code.co_varnames), list(code.co_freevars),
        list(code.co_cellvars), code.co_filename, code.co_name,
        getattr(code, "co_qualname", code.co_name), code.co_firstlineno,
        (code.co_linetable if hasattr(code, "co_linetable") else code.co_lnotab).hex(),
        getattr(code, "co_exceptiontable", b"").hex(),
    ]


def _code_digest(code):
    """A reference-independent digest of one code object and its nested constants."""
    body = json.dumps(_code_row(code), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    return _digest(body.encode("ascii"))

def _source_digest(path, filename):
    """Compile current source without importing it and hash that code object."""
    with open(path, "rb") as fh:
        source = fh.read()
    return _code_digest(compile(source, filename, "exec", dont_inherit=True))

def _fingerprint(rows):
    """Deterministic digest of JSON-serializable attestation rows."""
    body = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return _digest(body.encode("ascii"))

def _observe(frame):
    """Record one executed in-scope module frame exactly once per module object."""
    state = _STATE
    if state is None or frame.f_code.co_name != "<module>":
        return
    path = _path(frame.f_code.co_filename)
    if not _inside(path, state["root"]):
        return
    module_name = frame.f_globals.get("__name__")
    if not isinstance(module_name, str) or not module_name:
        state["disruptions"].append("module frame had no name: " + path)
        return
    try:
        code = _code_digest(frame.f_code)
    except Exception as exc:
        state["disruptions"].append(
            "module code capture failed for %s: %s" % (path, type(exc).__name__))
        return
    record = {"name": module_name, "path": path, "filename": frame.f_code.co_filename,
              "globals_id": id(frame.f_globals), "code": code}
    prior = state["captured"].get(path)
    if prior is not None and prior != record:
        state["disruptions"].append("module path executed more than once: " + path)
        return
    state["captured"][path] = record

def _profile(frame, event, arg):
    """Chain the pre-existing profiler while observing executed module frames."""
    try:
        if event == "call":
            _observe(frame)
    finally:
        prior = _STATE.get("prior") if _STATE is not None else None
        if prior is not None:
            prior(frame, event, arg)

def begin(source_root):
    """Start one capture window before any in-scope imports; replaces no import machinery."""
    global _STATE
    root = _path(source_root)
    if root is None:
        raise ValueError("source_root must be a path")
    if _STATE is not None and _STATE["active"]:
        _STATE["disruptions"].append("capture began while another capture was active")
        return
    _STATE = {"root": root, "captured": {}, "disruptions": [], "prior": sys.getprofile(),
              "load_id": uuid.uuid4().hex, "active": True}
    sys.setprofile(_profile)

def resume():
    """Resume capture into the current record set for one more bounded import window."""
    if _STATE is None:
        return False
    if _STATE["active"]:
        _STATE["disruptions"].append("capture resumed while already active")
        return False
    _STATE["prior"] = sys.getprofile()
    sys.setprofile(_profile)
    _STATE["active"] = True
    return True


def finish():
    """End the current capture window and restore the prior profiler exactly once."""
    if _STATE is None or not _STATE["active"]:
        return
    if sys.getprofile() is not _profile:
        _STATE["disruptions"].append("capture profiler was replaced before finish")
    else:
        sys.setprofile(_STATE["prior"])
    _STATE["active"] = False

def _loaded_modules(state):
    """The in-scope modules currently resident in sys.modules, keyed by source path."""
    rows, duplicates = {}, []
    for module in list(sys.modules.values()):
        path = _path(getattr(module, "__file__", None)) if module is not None else None
        if not _inside(path, state["root"]):
            continue
        row = {"name": getattr(module, "__name__", None),
               "globals_id": id(getattr(module, "__dict__", {}))}
        if path in rows and rows[path] != row:
            duplicates.append(path)
        rows[path] = row
    return rows, duplicates

def attest():
    """Return loaded-code evidence, failing closed for uncaptured or changed modules."""
    state = _STATE
    if state is None:
        return {"complete": False, "loaded_matches_source": False,
                "problems": ["capture was never started"]}
    if state["active"]:
        return {"load_id": state["load_id"], "complete": False,
                "loaded_matches_source": False,
                "problems": ["capture window is still active"]}
    loaded, duplicates = _loaded_modules(state)
    problems = list(state["disruptions"])
    problems.extend("same source path is bound to several modules: " + path for path in duplicates)
    rows = []
    for path in sorted(set(state["captured"]) - set(loaded)):
        problems.append("captured module is no longer loaded: " + path)
    for path, module in sorted(loaded.items()):
        captured = state["captured"].get(path)
        if captured is None:
            problems.append("uncaptured loaded module: " + path)
            continue
        if captured["name"] != module["name"] or captured["globals_id"] != module["globals_id"]:
            problems.append("loaded module was replaced: " + path)
            continue
        try:
            source = _source_digest(path, captured["filename"])
        except Exception as exc:
            problems.append("source compile failed for %s: %s" % (path, type(exc).__name__))
            continue
        if source != captured["code"]:
            problems.append("loaded code differs from source: " + path)
            continue
        rows.append({"path": os.path.relpath(path, state["root"]).replace(os.sep, "/"),
                     "code": captured["code"]})
    complete = bool(rows) and not problems
    return {"load_id": state["load_id"], "implementation_fingerprint": _fingerprint(rows),
            "executed_module_count": len(rows), "loaded_matches_source": complete,
            "complete": complete, "problems": problems,
            "scope": "commands/mcpServer import closure",
            "bootstrap_exclusions": ["Fusion-Essentials.py", "lib/loaded_attestation.py"]}
