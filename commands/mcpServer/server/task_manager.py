# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.
#
# Adapted from Autodesk's Fusion MCP add-in sample (MIT-licensed).

"""Thread-safe task execution on Fusion's main thread.

The MCP HTTP server handles each request on a background thread, but the Fusion
API is only safe to touch from the main (UI) thread. TaskManager bridges the two
using a Fusion custom event: a worker thread posts a callback and fires the
event; Fusion delivers the event on the main thread, where the callback runs.

This is the single most important piece to get right - calling adsk.* from a
request thread can crash Fusion.
"""

import json
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

import adsk.core

from ....lib import fusion360utils as futil

app = adsk.core.Application.get()

# Custom event id, namespaced to this add-in so it can't collide with Autodesk's
# own MCP server or another add-in.
CUSTOM_EVENT_ID = 'GTF_Fusion-Essentials.MCP.TaskManagerEvent'

# A pending task should be claimed by notify() within seconds of being posted. An unclaimed entry
# can outlive its caller, so the next post reaps entries older than this TTL.
_PENDING_TASK_TTL_S = 300.0


class TaskManager:
    """Singleton that marshals callbacks onto Fusion's main thread via a custom event."""

    _instance = None
    _event_handler = None
    _custom_event = None
    # Posted from worker (request) threads and consumed (read + deleted) on Fusion's
    # main thread when the custom event fires. Both sides take _tasks_lock - this is
    # the single most concurrency-sensitive object in the server.
    _pending_tasks: Dict[str, Dict[str, Any]] = {}
    _tasks_lock = threading.Lock()
    _lifecycle_lock = threading.Lock()
    _is_running = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(TaskManager, cls).__new__(cls)
        return cls._instance

    @classmethod
    def start(cls) -> bool:
        if not app:
            futil.log('TaskManager.start: phase=application_check Fusion is unavailable')
            return False
        with cls._lifecycle_lock:
            with cls._tasks_lock:
                if cls._is_running:
                    return True
                try:
                    try:
                        app.unregisterCustomEvent(CUSTOM_EVENT_ID)
                    except Exception:
                        pass
                    cls._custom_event = app.registerCustomEvent(CUSTOM_EVENT_ID)
                    cls._event_handler = TaskEventHandler(cls._pending_tasks)
                    cls._custom_event.add(cls._event_handler)
                    cls._is_running = True
                    futil.log('TaskManager: started')
                    return True
                except Exception:
                    futil.handle_error('TaskManager.start: phase=event_registration')
                    return False

    @classmethod
    def stop(cls) -> bool:
        with cls._lifecycle_lock:
            with cls._tasks_lock:
                if not cls._is_running:
                    return True
                cls._is_running = False
                custom_event = cls._custom_event
                event_handler = cls._event_handler
                cls._event_handler = None
                cls._custom_event = None
                dropped = list(cls._pending_tasks.values())
                cls._pending_tasks.clear()
            for task in dropped:
                cls._notify_drop(task, "task_manager_stopped_before_claim")
            try:
                if custom_event and event_handler:
                    custom_event.remove(event_handler)
                try:
                    app.unregisterCustomEvent(CUSTOM_EVENT_ID)
                except Exception:
                    pass
                futil.log('TaskManager: stopped')
                return True
            except Exception:
                futil.handle_error('TaskManager.stop: phase=event_teardown')
                return False

    @classmethod
    def post(cls, command: str, callback: Callable[[Dict[str, Any]], None],
             data: Dict[str, Any], on_drop=None) -> Optional[str]:
        if not callable(callback) or (on_drop is not None and not callable(on_drop)):
            futil.log('TaskManager.post: phase=callback_validation callback is not callable')
            return None
        try:
            cls._reap_stale()
            task_id = str(uuid.uuid4())
            with cls._tasks_lock:
                if not cls._is_running:
                    futil.log('TaskManager.post: phase=running_check manager is stopped')
                    return None
                cls._pending_tasks[task_id] = {'command': command, 'callback': callback,
                                               'data': data, 'created': time.monotonic(),
                                               'on_drop': on_drop}
            event_data = {'task_id': task_id, 'command': command, 'data': data}
            try:
                # Fusion can return False even when it delivers the event.
                app.fireCustomEvent(cls._custom_event.eventId, json.dumps(event_data))
            except Exception:
                # The entry is inserted BEFORE the fire so notify() can never arrive to a missing
                # task. If the fire itself fails, no event will ever claim that entry, and post()
                # returns None - so the caller has no task_id to cancel() with. Remove it here or it
                # lingers until the TTL reap.
                with cls._tasks_lock:
                    dropped = cls._pending_tasks.pop(task_id, None)
                cls._notify_drop(dropped, "custom_event_fire_failed")
                raise
            return task_id
        except Exception:
            futil.handle_error('TaskManager.post')
            return None

    @staticmethod
    def _notify_drop(task, reason):
        if task and callable(task.get('on_drop')):
            try:
                task['on_drop'](reason)
            except Exception:
                futil.handle_error('TaskManager.on_drop')

    @classmethod
    def _reap_stale(cls, ttl: float = _PENDING_TASK_TTL_S) -> int:
        """Drop pending tasks older than `ttl` seconds - orphans left when Fusion dropped their custom
        event and no cancel() removed them. Called opportunistically from post() (no background thread,
        so it stays off the main-thread-safety critical path). Returns the number reaped.

        A reaped task's callback will NEVER run. That is the correct outcome for a dropped event: the
        poster has long since timed out (TTL >> the request timeout), so the result was already reported
        as a timeout - running the callback minutes later would apply a side effect nobody is waiting
        for. notify() pops under the same lock, so a task claimed in the same instant is not double-freed.
        """
        now = time.monotonic()
        with cls._tasks_lock:
            stale = [tid for tid, t in cls._pending_tasks.items()
                     if now - t.get('created', now) > ttl]
            dropped = [cls._pending_tasks.pop(tid, None) for tid in stale]
        for task in dropped:
            cls._notify_drop(task, "pending_task_reaped")
        if stale:
            futil.log(f'TaskManager: reaped {len(stale)} orphaned pending task(s) (dropped events)')
        return len(stale)

    @classmethod
    def cancel(cls, task_id: str) -> bool:
        """Drop a still-pending task so it never runs. Returns True iff a pending task was
        actually removed (i.e. the cancel won the race and the callback will NOT run).

        Used when the poster has given up waiting (see _execute_on_main_thread's timeout): if the
        event has not yet fired on the main thread, removing the task here prevents the now-orphaned
        callback from running after the caller moved on - and we return True so the caller can
        truthfully say "cancelled before running". If the task was already CLAIMED by notify() (it
        is running, or finished, on the main thread), there is nothing to pop: we return False,
        because the callback's side effect may already be committing and the caller must NOT claim
        it was cancelled. There is no way to interrupt an in-flight main-thread callback.
        """
        if not task_id:
            return False
        with cls._tasks_lock:
            return cls._pending_tasks.pop(task_id, None) is not None

    @classmethod
    def is_running(cls) -> bool:
        with cls._tasks_lock:
            return cls._is_running

    @classmethod
    def get_pending_task_count(cls) -> int:
        with cls._tasks_lock:
            return len(cls._pending_tasks)


class TaskEventHandler(adsk.core.CustomEventHandler):
    """Runs a posted callback on the main thread when the custom event fires."""

    def __init__(self, pending_tasks: Dict[str, Dict[str, Any]]):
        super().__init__()
        self._pending_tasks = pending_tasks

    def notify(self, args: adsk.core.CustomEventArgs):
        try:
            event_data = json.loads(args.additionalInfo)
            task_id = event_data.get('task_id')
            command = event_data.get('command')

            # Atomically claim the task: pop under the lock so a concurrent cancel()
            # (poster timed out) can't double-fire or run after removal. A missing
            # id means it was already cancelled - nothing to do.
            with TaskManager._tasks_lock:
                task_info = self._pending_tasks.pop(task_id, None) if task_id else None
            if task_info is None:
                return

            callback = task_info['callback']
            try:
                callback(task_info['data'])
            except Exception:
                futil.handle_error(f'TaskManager.callback[{command}]')
        except json.JSONDecodeError:
            futil.handle_error('TaskManager.notify.json')
        except Exception:
            futil.handle_error('TaskManager.notify')
