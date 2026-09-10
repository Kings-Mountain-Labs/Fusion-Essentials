# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Durable pure-Python records for deferred drawing operations."""

import copy
from contextlib import contextmanager
import datetime
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid


_DEFAULT_STORE = None
_DEFAULT_STORE_LOCK = threading.Lock()


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _default_root():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "Fusion-Essentials" / "drawing-jobs"


def _copy(value):
    return copy.deepcopy(value)


class DrawingJobStore:
    """Persist drawing job records with transactional state changes."""

    def __init__(self, root=None, now=None):
        self.root = Path(root) if root is not None else _default_root()
        self.path = self.root / "drawing-jobs.sqlite3"
        self._now = now or _utc_now
        self._volatile = {}
        self._volatile_lock = threading.RLock()
        self.instance_id = uuid.uuid4().hex
        self._startup_error = None
        try:
            self._initialize_and_reconcile()
        except Exception as ex:
            self._startup_error = str(ex)

    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _transaction(self, write=False):
        connection = self._connect()
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write and connection.in_transaction:
                connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_and_reconcile(self):
        with self._transaction(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "request_key TEXT PRIMARY KEY, canonical_request TEXT NOT NULL, "
                "status TEXT NOT NULL, owner_instance TEXT NOT NULL, record_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS job_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            rows = connection.execute(
                "SELECT request_key, record_json FROM jobs "
                "WHERE status IN ('accepted','queued','running')"
            ).fetchall()
            for request_key, raw in rows:
                record = json.loads(raw)
                record["status"] = "unresolved"
                record["reason"] = "server_restarted"
                record["finished_at"] = self._now()
                record["updated_at"] = record["finished_at"]
                record["owner_instance"] = self.instance_id
                self._update_row(connection, record)
            connection.execute(
                "INSERT OR REPLACE INTO job_meta(key, value) VALUES ('current_owner', ?)",
                (self.instance_id,),
            )

    @staticmethod
    def _encode(record):
        return json.dumps(record, sort_keys=True, ensure_ascii=True)

    def _update_row(self, connection, record):
        connection.execute(
            "UPDATE jobs SET status = ?, owner_instance = ?, record_json = ? "
            "WHERE request_key = ?",
            (record["status"], record["owner_instance"], self._encode(record),
             record["request_key"]),
        )

    @staticmethod
    def _read_row(connection, request_key):
        row = connection.execute(
            "SELECT record_json FROM jobs WHERE request_key = ?", (request_key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _is_current_owner(self, connection):
        row = connection.execute(
            "SELECT value FROM job_meta WHERE key = 'current_owner'"
        ).fetchone()
        return row is not None and row[0] == self.instance_id

    def _remember_unresolved(self, request_key, reason, error, record=None, result=None):
        row = _copy(record) if isinstance(record, dict) else {"request_key": request_key}
        row["status"] = "unresolved"
        row["reason"] = reason
        row["persistence_error"] = str(error)
        row["finished_at"] = self._now()
        row["updated_at"] = row["finished_at"]
        if result is not None:
            row["terminal_result"] = _copy(result)
        with self._volatile_lock:
            self._volatile[request_key] = row

    def accept(self, request_key, tool_name, arguments):
        """Create or deduplicate an accepted job before any native dispatch."""
        if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 160:
            return None, False, "request_key must be a non-empty string of at most 160 characters"
        if self._startup_error is not None:
            return None, False, f"drawing job store is unavailable: {self._startup_error}"
        key = request_key.strip()
        canonical = json.dumps({"tool": tool_name, "arguments": arguments}, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=True)
        with self._volatile_lock:
            if key in self._volatile:
                return _copy(self._volatile[key]), False, (
                    "request_key has unresolved persistence state and cannot be dispatched")
            try:
                with self._transaction(write=True) as connection:
                    if not self._is_current_owner(connection):
                        return None, False, "drawing job store instance is stale"
                    existing = self._read_row(connection, key)
                    if existing is not None:
                        if existing.get("canonical_request") != canonical:
                            return existing, False, (
                                "request_key is already bound to different arguments")
                        return _copy(existing), False, None
                    now = self._now()
                    record = {
                        "job_id": uuid.uuid4().hex,
                        "request_key": key,
                        "tool": tool_name,
                        "arguments": _copy(arguments),
                        "expect_document": arguments.get("expect_document"),
                        "canonical_request": canonical,
                        "status": "accepted",
                        "accepted_at": now,
                        "updated_at": now,
                        "owner_instance": self.instance_id,
                    }
                    connection.execute(
                        "INSERT INTO jobs(request_key, canonical_request, status, owner_instance, "
                        "record_json) VALUES (?, ?, ?, ?, ?)",
                        (key, canonical, "accepted", self.instance_id, self._encode(record)),
                    )
                    return _copy(record), True, None
            except Exception as ex:
                return None, False, f"job acceptance could not be persisted: {ex}"

    def _change_state(self, request_key, expected_status, new_status, fields=None,
                      failure_reason=None, result=None):
        if self._startup_error is not None:
            return False
        record = None
        with self._volatile_lock:
            if request_key in self._volatile:
                return False
            try:
                with self._transaction(write=True) as connection:
                    if not self._is_current_owner(connection):
                        return False
                    record = self._read_row(connection, request_key)
                    if (record is None or record.get("owner_instance") != self.instance_id
                            or record.get("status") not in expected_status):
                        return False
                    record["status"] = new_status
                    record["updated_at"] = self._now()
                    if fields:
                        record.update(fields(record["updated_at"]))
                    self._update_row(connection, record)
                return True
            except Exception as ex:
                self._remember_unresolved(
                    request_key, failure_reason or "state_persistence_failed", ex, record, result)
                return False

    def claim_dispatch(self, request_key):
        """Atomically claim one accepted job for main-thread queueing."""
        return self._change_state(
            request_key, frozenset(("accepted",)), "queued",
            failure_reason="queue_persistence_failed")

    def mark_running(self, request_key):
        """Persist the claimed state before allowing the native callback to mutate."""
        return self._change_state(
            request_key, frozenset(("queued",)), "running",
            fields=lambda now: {"started_at": now},
            failure_reason="running_persistence_failed")

    def finish(self, request_key, result):
        """Persist the raw terminal tool result without inventing an effect verdict."""
        status = "failed" if isinstance(result, dict) and bool(result.get("isError")) else "completed"
        return self._change_state(
            request_key, frozenset(("running",)), status,
            fields=lambda now: {"finished_at": now, "terminal_result": _copy(result)},
            failure_reason="terminal_persistence_failed", result=result)

    def fail_unclaimed(self, request_key, reason):
        """Mark work known not to have been claimed by Fusion's main thread."""
        return self._change_state(
            request_key, frozenset(("accepted", "queued")), "failed",
            fields=lambda now: {"finished_at": now, "reason": reason},
            failure_reason="drop_persistence_failed")

    def get(self, request_key):
        """Return a detached snapshot, or None for an unknown key."""
        with self._volatile_lock:
            volatile = self._volatile.get(request_key)
            if volatile is not None:
                return _copy(volatile)
            if self._startup_error is not None:
                return {"request_key": request_key, "status": "unresolved",
                        "reason": "store_initialization_failed",
                        "persistence_error": self._startup_error}
            try:
                with self._transaction() as connection:
                    return _copy(self._read_row(connection, request_key))
            except Exception as ex:
                return {"request_key": request_key, "status": "unresolved",
                        "reason": "record_unreadable", "persistence_error": str(ex)}


def start_server_store(root=None, store=None):
    """Install a fresh store generation before one bound server begins serving."""
    global _DEFAULT_STORE
    current = store if store is not None else DrawingJobStore(root)
    with _DEFAULT_STORE_LOCK:
        _DEFAULT_STORE = current
    return current


def get_store():
    """Return this loaded server instance's shared drawing job store."""
    global _DEFAULT_STORE
    with _DEFAULT_STORE_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = DrawingJobStore()
        return _DEFAULT_STORE
