# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The step engine and the receipt: what runs the acts, and what stamps the run.

`run_steps` is the ONE (tool, args, expect, save) engine every live harness judges its steps
through, so the status vocabulary cannot fork; `judged_steps` is the list that pairs positionally
with its rows. `run` probes each declared capability once - at the first act or step declaring it -
walks the acts, takes each one's narrative or fallback lane - or holds it back where its capability
is unmet - fires the reload beat once every act has run, and turns the rows into the per-tool
ledger.
`source_hash`/`write_verified`/`check` are the receipt: a run whose every act has passed stamps
VERIFIED_TOOLS.md with a hash of the tool source AND this harness, and `--check` recomputes it
offline. A run is not capped by wall clock - it is RESUMABLE: `--run <id>` saves the ctx, the ledger
so far and the act cursor after every act, and `--resume` continues that id against the document the
last chunk left open, so the receipt is stamped from the union of the chunks that walked the program.

Orchestration only - no step rows, no domain knowledge. The names a consumer patches (`call`,
`ACTS`, `STORY`, `source_hash`, ...) are read off the FACADE namespace through `verify_core.facade`
when a run starts, never bound here at import.
"""

import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time

# Fusion payloads carry non-ASCII (PMI symbols); the Windows console default cannot encode them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from verify_core import (
    NOTE_MAX, REFUSAL_NOTE_MAX, REPO_ROOT, SRC_ROOT, STEP_SLEEP_S, VERIFIED, _HERE, _RECALL,
    _Refusal, _leaves_no_row, _unparked, capability_met,
    capability_skip_reason, facade, parked_reason, predicate_kind, probe_capabilities,
    step_capability)


# tests/live also holds harnesses the sweep never imports - they measure API facts and drive the
# owner-present drawing tier, and judge no step of this run. Hashing them makes a row-only edit
# to one invalidate a receipt its content takes no part in.
_NOT_THE_SWEEP = ("measure_api.py", "drawing_verify.py")
_ATTESTATION_TCB = ("Fusion-Essentials.py", "lib/loaded_attestation.py")


def source_hash(root=None):
    """SHA-256 over every .py under commands/mcpServer/ PLUS the SWEEP harness under tests/live/
    (its standalone siblings excepted - see _NOT_THE_SWEEP) - the receipt key binding a green run
    to the exact tool source AND the exact predicates/exclusions it was judged by (a weakened
    predicate or a tool quietly moved into EXCLUDED must invalidate the receipt, not ride under
    it). Relative paths are normalized to '/' and CRLF to LF so the digest is identical across OS
    and git line-ending config; __pycache__ is skipped."""
    root = root or SRC_ROOT
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                entries.append((rel, full))
    if root == SRC_ROOT:      # a custom root (the offline tests') hashes only itself
        for rel in _ATTESTATION_TCB:
            entries.append((rel, os.path.join(REPO_ROOT, *rel.split("/"))))
        for fn in sorted(os.listdir(_HERE)):
            if fn.endswith(".py") and fn not in _NOT_THE_SWEEP:
                entries.append(("tests_live/" + fn, os.path.join(_HERE, fn)))
    hasher = hashlib.sha256()
    for rel, full in sorted(entries):
        with open(full, "rb") as fh:
            content = fh.read().replace(b"\r\n", b"\n")
        hasher.update(rel.encode("utf-8") + b"\0" + content + b"\0")
    return hasher.hexdigest()


_STAMP_RE = re.compile(r"^Stamp: source ([0-9a-f]{64}) \| Fusion (\S+) \| verified (\S+)",
                       re.MULTILINE)
_LOADED_RE = re.compile(
    r"^Loaded: implementation ([0-9a-f]{64}) \| schema ([0-9a-f]{64}) \| load (\S+) \| session (\S+)$",
    re.MULTILINE)


def write_verified(ledger, fusion_version, stamp_date, src_hash, path=None, notes=None,
                   act_modes=None, attestation=None):
    """Write the tracked receipt. Called only on a run with zero FAIL/blocked/pass* steps. 'notes'
    maps a driven tool to its shot-list step text (the ledger doubles as the demo's shot list) - a
    third column, empty when absent so the two-column stamp/count contract is unchanged.
    'act_modes' lists (act, narrative|fallback) - a machine-readable column, so a fallback-heavy
    run is visible without reading prose."""
    if not isinstance(attestation, dict) or not all(
            isinstance(attestation.get(key), str) and attestation[key]
            for key in ("implementation_fingerprint", "schema_fingerprint", "load_id",
                        "session_id")):
        raise ValueError("receipt needs the complete loaded implementation/schema/session identity")
    notes = notes or {}
    n_cov = sum(1 for _, s in ledger if s == "covered")
    # 'called' and 'called (<parked reason>)' are the SAME bucket - the reason is rendering, not a
    # third state, so the count line cannot drift from the table when a step is parked.
    n_called = sum(1 for _, s in ledger if s == "called" or s.startswith("called ("))
    n_pend = sum(1 for _, s in ledger if s.startswith("PENDING"))
    n_ref = sum(1 for _, s in ledger if s.startswith("refusals-only"))
    n_skip = len(ledger) - n_cov - n_called - n_pend - n_ref
    lines = [
        "# Live tool verification (generated by tool_verify.py - do not edit)",
        "",
        "This is a FIVE-BUCKET ledger, not a clean bill of health. It does NOT claim every tool",
        "is verified - the count line below is authoritative, and the per-tool table says which",
        "bucket each tool is in:",
        "",
        "- covered: a live step drove the tool this run and its expectation was a VALUE PREDICATE -",
        "  the step read keys off the ok payload (a delta, a count read back off the feature, a",
        "  verified flag) and those values held. This is the bucket that carries evidence.",
        "- called: every passing step for this tool was a bare 'ok' expectation - the call came back",
        "  without an error and NOTHING in the payload was read. What that proves is whatever the",
        "  tool's own post-write verification refuses on, and no more. Read a called row as",
        "  'called, and did not fail' - it is the queue for the next predicate tranche. A",
        "  'called (reason)' row is PARKED: a step deliberately held at a bare 'ok', the reason",
        "  naming what blocks its value predicate.",
        "- refusals-only: every step that ran was a deliberate guard refusal - the guards are",
        "  proven, but NO effect was produced or read back. Not covered; the create/act path",
        "  still needs a real step or a recorded gate reason.",
        "- skipped(reason): deliberately NOT driven unattended (cloud / interactive / irreversible",
        "  tier), or held back by the CAPABILITY tier - a step or act declaring an entitlement this",
        "  installation's capability probe did not read as granted. Each row names why. Not",
        "  verified - excused.",
        "- pending: no step drives it yet. UNVERIFIED, not known-good - it has never run in this",
        "  sweep. Shrinking this bucket means scripting a real step, not relabelling it.",
        "",
        "The stamp's source hash binds this run to the exact `commands/mcpServer/` tree AND the",
        "tests/live/ harness (steps, predicates, exclusions) it was judged by: `--check`",
        "recomputes the hash and fails on any difference, so a green suite cannot ride on a live",
        "run that never saw the current code or a weakened predicate. Only a run with zero",
        "FAIL/blocked/pass* steps rewrites this file.",
        "",
        "Stamp: source {0} | Fusion {1} | verified {2}".format(src_hash, fusion_version, stamp_date),
        "Loaded: implementation {0} | schema {1} | load {2} | session {3}".format(
            attestation["implementation_fingerprint"], attestation["schema_fingerprint"],
            attestation["load_id"], attestation["session_id"]),
        "",
        "{0} covered / {1} called / {2} refusals-only / {3} skipped(reason) / {4} pending".format(
            n_cov, n_called, n_ref, n_skip, n_pend),
    ]
    if act_modes:
        lines += ["", "| act | mode |", "|---|---|"]
        lines += ["| {0} | {1} |".format(a, m) for a, m in act_modes]
    lines += [
        "",
        "| tool | status | step (the demo's shot list) |",
        "|---|---|---|",
    ]
    for tool, status in ledger:
        lines.append("| {0} | {1} | {2} |".format(
            tool, status.replace("|", "/"), notes.get(tool, "").replace("|", "/")))
    with open(path or VERIFIED, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return path or VERIFIED


def check(root=None, verified_path=None, attestation=None):
    """The receipt gate: drives nothing, needs no Fusion. Exit 0 = the last green live run saw
    exactly this tool source; 1 = no receipt, or the source changed since that run."""
    source_hash = facade("source_hash")
    path = verified_path or VERIFIED
    if not os.path.exists(path):
        print("VERIFIED_TOOLS.md does not exist - run tool_verify.py once against live Fusion.")
        return 1
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = _STAMP_RE.search(text)
    loaded = _LOADED_RE.search(text)
    if not m or not loaded:
        print("VERIFIED_TOOLS.md has no complete source/loaded stamp - regenerate it "
              "(run tool_verify.py).")
        return 1
    stamped_hash, stamped_version, stamped_date = m.groups()
    current = source_hash(root)
    if current != stamped_hash:
        print("tool source changed since the last live verification ({0}, Fusion {1}) -"
              .format(stamped_date, stamped_version))
        print("re-run with Fusion up: py -3 tests/live/tool_verify.py")
        return 1
    stamped_identity = dict(zip(
        ("implementation_fingerprint", "schema_fingerprint", "load_id", "session_id"),
        loaded.groups()))
    if attestation is not None and _identity_refusal(stamped_identity, attestation):
        print("loaded implementation/schema/session differs from the verification receipt")
        return 1
    print("live verification current: source matches the green run of {0} (Fusion {1})"
          .format(stamped_date, stamped_version))
    return 0


RESULTS_DIR = os.path.join(_HERE, "results")


def run_state_path(run_id):
    """Where one run id's chunk state lives - the file a --resume reads."""
    return os.path.join(RESULTS_DIR, "run-{0}.json".format(run_id))


def _jsonable(mapping, what):
    """`mapping` as it will be written, refusing a value that cannot cross a chunk boundary by
    NAMING its key - a stringified handle would resume into a step that fails on a bad reference."""
    bad = []
    for key, value in mapping.items():
        try:
            json.dump({key: value}, io.StringIO())
        except (TypeError, ValueError):
            bad.append(key)
    if bad:
        raise TypeError("{0} cannot be saved across a chunk boundary: {1} - the run id carries "
                        "what a later act reads, so a value that will not serialize has to be "
                        "stored as one that does".format(what, ", ".join(sorted(bad))))
    return mapping


def save_run_state(run_id, state):
    """Atomically replace one resumable run checkpoint after fully writing its new state."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    body = dict(state)
    body["ctx"] = _jsonable(body.get("ctx") or {}, "the run's ctx")
    body["recall"] = _jsonable(body.get("recall") or {}, "the run's recalled values")
    encoded = io.StringIO()
    json.dump(body, encoded, indent=2)
    path = run_state_path(run_id)
    fd, temporary = tempfile.mkstemp(
        prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=RESULTS_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(encoded.getvalue())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path


def load_run_state(run_id):
    """The saved state for a run id, or None when that id has never run."""
    path = run_state_path(run_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _document_now():
    """Read the active document handle and design count used by resume at a chunk boundary."""
    call = facade("call")
    is_error, doc = call("doc_get", {})
    if is_error or not isinstance(doc, dict):
        return {"name": None, "document_id": None, "document_handle": None,
                "feature_count": None, "state": "unknown"}
    active = doc.get("active")
    if active is not None and not isinstance(active, dict):
        active = {}
    active = active or {}
    is_error, design = call("design_get", {})
    features = None if is_error or not isinstance(design, dict) else design.get("feature_count")
    handle = active.get("document_handle")
    state = "known" if _valid_session_handle(handle) else "unknown"
    return {"name": active.get("name"), "document_id": active.get("document_id"),
            "document_handle": handle, "feature_count": features, "state": state}


def _identity_refusal(previous, current, allow_new_session=False):
    """Why a loaded identity no longer matches the run boundary, or None."""
    if current is None:
        return "the current health row has no complete loaded attestation"
    for field in ("implementation_fingerprint", "schema_fingerprint", "load_id"):
        if previous.get(field) != current.get(field):
            return "the loaded %s changed during this run" % field
    if not allow_new_session and previous.get("session_id") != current.get("session_id"):
        return "the server session changed without an observed reload"
    return None


def resume_refusal(state, run_id, current_hash, document, current_identity=None):
    """Why this resume cannot be joined to the chunks before it, or None: no such run id, an id that
    already walked the program (a resume would stamp from its saved ledger without driving a step),
    a SOURCE that moved (the receipt binds one hash), or a DOCUMENT that is not the one the last
    chunk left open (the remaining acts would run against a world that was never built)."""
    if state is None:
        return ("no run state for {0!r} - {1} does not exist, so there is nothing to resume; start "
                "the run with --run {0}".format(run_id, run_state_path(run_id)))
    if state.get("complete"):
        return ("run {0!r} has already walked the whole program - resuming it would stamp a receipt "
                "from its saved ledger without driving anything. Start a new run id.".format(run_id))
    if state.get("source_hash") != current_hash:
        return ("the tool source changed since this run's first chunk ({0}...) - the receipt binds "
                "ONE hash, so a resume under {1}... would stamp a run no chunk was judged under. "
                "Start a new run id.".format(str(state.get("source_hash"))[:12], current_hash[:12]))
    identity = _identity_refusal(state.get("attestation") or {}, current_identity)
    if identity:
        return identity + ". Start a new run id."
    return _document_refusal(state, run_id, document)


def develop_refusal(state, run_id, document):
    """Why a DEVELOPMENT walk (--resume with --acts) cannot use this run's saved world, or None: no
    such run id, or a document that is not the one it left open. The source hash is not held - a
    walk that stamps nothing may run edited acts, which is what it is for."""
    if state is None:
        return ("no run state for {0!r} - {1} does not exist, so there is no saved world to walk; "
                "start the run with --run {0}".format(run_id, run_state_path(run_id)))
    return _document_refusal(state, run_id, document)


def _document_refusal(state, run_id, document):
    """Why the active document is not the one this run left open, or None."""
    was = state.get("document") or {}
    saved_handle, current_handle = was.get("document_handle"), document.get("document_handle")
    def valid_handle(value):
        suffix = value[len("session:"):] if isinstance(value, str) else ""
        return (isinstance(value, str) and value.startswith("session:") and bool(suffix)
                and all(not character.isspace() for character in value))
    if not valid_handle(saved_handle) or not valid_handle(current_handle):
        return ("run {0!r} has no complete document handle in its saved or current identity - "
                "the exact active document is unreadable, so start a new run id.".format(run_id))
    if type(saved_handle) is not type(current_handle) or saved_handle != current_handle:
        return ("the document handle this run left open is not the active one: it was {0!r} and "
                "{1!r} is active now. Start a new run id.".format(saved_handle, current_handle))
    then, now = was.get("feature_count"), document.get("feature_count")
    if isinstance(then, int) and isinstance(now, int) and now < then:
        return ("the active document holds {0} features and this run left {1} - it is not the world "
                "the earlier acts built. Start a new run id.".format(now, then))
    return None


def _valid_session_handle(value):
    return (isinstance(value, str) and value.startswith("session:")
            and len(value) > len("session:")
            and all(not character.isspace() for character in value))


def _invalidate_pin(document_pin, reason):
    document_pin.update(state="unknown", handle=None, snapshot=None,
                        blocked_transition=reason)


def _remember_document(document_pin, snapshot, *aliases):
    handle = snapshot.get("document_handle") if isinstance(snapshot, dict) else None
    if not _valid_session_handle(handle):
        return
    known = document_pin.setdefault("known_documents", {})
    known[handle] = handle
    for alias in (snapshot.get("document_id"),) + aliases:
        if not isinstance(alias, str) or not alias.strip():
            continue
        if alias not in known:
            known[alias] = handle
        elif known[alias] != handle:
            known[alias] = None


def _adopt_snapshot(document_pin, snapshot, *aliases):
    handle = snapshot.get("document_handle") if isinstance(snapshot, dict) else None
    if snapshot.get("state") != "known" or not _valid_session_handle(handle):
        _invalidate_pin(document_pin, "the intended document identity did not read back")
        return False
    document_pin.update(state="known", handle=handle, snapshot=snapshot,
                        blocked_transition=False)
    _remember_document(document_pin, snapshot, *aliases)
    return True


def _pin_snapshot(document_pin):
    if document_pin.get("blocked_transition"):
        return document_pin.get("snapshot") or {"state": "unknown"}
    retained = document_pin.get("handle")
    snapshot = facade("_document_now")()
    if snapshot.get("state") == "known":
        if (_valid_session_handle(retained)
                and snapshot.get("document_handle") != retained):
            document_pin["blocked_transition"] = "the active document no longer matches the pin"
        else:
            _adopt_snapshot(document_pin, snapshot)
    elif snapshot.get("state") == "none" and not _valid_session_handle(retained):
        document_pin.update(snapshot=snapshot, state="none", handle=None,
                            blocked_transition=False)
    elif _valid_session_handle(retained):
        document_pin["blocked_transition"] = "the active document identity is unreadable"
    else:
        _invalidate_pin(document_pin, "the active document identity is unreadable")
    return snapshot


def _refresh_pin(document_pin):
    """Refresh observations only while the exact retained document is still active."""
    expected = document_pin.get("handle")
    if document_pin.get("blocked_transition") or not _valid_session_handle(expected):
        return "the document pin is unresolved"
    snapshot = facade("_document_now")()
    if snapshot.get("state") != "known" or snapshot.get("document_handle") != expected:
        document_pin["blocked_transition"] = "the active document no longer matches the pin"
        return document_pin["blocked_transition"]
    document_pin["snapshot"] = snapshot
    _remember_document(document_pin, snapshot)
    return None


def _complete_open_documents():
    """Return a complete doc_get census and active handle, or a refusal."""
    is_error, payload = facade("call")("doc_get", {})
    if is_error or not isinstance(payload, dict):
        return None, None, "doc_get did not return a document census"
    rows = payload.get("open_documents")
    count = payload.get("open_count")
    if (payload.get("truncated") is not False or not isinstance(rows, list)
            or not isinstance(count, int) or isinstance(count, bool) or count != len(rows)
            or any(not isinstance(row, dict)
                   or not _valid_session_handle(row.get("document_handle"))
                   or not isinstance(row.get("open_index"), int)
                   or isinstance(row.get("open_index"), bool) for row in rows)):
        return None, None, "doc_get did not return a complete exact-handle census"
    active = payload.get("active")
    active_handle = active.get("document_handle") if isinstance(active, dict) else None
    active_rows = [row for row in rows if row.get("is_active") is True]
    if (not _valid_session_handle(active_handle)
            or len(active_rows) != 1
            or active_rows[0]["document_handle"] != active_handle):
        return None, None, "doc_get did not identify the active document exactly"
    return rows, active_handle, None


def _resolve_document_target(document_pin, target):
    """Resolve a transition target to one exact session handle without adopting it."""
    if _valid_session_handle(target):
        return target, None
    if not isinstance(target, str) or not target.strip():
        return None, "the document target is empty"
    known = document_pin.get("known_documents") or {}
    if _valid_session_handle(known.get(target)):
        return known[target], None
    rows, _active, problem = _complete_open_documents()
    if problem:
        return None, problem
    if re.fullmatch(r"open:[0-9]+", target):
        index = int(target.split(":", 1)[1])
        matches = [row for row in rows if row.get("open_index") == index]
    else:
        matches = [row for row in rows if row.get("name") == target]
    handles = {row["document_handle"] for row in matches}
    if len(handles) != 1:
        return None, "document target %r did not resolve to one exact open handle" % target
    return handles.pop(), None


def _confirm_transition(document_pin, expected, *aliases, polls=12):
    """Settle an accepted transition through bounded reads of its intended handle."""
    for attempt in range(polls):
        snapshot = facade("_document_now")()
        if (snapshot.get("state") == "known"
                and snapshot.get("document_handle") == expected):
            _adopt_snapshot(document_pin, snapshot, *aliases)
            return None
        if attempt + 1 < polls:
            time.sleep(0.25)
    _invalidate_pin(document_pin, "the intended document did not become active")
    return document_pin["blocked_transition"]


def _observe_doc_get(document_pin, payload):
    """Refresh a same-pin doc_get row, or block on unreadable or changed identity."""
    if document_pin.get("blocked_transition"):
        return document_pin["blocked_transition"]
    if document_pin.get("state") != "known" or not _valid_session_handle(document_pin.get("handle")):
        return None
    active = payload.get("active") if isinstance(payload, dict) else None
    observed = active.get("document_handle") if isinstance(active, dict) else None
    if observed != document_pin["handle"]:
        document_pin["blocked_transition"] = (
            "doc_get observed a different or unreadable active document")
        return document_pin["blocked_transition"]
    snapshot = dict(document_pin.get("snapshot") or {})
    snapshot.update(name=active.get("name"), document_id=active.get("document_id"),
                    document_handle=observed, state="known")
    document_pin["snapshot"] = snapshot
    _remember_document(document_pin, snapshot)
    return None


_NO_EXPECT = object()


def _shoot(label, out_dir, seq, expected_document=_NO_EXPECT):
    """Capture the CURRENT view to out_dir. Used by --shots after each framing row: the frame ratio
    being arithmetically right is not evidence the view shows the thing - only looking is. Returns
    the path written, or None (a shot that fails is never worth failing the sweep over)."""
    call = facade("call")
    safe_label = re.sub(r"[^A-Za-z0-9_.-]", "_", label)[:60]
    path = os.path.join(out_dir, f"{seq:04d}_{safe_label}.png")
    if expected_document is _NO_EXPECT:
        is_error, _ = call("view_screenshot", {"width": 640, "height": 460,
                                                "file_path": path})
    elif not expected_document:
        return None
    else:
        is_error, _ = call("view_screenshot", {"width": 640, "height": 460,
                                                "file_path": path,
                                                "expect_document": expected_document})
    return None if is_error else path


_UPLOAD_SETTLE_POLLS = 12
_UPLOAD_SETTLE_GAP_S = 5.0


def _settle_upload_status(call, arguments):
    """Return one exact upload's final response and dispatch count after bounded reads."""
    requested = arguments.get("handle")
    if (not isinstance(requested, str) or not requested.strip()
            or requested != requested.strip() or requested.lower() == "latest"):
        return True, "upload settle requires a nonempty exact upload handle", 0
    for poll in range(_UPLOAD_SETTLE_POLLS):
        if poll:
            time.sleep(_UPLOAD_SETTLE_GAP_S)
        try:
            is_error, current = call("data_get_upload_status", dict(arguments))
        except Exception as exc:
            return True, "upload %r status read raised: %s" % (requested, exc), poll + 1
        if is_error:
            return True, "upload %r status read failed: %s" % (requested, current), poll + 1
        if not isinstance(current, dict):
            return True, "upload %r status payload is not an object" % requested, poll + 1
        returned = current.get("handle")
        if returned != requested:
            return True, "upload handle %r does not match requested %r" % (returned, requested), poll + 1
        state = current.get("state")
        if state == "complete":
            return False, current, poll + 1
        if state == "failed":
            return True, "upload %r reported failed: %s" % (requested, current), poll + 1
        if state not in ("uploading", "processing"):
            return True, "upload %r has unknown state %r" % (requested, state), poll + 1
    return True, "upload %r did not settle after %s reads; last state %r" % (
        requested, _UPLOAD_SETTLE_POLLS, state), _UPLOAD_SETTLE_POLLS


def run_steps(steps, ctx, trace=False, sleep_s=STEP_SLEEP_S, on_result=None, timings=None,
              shots_dir=None, act="", document_pin=None, guarded_tools=None):
    """Run and judge one sequence while guarding writes to one exact document."""
    call = facade("call")
    rows = []
    transitions = {"doc_new", "doc_open", "doc_activate", "doc_close"}
    guarded_tools = guarded_tools or set()
    document_pin = document_pin if document_pin is not None else {
        "state": "unknown", "handle": None, "known_documents": {}}
    if (guarded_tools and document_pin.get("snapshot") is None
            and not document_pin.get("blocked_transition")):
        _pin_snapshot(document_pin)

    def block(tool, note):
        row = (tool, "blocked", note)
        rows.append(row)
        if on_result:
            on_result(*row)

    for step in steps:
        if _leaves_no_row(step):
            time.sleep(step[1]["seconds"])
            continue
        tool, args, expect, save = step
        expect = _unparked(expect)
        deliberate_refusal = isinstance(expect, _Refusal) or expect == "refused"
        try:
            arguments = args(ctx) if callable(args) else dict(args)
        except KeyError as e:
            block(tool, str(e))
            continue

        authored_value = arguments.get("expect_document")
        authored_expect = (isinstance(authored_value, str) and bool(authored_value.strip()))
        authored_refusal = authored_expect and deliberate_refusal
        if not authored_expect:
            arguments.pop("expect_document", None)

        intended_handle = None
        recovery = False
        pre_call_handle = document_pin.get("handle")
        transition_aliases = ()
        if tool in guarded_tools:
            blocked_reason = document_pin.get("blocked_transition")
            if blocked_reason and not authored_refusal:
                explicit_recovery = (
                    blocked_reason == "the active document was closed"
                    and tool in ("doc_activate", "doc_close")
                    and isinstance(arguments.get("name"), str)
                    and bool(arguments["name"].strip())
                    and not arguments.get("close_all"))
                if not explicit_recovery:
                    block(tool, "document pin unresolved ({0}); refusing the write before dispatch"
                          .format(blocked_reason))
                    continue
                intended_handle, problem = _resolve_document_target(
                    document_pin, arguments["name"])
                census, active_handle, census_problem = _complete_open_documents()
                if (problem or census_problem or intended_handle not in
                        {row["document_handle"] for row in (census or [])}):
                    block(tool, "document recovery target is unconfirmed: {0}".format(
                        problem or census_problem or "the exact target is not open"))
                    continue
                arguments["name"] = intended_handle
                if authored_expect and not deliberate_refusal and authored_value != active_handle:
                    block(tool, "authored expect_document does not match the recovery guard")
                    continue
                if not authored_refusal:
                    arguments["expect_document"] = active_handle
                recovery = True
            else:
                if document_pin.get("snapshot") is None:
                    _pin_snapshot(document_pin)
                if document_pin.get("blocked_transition") and not authored_refusal:
                    block(tool, "document pin unresolved ({0}); refusing the write before dispatch"
                          .format(document_pin["blocked_transition"]))
                    continue
                if tool in ("doc_activate", "doc_close") and arguments.get("name") and not deliberate_refusal:
                    intended_handle, problem = _resolve_document_target(
                        document_pin, arguments["name"])
                    if problem:
                        _invalidate_pin(document_pin, problem)
                        block(tool, "document transition target is unconfirmed: " + problem)
                        continue
                    arguments["name"] = intended_handle
                if not authored_refusal:
                    retained = document_pin.get("handle")
                    if authored_expect and authored_value != retained:
                        block(tool, "authored expect_document does not match the retained document pin")
                        continue
                    if (_valid_session_handle(retained)
                            and not document_pin.get("blocked_transition")):
                        arguments["expect_document"] = retained
                    elif (document_pin.get("state") == "none"
                          and tool in ("doc_new", "doc_open") and not authored_expect):
                        pass
                    else:
                        state = document_pin.get("state", "unknown")
                        block(tool, "document pin unavailable ({0}); refusing the write before dispatch"
                              .format(state))
                        continue

        if (tool in guarded_tools and tool == "doc_activate"
                and intended_handle is None and not deliberate_refusal):
            intended_handle, problem = _resolve_document_target(
                document_pin, arguments.get("name"))
            if problem:
                _invalidate_pin(document_pin, problem)
                block(tool, "document transition target is unconfirmed: " + problem)
                continue
            arguments["name"] = intended_handle
        elif tool in guarded_tools and tool == "doc_open":
            transition_aliases = (arguments.get("file_id"),)

        print(f"    -> {tool} {act}".rstrip()
              + (f" {json.dumps(arguments)[:120]}" if trace else ""), flush=True)
        t0 = time.time()
        if tool == "data_get_upload_status":
            is_error, payload, calls = _settle_upload_status(call, arguments)
        else:
            is_error, payload = call(tool, arguments)
            calls = 1
        if timings is not None:
            spent, count = timings.get(tool, (0.0, 0))
            timings[tool] = (spent + time.time() - t0, count + calls)

        identity_problem = None
        if tool == "doc_get" and not deliberate_refusal:
            if is_error:
                _invalidate_pin(document_pin, "doc_get could not read the active document identity")
                identity_problem = document_pin["blocked_transition"]
            else:
                identity_problem = _observe_doc_get(document_pin, payload)

        if identity_problem:
            status, note = "blocked", identity_problem
        elif isinstance(expect, _Refusal):
            if not is_error:
                status, note = "FAIL", f"expected a refusal, got ok: {str(payload)[:NOTE_MAX]}"
            else:
                absent = expect.missing(str(payload))
                if absent:
                    status, note = "FAIL", f"refusal missing {absent}: {str(payload)[:NOTE_MAX]}"
                else:
                    status, note = "expected-refusal", str(payload)[:REFUSAL_NOTE_MAX]
        elif callable(expect):
            if is_error:
                status, note = "FAIL", str(payload)[:NOTE_MAX]
            else:
                try:
                    good = bool(expect(payload))
                except Exception as e:
                    good, payload = False, f"predicate raised: {e}"
                status, note = ("pass", "") if good else ("FAIL", str(payload)[:NOTE_MAX])
        elif expect == "ok" and not is_error:
            status, note = "pass", ""
        elif expect == "refused" and is_error:
            status, note = "expected-refusal", str(payload)[:REFUSAL_NOTE_MAX]
        else:
            status, note = "FAIL", str(payload)[:NOTE_MAX]

        if tool in guarded_tools and tool in transitions:
            if status == "expected-refusal":
                pass
            elif status != "pass":
                _invalidate_pin(document_pin, "document transition did not pass")
            elif tool in ("doc_new", "doc_open"):
                expected = payload.get("document_handle") if isinstance(payload, dict) else None
                if not _valid_session_handle(expected):
                    problem = "the transition returned no exact document handle"
                else:
                    problem = _confirm_transition(
                        document_pin, expected, *transition_aliases)
                if problem:
                    if not document_pin.get("blocked_transition"):
                        _invalidate_pin(document_pin, problem)
                    status, note = "blocked", problem
            elif tool == "doc_activate":
                problem = (_confirm_transition(document_pin, intended_handle)
                           if _valid_session_handle(intended_handle)
                           else "the activation target was not exact")
                if problem:
                    if not document_pin.get("blocked_transition"):
                        _invalidate_pin(document_pin, problem)
                    status, note = "blocked", problem
            elif tool == "doc_close":
                target = intended_handle
                closes_active = bool(arguments.get("close_all")) or not arguments.get("name")
                closes_active = closes_active or target == pre_call_handle
                if target:
                    known = document_pin.get("known_documents") or {}
                    document_pin["known_documents"] = {
                        alias: handle for alias, handle in known.items() if handle != target}
                if closes_active or recovery:
                    _invalidate_pin(document_pin, "the active document was closed")

        if status == "pass" and save is not None and not is_error:
            key, extract = save
            try:
                ctx[key] = extract(payload)
            except Exception as e:
                status, note = "pass*", f"saved-value extraction failed: {e}"

        rows.append((tool, status, note))
        if on_result:
            on_result(*rows[-1])
        if shots_dir and tool == "view_set" and status == "pass" and arguments.get("focus"):
            focus = arguments["focus"]
            expected = (document_pin.get("handle")
                        if not document_pin.get("blocked_transition") else None)
            _shoot("-".join(focus) if isinstance(focus, list) else str(focus),
                   shots_dir, len(rows), expected_document=expected)
        if sleep_s:
            time.sleep(sleep_s)
    return rows


def judged_steps(steps):
    """The steps run_steps produces a ROW for, in order - which is what pairs positionally with
    those rows. A row-less step (today only a _dwell, which holds the view and is judged by
    nothing) would otherwise shift the pairing: every expectation after it gets credited to a LATER
    step's tool, so a bare "ok" reads as covered and a value predicate is lost."""
    return [s for s in steps if not _leaves_no_row(s)]


def _precondition_holds(pre):
    """Run an act's precondition READ; True when it returns without error (the geometry the act's
    narrative consumes exists). A False routes the act to its scratch fallback."""
    tool, args = pre
    is_error, _ = facade("call")(tool, args)
    return not is_error


def _one_act(names, selector):
    """The ONE act `selector` names: the act whose name IS it, or whose name continues it at a word
    break - so 'ACT 10b' is not 'ACT 10b2'. Anything else raises, naming every act."""
    hits = [n for n in names if n == selector or n.startswith(selector + " ")]
    if len(hits) != 1:
        raise ValueError("--acts selector {0!r} names {1} acts, not one - the acts are: {2}".format(
            selector, len(hits), ", ".join(names)))
    return hits[0]


def select_acts(acts, spec):
    """The act names `--acts <spec>` selects, in ACTS order: a comma list of selectors, each an act
    name as printed in the log or a 'FIRST..LAST' range spanning two of them (either way round).
    Raises ValueError naming every act when a selector names other than one act, or when the whole
    spec selects none."""
    names = [act[0] for act in acts]
    chosen = set()
    for item in (part.strip() for part in spec.split(",")):
        if not item:
            continue
        if ".." in item:
            lo, hi = [_one_act(names, s.strip()) for s in item.split("..", 1)]
            a, b = names.index(lo), names.index(hi)
            chosen.update(names[min(a, b):max(a, b) + 1])
        else:
            chosen.add(_one_act(names, item))
    if not chosen:
        raise ValueError("--acts {0!r} selects no act - the acts are: {1}".format(
            spec, ", ".join(names)))
    return [name for name in names if name in chosen]


def run(write_json, keep_open=False, trace=False, shots_dir=None, acts_spec=None,
        run_id=None, resume=False):
    """Walk the act program and stamp the receipt when the whole of it has run.

    'run_id' makes the walk RESUMABLE: the ctx, the ledger so far and the acts already done are
    saved after every act, and a later invocation with resume=True carries on from there against the
    document this chunk leaves open. The receipt is written from the UNION of a run id's chunks, and
    only when every act of the program has run under it."""
    # The wire reads, the act program and the ledger tables as the FACADE holds them at the moment
    # the run starts - see verify_core.facade for why they are not this module's own globals.
    health_gate, registered_tools = facade("health_gate"), facade("registered_tools")
    attestation_identity = facade("attestation_identity")
    source_hash, write_verified = facade("source_hash"), facade("write_verified")
    poll_generation, reload_smoke = facade("poll_generation"), facade("reload_smoke")
    ACTS, POLL_AFTER, STORY, EXCLUDED = (facade("ACTS"), facade("POLL_AFTER"), facade("STORY"),
                                         facade("EXCLUDED"))
    ACT_NEEDS = facade("ACT_NEEDS")

    # --acts: a SLICE of the story, walked against the document a prior --keep-open run left open.
    # Selected before the first wire call, so an unknown selector costs nothing.
    selected = None
    if acts_spec is not None:
        try:
            selected = select_acts(ACTS, acts_spec)
        except ValueError as e:
            print(str(e))
            return 1
        skipped = [act[0] for act in ACTS if act[0] not in set(selected)]
        print("partial run (--acts {0}): {1} act(s) not run - {2}.{3}".format(
            acts_spec, len(skipped), ", ".join(skipped) or "(none)",
            "" if run_id else " The ctx values they save are absent, so a step whose arguments "
            "need one lands blocked."))
    # THE RESUME: the state a prior chunk of this run id saved. Both refusals are settled before the
    # first act runs - a chunk that cannot be joined to the ones before it must change nothing.
    # With --acts it is a DEVELOPMENT walk instead: the named acts run again against the saved
    # world with its ctx, nothing is stamped and the state is not advanced.
    develop = bool(run_id and resume and acts_spec is not None)
    state = load_run_state(run_id) if (run_id and resume) else None
    # This run is bound to one working-tree snapshot; this does not identify loaded Fusion code.
    pinned_source_hash = source_hash()
    # A run id that already holds state is CONTINUED, never restarted over: starting it again
    # would run the acts with an empty ctx (every saved value gone) and rewrite its progress.
    if run_id and not resume and load_run_state(run_id) is not None:
        print("run refused: {0!r} already holds state at {1} - pass --resume to continue it "
              "(--resume --acts for a development walk), or start a new run id".format(
                  run_id, run_state_path(run_id)))
        return 1
    health = health_gate()
    pinned_attestation = attestation_identity(health)
    if pinned_attestation is None:
        print("run refused: health did not provide a complete loaded attestation")
        return 1
    print(f"server ok: {health.get('server')} v{health.get('version', '?')}")

    def current_attestation():
        return attestation_identity(health_gate())

    current_document = _document_now() if run_id and resume else None
    if run_id and resume:
        refusal = (develop_refusal(state, run_id, current_document) if develop
                   else resume_refusal(state, run_id, pinned_source_hash, current_document,
                                       pinned_attestation))
        if refusal:
            print("resume refused: " + refusal)
            return 1
        if develop:
            print("development walk of run {0}: the named acts run again against its world with "
                  "its saved ctx - no receipt, state not advanced{1}".format(
                      run_id, "" if state.get("source_hash") == pinned_source_hash
                      else " (the source moved since the run's first chunk)"))
        else:
            print("resuming run {0}: {1} act(s) already done".format(
                run_id, len(state.get("acts_done") or [])))
    all_tools, registry_rows = registered_tools(health, include_rows=True)
    guarded_tools = frozenset(
        row["name"] for row in registry_rows
        if isinstance(row.get("annotations"), dict)
        and row["annotations"].get("readOnlyHint") is not True
        and "expect_document" in ((row.get("inputSchema") or {}).get("properties") or {}))

    # THE CAPABILITY TIER: every capability an act or a step declares, answered True/False/None by
    # its own probe. An unmet capability routes its acts and steps to the receipt's
    # skipped(<capability> not entitled) bucket rather than running them into the refusal an
    # unentitled installation would answer with.
    entitlements = {}

    def met(capability):
        """capability_met, taking the capability's own probe the first time it is asked for - workspace_orient errors with no document open."""
        if capability is not None and capability not in entitlements:
            entitlements.update(probe_capabilities([capability]))
            state = entitlements[capability]
            print("capability {0}: {1}".format(
                capability, "entitled" if state is True
                else ("NOT entitled" if state is False else "unreadable (routed as not entitled)")))
        return capability_met(entitlements, capability)

    # Everything a later chunk needs is seeded from the saved state, so the acts below cannot tell
    # whether the ones before them ran in this process or the last one.
    prior = state or {}
    ctx = dict(prior.get("ctx") or {})
    rows = [tuple(r) for r in (prior.get("rows") or [])]
    notes = dict(prior.get("notes") or {})
    act_modes = [tuple(m) for m in (prior.get("act_modes") or [])]
    acts_done = list(prior.get("acts_done") or [])
    # tool -> the capability reason the tier held its every step back with, for the ledger.
    gated = dict(prior.get("gated") or {})
    timings = {t: tuple(v) for t, v in (prior.get("timings") or {}).items()}
    act_seconds = [tuple(a) for a in (prior.get("act_seconds") or [])]
    run_started = time.time()
    chunk_started = prior.get("elapsed_s") or 0.0
    # the tools whose PASSING step read a value off the payload - run_steps returns exactly one row
    # per step, in order, so a row is paired back with the expectation that judged it.
    valued = set(prior.get("valued") or [])
    # tool -> the reason a Parked step held it at a bare "ok", for the ledger's status column.
    parked = dict(prior.get("parked") or {})
    # the values predicates recall by name across acts, which live in verify_core rather than in ctx.
    _RECALL.update(prior.get("recall") or {})
    entitlements.update(prior.get("entitlements") or {})
    # the document this chunk works in, and how many acts it drove itself - a chunk that drove none
    # produced no evidence of its own, whatever the saved ledger says.
    document = prior.get("document") or current_document or {
        "name": None, "document_id": None, "document_handle": None,
        "feature_count": None, "state": "unknown"}
    document_pin = {
        "state": document.get("state", "known" if document.get("document_handle") else "unknown"),
        "handle": document.get("document_handle"),
        "snapshot": document if prior.get("document") or current_document else None,
        "blocked_transition": False,
        "known_documents": dict(prior.get("document_handles") or {})}
    _remember_document(document_pin, document)
    if (guarded_tools and document_pin.get("snapshot") is None
            and not document_pin.get("blocked_transition")):
        _pin_snapshot(document_pin)
    walked = 0
    program = [act[0] for act in ACTS]

    def checkpoint(allow_terminal_close=False):
        nonlocal document
        if document_pin.get("snapshot") is None and not document_pin.get("blocked_transition"):
            _pin_snapshot(document_pin)
        if document_pin.get("blocked_transition"):
            if (allow_terminal_close
                    and document_pin["blocked_transition"] == "the active document was closed"):
                return True
            print("run refused: document pin unresolved ({0}); no state written".format(
                document_pin["blocked_transition"]))
            return False
        problem = _refresh_pin(document_pin)
        if problem:
            print("run refused: " + problem + "; no state written")
            return False
        document = document_pin["snapshot"]
        save_run_state(run_id, {
            "run": run_id, "source_hash": pinned_source_hash,
            "attestation": pinned_attestation, "acts_done": acts_done,
            "rows": [list(r) for r in rows], "notes": notes,
            "act_modes": [list(m) for m in act_modes], "valued": sorted(valued),
            "parked": parked, "gated": gated, "entitlements": entitlements,
            "ctx": ctx, "recall": dict(_RECALL),
            "timings": {t: list(v) for t, v in timings.items()},
            "act_seconds": [list(a) for a in act_seconds],
            "elapsed_s": chunk_started + (time.time() - run_started),
            "document": document,
            "document_handles": dict(document_pin.get("known_documents") or {})})
        return True

    for name, pre, narrative, fallback in ACTS:
        if name in acts_done and not develop:
            continue
        if selected is not None and name not in selected:
            continue
        # An act the tier holds back runs NOTHING - not even its precondition read, which would
        # cost a wire call to decide between two lanes neither of which may run.
        act_cap = ACT_NEEDS.get(name)
        if not met(act_cap):
            reason = capability_skip_reason(act_cap, entitlements)
            act_modes.append((name, "skipped(" + reason + ")"))
            print(f"\n-- {name} [skipped: {reason}] --")
            # BOTH lanes: neither ran, and a tool driven only by the fallback belongs in the
            # capability bucket too - left out, it reads as PENDING, which is a different claim.
            for step in judged_steps(list(narrative) + list(fallback or [])):
                gated.setdefault(step[0], reason)
            # a held-back act is DONE - it has its outcome, and a resume must not run it again
            if name not in acts_done:
                acts_done.append(name)
            walked += 1
            if not develop:
                if source_hash() != pinned_source_hash:
                    print("run refused: source changed during this run; no state or receipt written")
                    return 1
                identity_problem = _identity_refusal(pinned_attestation, current_attestation())
                if identity_problem:
                    print("run refused: " + identity_problem + "; no state or receipt written")
                    return 1
                if run_id and not checkpoint(
                        allow_terminal_close=set(program) <= set(acts_done)):
                    return 1
            continue
        mode, steps = "narrative", narrative
        if pre is not None and fallback is not None and not _precondition_holds(pre):
            mode, steps = "fallback", fallback
        act_modes.append((name, mode))
        print(f"\n-- {name} [{mode}] --")
        act_started = time.time()
        if keep_open and name == "FINALE":
            steps = [s for s in steps if s[0] != "doc_close"]
        # ...then the per-STEP half of the same tier: the unmet rows are dropped BEFORE the step
        # engine, so the by-position pairing between judged_steps and its rows is untouched.
        for step in judged_steps(steps):
            cap = step_capability(step[2])
            if not met(cap):
                gated.setdefault(step[0], capability_skip_reason(cap, entitlements))
        steps = [s for s in steps if met(step_capability(s[2]))]
        # judged_steps, not the act's raw list, is what pairs with the rows below - see its
        # docstring for what a dwell does to the pairing.
        for step, (tool, status, note) in zip(judged_steps(steps),
                                              run_steps(steps, ctx, trace=trace, timings=timings,
                                                        shots_dir=shots_dir, act=name,
                                                        document_pin=document_pin,
                                                        guarded_tools=guarded_tools)):
            rows.append((tool, status, note))
            if status in ("pass", "pass*") and predicate_kind(step[2]) == "value":
                valued.add(tool)
            if status in ("pass", "pass*") and parked_reason(step[2]) is not None:
                parked[tool] = parked_reason(step[2])
            if status in ("pass", "pass*", "expected-refusal"):
                story = STORY.get(tool, "")
                notes[tool] = (story + " (fallback fixture)").strip() if mode == "fallback" else story
        if name in POLL_AFTER:
            # one act can leave SEVERAL setups generating, so the boundary poll takes a list as
            # readily as a name and certifies each in turn. 'max_polls' sizes that act's own budget
            # to the families it launched, and is passed only where the act states one.
            targets = POLL_AFTER[name][mode]
            budget = {k: v for k, v in POLL_AFTER[name].items() if k == "max_polls"}
            for setup in ([targets] if isinstance(targets, str) else targets):
                poll_generation(rows, notes, setup, valued=valued, **budget)
        if document_pin.get("snapshot") is not None:
            document = document_pin["snapshot"]
        elif document_pin.get("state") != "known":
            document = dict(document, document_handle=None, state=document_pin.get("state"))
        act_seconds.append((name, time.time() - act_started))
        if name not in acts_done:
            acts_done.append(name)
        walked += 1
        # THE CHUNK BOUNDARY: an act is the unit a resume restarts from, so the state is saved here
        # - after the act's own boundary poll, with everything a later act reads.
        if not develop and source_hash() != pinned_source_hash:
            print("run refused: source changed during this run; no state or receipt written")
            return 1
        identity_problem = _identity_refusal(pinned_attestation, current_attestation())
        if not develop and identity_problem:
            print("run refused: " + identity_problem + "; no state or receipt written")
            return 1
        if run_id and not develop and not checkpoint(
                allow_terminal_close=set(program) <= set(acts_done)):
            return 1

    # A run is COMPLETE when every act of the program has run under this id - in this chunk or an
    # earlier one. Only a complete run stamps, and only a complete run fires the reload beat.
    complete = not develop and set(program) <= set(acts_done)

    # THE RELOAD BEAT, after every act: it restarts the server, so no step can be dispatched
    # afterwards and no act can hold it. It appends its own row rather than running through the
    # step engine, because what it has to judge is a reconnect, not one wire call (reload_smoke).
    if complete and not develop and source_hash() != pinned_source_hash:
        print("run refused: source changed during this run; no receipt written")
        return 1
    pre_reload_failures = [row for row in rows
                           if row[1] in ("FAIL", "blocked", "pass*")]
    final_attestation = pinned_attestation
    if complete and not pre_reload_failures:
        reloaded = reload_smoke(rows, notes, valued=valued,
                                expected_attestation=pinned_attestation)
        if reloaded is None:
            print("run refused: reload did not prove the expected loaded identity")
            return 1
        if reloaded is not None:
            final_attestation = reloaded
        if not develop and source_hash() != pinned_source_hash:
            print("run refused: source changed during reload; no receipt written")
            return 1
    if keep_open:
        print("\n--keep-open: the story document is left open for inspection.")

    # covered demands at least one step that PRODUCED something (pass/pass*): a tool whose every
    # step is an expected-refusal exercised only its guards - no effect existed to read back, so
    # calling that "covered" would let the legend lie. Those rows get their own bucket.
    # covered vs called splits the produced-something set again: covered means a value predicate
    # read the effect off the payload, called means every passing step was a bare "ok".
    passed = {t for t, s, _ in rows if s in ("pass", "pass*")}
    refused_only = {t for t, s, _ in rows if s == "expected-refusal"} - passed
    ledger = []
    for tool in all_tools:
        if tool in valued:
            ledger.append((tool, "covered"))
        elif tool in passed:
            # a parked tool stays in the CALLED bucket and carries why it was held there, so the
            # reason is on the receipt instead of in a source comment only.
            ledger.append((tool, "called ({0})".format(parked[tool]) if tool in parked else "called"))
        elif tool in refused_only:
            ledger.append((tool, "refusals-only: every step is a guard refusal - no effect was "
                                 "produced or read back this run"))
        elif tool in EXCLUDED:
            ledger.append((tool, f"skipped: {EXCLUDED[tool]}"))
        elif tool in gated:
            # the CAPABILITY tier's own bucket: every step driving this tool declared an
            # entitlement the capability probe did not read as granted, so none of them ran.
            ledger.append((tool, f"skipped: {gated[tool]}"))
        else:
            ledger.append((tool, "PENDING (no step yet)"))

    print(f"\n== step results ({len(rows)}):")
    for tool, status, note in rows:
        print(f"  {status:18} {tool:28} {note}")
    n_cov = sum(1 for _, s in ledger if s == "covered")
    n_called = sum(1 for _, s in ledger if s == "called" or s.startswith("called ("))
    n_pend = sum(1 for _, s in ledger if s.startswith("PENDING"))
    n_ref = sum(1 for _, s in ledger if s.startswith("refusals-only"))
    n_skip = len(ledger) - n_cov - n_called - n_pend - n_ref
    print(f"\n== ledger: {n_cov}/{len(ledger)} covered, {n_called} called (bare ok), "
          f"{n_ref} refusals-only, {n_skip} skipped(reason), {n_pend} pending")
    for tool, s in ledger:
        if s != "covered":
            print(f"  {tool:32} {s}")

    # WHERE THE SECONDS WENT. Time is what the sweep is OPTIMIZED from, never what it is capped by:
    # the run publishes the total, the slowest acts and the tools that spent the most wire time -
    # with a per-call average, which is what separates a tool that is CALLED a lot from a slow one.
    # A run that outgrows one 600 s shell call is walked in chunks (--run <id> / --resume), so a
    # long run still stamps its receipt instead of refusing one.
    chunk_elapsed = time.time() - run_started
    elapsed = chunk_started + chunk_elapsed
    print(f"\n== elapsed: {chunk_elapsed:.0f}s for {len(rows)} steps "
          f"({STEP_SLEEP_S * len(rows):.0f}s of it the inter-step sleep)"
          + (f"; {elapsed:.0f}s over this run's chunks" if chunk_started else ""))
    for nm, secs in sorted(act_seconds, key=lambda r: -r[1])[:5]:
        print(f"  {secs:7.1f}s  {nm}")
    print("  slowest tools (total / calls / per call):")
    for tool, (secs, count) in sorted(timings.items(), key=lambda kv: -kv[1][0])[:8]:
        print(f"  {secs:7.1f}s  {count:4} x {secs / max(count, 1):5.2f}s  {tool}")

    n_narr = sum(1 for _, m in act_modes if m == "narrative")
    n_fb = sum(1 for _, m in act_modes if m == "fallback")
    print(f"\n== acts: {n_narr} narrative / {n_fb} fallback (of {len(act_modes)})")
    for nm, m in act_modes:
        print(f"  {m:10} {nm}")

    # pass* blocks the receipt: the payload did not carry a key the step contract expected - a
    # payload-shape mismatch is a real signal, not a pass.
    fails = [r for r in rows if r[1] in ("FAIL", "blocked", "pass*")]
    # A run that walked only PART of the program judges the slice it walked: its ledger reads PENDING
    # for every tool the unrun acts drive, and the receipt would publish that as the tool surface's
    # coverage. The union of a run id's chunks is what makes the program whole again.
    if complete:
        if fails:
            print("\nVERIFIED_TOOLS.md NOT rewritten - resolve the FAIL/blocked/pass* steps first.")
        else:
            src_hash = pinned_source_hash
            stamp_date = time.strftime("%Y-%m-%d")
            fusion_version = ctx.get("fusion_version", "?")
            # WHAT THIS INVOCATION CONTRIBUTED, beside the stamp: a chunk can complete a run without
            # driving an act of its own (the last chunk died after its final boundary save), and a
            # receipt written from a saved ledger has to say that on the line that announces it.
            drove = (" - {0} acts over this run's chunks".format(len(acts_done))
                     if chunk_started else "")
            if run_id and walked == 0:
                drove += " - 0 acts driven this chunk - the reload beat only"
            print("\nwrote {0} (stamp: source {1}..., Fusion {2}, {3}){4}".format(
                write_verified(ledger, fusion_version, stamp_date, src_hash,
                               notes=notes, act_modes=act_modes, attestation=final_attestation),
                src_hash[:12], fusion_version, stamp_date, drove))
    if write_json:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        path = os.path.join(RESULTS_DIR, f"verify-{time.strftime('%Y%m%d-%H%M%S')}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"steps": rows, "ledger": ledger, "acts": act_modes, "server": health}, fh, indent=2)
        print(f"\nwrote {path}")
    if complete and run_id and not fails:
        # the id is spent: its acts have all run, so a later --resume of it would stamp from the
        # saved ledger without driving a step.
        save_run_state(run_id, dict(load_run_state(run_id) or {}, complete=True))
    if not complete:
        if develop:
            print("\ndevelopment walk of run {0}: {1} act(s) re-run against its world, receipt not "
                  "written, state not advanced; the document stays open".format(run_id, walked))
        elif run_id:
            # the document stays open for the chunk that carries on, and its identity is saved with
            # the state: a resume against a different one is refused rather than run.
            state_file = run_state_path(run_id)
            print("\nrun {0}: {1} of {2} acts done, receipt not written - carry on with "
                  "'--run {0} --resume' against the document left open ({3})".format(
                      run_id, len(acts_done), len(program), state_file))
        elif selected is not None:
            # nothing closes the document unless the FINALE was selected AND allowed to close it.
            print("\npartial run (--acts {0}): receipt not written{1}".format(
                acts_spec, "" if ("FINALE" in selected and not keep_open)
                else "; the story document is left open"))
        else:
            print("\nreceipt not written: {0} of {1} acts ran".format(len(acts_done), len(program)))
    return 1 if fails else 0
