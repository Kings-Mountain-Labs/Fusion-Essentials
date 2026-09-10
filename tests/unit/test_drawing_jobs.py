"""Deferred drawing work: durable deduplication, transport ordering, and status polling."""

import asyncio
import json
import threading

from conftest import load_mcp_server, load_tool


def _arguments(**extra):
    out = {"expect_document": "session:" + "a" * 32}
    out.update(extra)
    return out


class TestDrawingJobStore:
    def test_concurrent_identical_accepts_create_one_job_and_conflict_is_refused(self, tmp_path):
        jobs = load_mcp_server().drawing_jobs
        store = jobs.DrawingJobStore(tmp_path)
        answers = []

        def accept():
            answers.append(store.accept("request-1", "drawing_export", _arguments(file_path="a.pdf")))

        threads = [threading.Thread(target=accept) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(created for _record, created, _error in answers) == 1
        assert len({record["job_id"] for record, _created, _error in answers}) == 1
        _record, _created, error = store.accept(
            "request-1", "drawing_export", _arguments(file_path="b.pdf"))
        assert "different arguments" in error

    def test_restart_marks_nonterminal_unresolved_and_old_owner_cannot_finish(self, tmp_path):
        jobs = load_mcp_server().drawing_jobs
        old = jobs.DrawingJobStore(tmp_path)
        old.accept("request-1", "drawing_update", _arguments())
        assert old.claim_dispatch("request-1") is True
        assert old.mark_running("request-1") is True
        restarted = jobs.DrawingJobStore(tmp_path)
        assert restarted.get("request-1")["status"] == "unresolved"
        assert restarted.get("request-1")["reason"] == "server_restarted"
        assert old.finish("request-1", {"isError": False}) is False
        assert restarted.get("request-1")["status"] == "unresolved"

    def test_raw_success_and_error_results_are_preserved(self, tmp_path):
        jobs = load_mcp_server().drawing_jobs
        store = jobs.DrawingJobStore(tmp_path)
        for key, result, status in (
                ("ok", {"isError": False, "content": [{"type": "text", "text": "done"}]},
                 "completed"),
                ("bad", {"isError": True, "message": "native refused"}, "failed")):
            store.accept(key, "drawing_update", _arguments())
            assert store.claim_dispatch(key) is True
            assert store.mark_running(key) is True
            assert store.finish(key, result) is True
            row = store.get(key)
            assert row["status"] == status and row["terminal_result"] == result

    def test_acceptance_persistence_failure_refuses_dispatchable_record(self, tmp_path, monkeypatch):
        jobs = load_mcp_server().drawing_jobs
        store = jobs.DrawingJobStore(tmp_path)
        monkeypatch.setattr(
            store, "_connect", lambda: (_ for _ in ()).throw(OSError("disk full")))
        record, created, error = store.accept("request-1", "drawing_update", _arguments())
        assert record is None and created is False and "disk full" in error

    def test_running_persistence_failure_stays_visible_and_blocks_execution(
            self, tmp_path, monkeypatch):
        jobs = load_mcp_server().drawing_jobs
        store = jobs.DrawingJobStore(tmp_path)
        store.accept("request-1", "drawing_update", _arguments())
        assert store.claim_dispatch("request-1") is True
        monkeypatch.setattr(
            store, "_connect", lambda: (_ for _ in ()).throw(OSError("disk full")))
        assert store.mark_running("request-1") is False
        row = store.get("request-1")
        assert row["status"] == "unresolved"
        assert row["reason"] == "running_persistence_failed"

    def test_terminal_persistence_failure_stays_visible_as_unresolved(self, tmp_path, monkeypatch):
        jobs = load_mcp_server().drawing_jobs
        store = jobs.DrawingJobStore(tmp_path)
        store.accept("request-1", "drawing_update", _arguments())
        store.claim_dispatch("request-1")
        store.mark_running("request-1")
        original = store._update_row

        def fail_terminal(connection, record):
            if record.get("terminal_result") is not None:
                raise OSError("disk full at completion")
            return original(connection, record)

        monkeypatch.setattr(store, "_update_row", fail_terminal)
        result = {"isError": False, "content": [{"type": "text", "text": "done"}]}
        assert store.finish("request-1", result) is False
        row = store.get("request-1")
        assert row["status"] == "unresolved"
        assert row["reason"] == "terminal_persistence_failed"
        assert row["terminal_result"] == result

    def test_restart_transaction_wins_before_an_old_finish_write(self, tmp_path, monkeypatch):
        jobs = load_mcp_server().drawing_jobs
        old = jobs.DrawingJobStore(tmp_path)
        old.accept("request-1", "drawing_update", _arguments())
        old.claim_dispatch("request-1")
        old.mark_running("request-1")
        entered = threading.Event()
        release = threading.Event()
        original_connect = old._connect

        def paused_connect():
            entered.set()
            assert release.wait(2)
            return original_connect()

        monkeypatch.setattr(old, "_connect", paused_connect)
        outcome = []
        worker = threading.Thread(
            target=lambda: outcome.append(old.finish("request-1", {"isError": False})))
        worker.start()
        assert entered.wait(2)
        restarted = jobs.DrawingJobStore(tmp_path)
        release.set()
        worker.join(2)
        assert outcome == [False]
        assert restarted.get("request-1")["status"] == "unresolved"

    def test_superseded_store_cannot_accept_a_new_key(self, tmp_path):
        jobs = load_mcp_server().drawing_jobs
        old = jobs.DrawingJobStore(tmp_path)
        restarted = jobs.DrawingJobStore(tmp_path)
        record, created, error = old.accept(
            "late-request", "drawing_update", _arguments())
        assert record is None and created is False
        assert error == "drawing job store instance is stale"
        accepted, created, error = restarted.accept(
            "current-request", "drawing_update", _arguments())
        assert accepted["status"] == "accepted" and created is True and error is None

    def test_drop_persistence_failure_is_visible_and_blocks_execution(
            self, tmp_path, monkeypatch):
        jobs = load_mcp_server().drawing_jobs
        store = jobs.DrawingJobStore(tmp_path)
        store.accept("request-1", "drawing_update", _arguments())
        store.claim_dispatch("request-1")
        original_connect = store._connect
        monkeypatch.setattr(
            store, "_connect", lambda: (_ for _ in ()).throw(OSError("disk full")))
        assert store.fail_unclaimed("request-1", "task_manager_stopped_before_claim") is False
        row = store.get("request-1")
        assert row["status"] == "unresolved"
        assert row["reason"] == "drop_persistence_failed"
        monkeypatch.setattr(store, "_connect", original_connect)
        record, created, error = store.accept(
            "request-1", "drawing_update", _arguments())
        assert record["status"] == "unresolved" and created is False
        assert "unresolved persistence state" in error
        assert store.claim_dispatch("request-1") is False

    def test_failed_reconciliation_is_visible_and_refuses_new_work(
            self, tmp_path, monkeypatch):
        jobs = load_mcp_server().drawing_jobs
        old = jobs.DrawingJobStore(tmp_path)
        old.accept("request-1", "drawing_update", _arguments())
        original = jobs.DrawingJobStore._update_row
        monkeypatch.setattr(
            jobs.DrawingJobStore, "_update_row",
            lambda *_args: (_ for _ in ()).throw(OSError("reconcile disk failure")))
        restarted = jobs.DrawingJobStore(tmp_path)
        row = restarted.get("request-1")
        assert row["status"] == "unresolved"
        assert row["reason"] == "store_initialization_failed"
        record, created, error = restarted.accept(
            "request-2", "drawing_update", _arguments())
        assert record is None and created is False and "reconcile disk failure" in error
        monkeypatch.setattr(jobs.DrawingJobStore, "_update_row", original)

    def test_simultaneous_first_access_builds_one_default_store(self, monkeypatch):
        jobs = load_mcp_server().drawing_jobs
        made = []
        release = threading.Event()

        class SlowStore:
            def __init__(self):
                made.append(self)
                release.wait(0.05)

        monkeypatch.setattr(jobs, "DrawingJobStore", SlowStore)
        monkeypatch.setattr(jobs, "_DEFAULT_STORE", None)
        stores = []
        threads = [threading.Thread(target=lambda: stores.append(jobs.get_store()))
                   for _ in range(8)]
        for thread in threads:
            thread.start()
        release.set()
        for thread in threads:
            thread.join()
        assert len(made) == 1
        assert len({id(store) for store in stores}) == 1


class _Tasks:
    callback = None
    data = None
    posts = 0

    @classmethod
    def is_running(cls):
        return True

    @classmethod
    def start(cls):
        return True

    @classmethod
    def post(cls, command, callback, data, on_drop=None):
        cls.callback, cls.data = callback, data
        cls.posts += 1
        return "task-1"


def _deferred_item(handler=None):
    from mcpServer.mcp_primitives.item import Item
    from mcpServer.mcp_primitives.tool import Tool

    tool = Tool.create_simple(name="drawing_probe", description="test deferred drawing write")
    tool.strict_schema()
    return Item.create_tool_item(
        tool=tool, handler=handler or (lambda: {"isError": False}), write="write",
        deferred_capable=True)


class _Doc:
    isValid = True

    def __init__(self, name):
        self.name = name


class TestDeferredRegistration:
    def test_only_the_three_drawing_writes_opt_in_with_clear_schema(self):
        for name in ("drawing_create", "drawing_update", "drawing_export"):
            module = load_tool(name)
            props = module.tool.input_schema["properties"]
            assert module.item.deferred_capable is True
            assert props["deferred"]["type"] == "boolean"
            assert props["request_key"]["type"] == "string"


class TestDeferredTransport:
    def test_validation_and_identical_retry_dispatch_once(self, tmp_path, monkeypatch):
        mcp = load_mcp_server()
        store = mcp.drawing_jobs.DrawingJobStore(tmp_path)
        server = mcp.SimpleMCPServer(job_store=store)
        server.register(_deferred_item())
        monkeypatch.setattr(mcp, "TaskManager", _Tasks)
        _Tasks.posts = 0

        bad_bool = asyncio.run(server.handle_request({
            "id": 1, "method": "tools/call", "params": {
                "name": "drawing_probe", "arguments": {
                    "deferred": "yes", "request_key": "r", **_arguments()}}}))
        assert bad_bool["result"]["isError"] is True
        missing_handle = asyncio.run(server.handle_request({
            "id": 2, "method": "tools/call", "params": {
                "name": "drawing_probe", "arguments": {
                    "deferred": True, "request_key": "r"}}}))
        assert missing_handle["result"]["isError"] is True

        request = {"id": 3, "method": "tools/call", "params": {
            "name": "drawing_probe", "arguments": {
                "deferred": True, "request_key": "r", **_arguments()}}}
        first = asyncio.run(server.handle_request(request))
        same = asyncio.run(server.handle_request(request))
        assert _Tasks.posts == 0
        first.after_send()
        assert _Tasks.posts == 1
        assert json.loads(first["result"]["content"][0]["text"])["job_id"] == \
            json.loads(same["result"]["content"][0]["text"])["job_id"]

    def test_active_document_switch_is_refused_by_the_actual_wrapped_callback(
            self, tmp_path, monkeypatch):
        mcp = load_mcp_server()
        store = mcp.drawing_jobs.DrawingJobStore(tmp_path)
        wg = load_tool("_write_guard")
        expected = _Doc("Expected")
        session = type("Session", (), {"activeDocument": expected})()
        monkeypatch.setattr(wg, "app", session)
        monkeypatch.setattr(wg, "_SESSION_DOCUMENTS", [])
        monkeypatch.setattr(wg, "_active_identity",
                            lambda: (session.activeDocument.name, None))
        handle = wg.document_handle(expected)
        server = mcp.SimpleMCPServer(job_store=store)
        server.register(_deferred_item())
        monkeypatch.setattr(mcp, "TaskManager", _Tasks)
        request = {"id": 4, "method": "tools/call", "params": {
            "name": "drawing_probe", "arguments": {
                "deferred": True, "request_key": "switch",
                "expect_document": handle}}}
        response = asyncio.run(server.handle_request(request))
        response.after_send()
        session.activeDocument = _Doc("Other")
        _Tasks.callback(_Tasks.data)
        row = store.get("switch")
        assert row["status"] == "failed"
        assert "active_document_changed" in row["terminal_result"]["message"]

    def test_callback_exception_and_failed_post_remain_pollable(self, tmp_path, monkeypatch):
        mcp = load_mcp_server()
        store = mcp.drawing_jobs.DrawingJobStore(tmp_path)
        server = mcp.SimpleMCPServer(job_store=store)
        wg = load_tool("_write_guard")
        document = _Doc("Expected")
        session = type("Session", (), {"activeDocument": document})()
        monkeypatch.setattr(wg, "app", session)
        monkeypatch.setattr(wg, "_SESSION_DOCUMENTS", [])
        monkeypatch.setattr(wg, "_active_identity", lambda: (document.name, None))
        handle = wg.document_handle(document)

        def boom():
            raise RuntimeError("native boom")

        server.register(_deferred_item(boom))
        monkeypatch.setattr(mcp, "TaskManager", _Tasks)
        request = {"id": 5, "method": "tools/call", "params": {
            "name": "drawing_probe", "arguments": {
                "deferred": True, "request_key": "exception",
                "expect_document": handle}}}
        response = asyncio.run(server.handle_request(request))
        response.after_send()
        _Tasks.callback(_Tasks.data)
        assert store.get("exception")["status"] == "failed"
        assert "native boom" in store.get("exception")["terminal_result"]["message"]

        class NoPost(_Tasks):
            @classmethod
            def post(cls, command, callback, data, on_drop=None):
                return None

        monkeypatch.setattr(mcp, "TaskManager", NoPost)
        request["params"]["arguments"]["request_key"] = "no-post"
        response = asyncio.run(server.handle_request(request))
        response.after_send()
        row = store.get("no-post")
        assert row["status"] == "failed" and row["reason"] == "task_post_failed"

    def test_http_flush_precedes_dispatch(self):
        mcp = load_mcp_server()
        order = []
        response = mcp._AfterSendResponse({"ok": True}, lambda: order.append("dispatch"))

        async def _answer(_request):
            return response

        class Wire:
            def write(self, _body):
                order.append("write")
            def flush(self):
                order.append("flush")

        class Probe:
            path = "/mcp"
            headers = {"Content-Length": "2"}
            rfile = type("Body", (), {"read": lambda self, _n: b"{}"})()
            wfile = Wire()
            mcp_server = type("Server", (), {"handle_request": staticmethod(_answer),
                                                "session_id": "s"})()
            def _origin_ok(self): return True
            def send_response(self, *_args): pass
            def send_header(self, *_args): pass
            def end_headers(self): pass
            def _send_json(self, data):
                return mcp.MCPHandler._send_json(self, data)

        mcp.MCPHandler.do_POST(Probe())
        assert order == ["write", "flush", "dispatch"]

    def test_lost_acceptance_response_retries_then_reaches_terminal(
            self, tmp_path, monkeypatch):
        mcp = load_mcp_server()
        store = mcp.drawing_jobs.DrawingJobStore(tmp_path)
        server = mcp.SimpleMCPServer(job_store=store)
        server.register(_deferred_item())
        monkeypatch.setattr(mcp, "TaskManager", _Tasks)
        wg = load_tool("_write_guard")
        document = _Doc("Expected")
        session = type("Session", (), {"activeDocument": document})()
        monkeypatch.setattr(wg, "app", session)
        monkeypatch.setattr(wg, "_SESSION_DOCUMENTS", [])
        monkeypatch.setattr(wg, "_active_identity", lambda: (document.name, None))
        handle = wg.document_handle(document)
        _Tasks.posts = 0
        request = {"id": 8, "method": "tools/call", "params": {
            "name": "drawing_probe", "arguments": {
                "deferred": True, "request_key": "lost", "expect_document": handle}}}

        first = asyncio.run(server.handle_request(request))
        assert store.get("lost")["status"] == "accepted"
        retry = asyncio.run(server.handle_request(request))
        retry.after_send()
        assert _Tasks.posts == 1
        assert store.get("lost")["status"] == "queued"
        _Tasks.callback(_Tasks.data)
        assert store.get("lost")["status"] == "completed"
        terminal_retry = asyncio.run(server.handle_request(request))
        terminal_payload = json.loads(terminal_retry["result"]["content"][0]["text"])
        assert terminal_payload["status"] == "completed"
        assert not hasattr(terminal_retry, "after_send")
        assert _Tasks.posts == 1
        assert json.loads(first["result"]["content"][0]["text"])["job_id"] == \
            terminal_payload["job_id"]

    def test_http_send_failure_does_not_run_after_send(self):
        mcp = load_mcp_server()
        called = []
        response = mcp._AfterSendResponse({"ok": True}, lambda: called.append("dispatch"))

        class Probe:
            path = "/mcp"
            headers = {"Content-Length": "2"}
            rfile = type("Body", (), {"read": lambda self, _n: b"{}"})()
            mcp_server = type("Server", (), {
                "handle_request": staticmethod(lambda _request: _answer()), "session_id": "s"})()

            def _origin_ok(self):
                return True

            def _send_json(self, _response):
                raise OSError("client disconnected")

        async def _answer():
            return response

        try:
            mcp.MCPHandler.do_POST(Probe())
        except OSError:
            pass
        assert called == []


class TestServerStartupReconciliation:
    def test_bind_precedes_reconcile_and_serving_follows_it(self, tmp_path, monkeypatch):
        mcp = load_mcp_server()
        jobs = mcp.drawing_jobs
        old = jobs.DrawingJobStore(tmp_path)
        old.accept("running", "drawing_update", _arguments())
        old.claim_dispatch("running")
        old.mark_running("running")
        order = []

        class HTTP:
            def __init__(self, _address, _handler):
                order.append("bind")
            def serve_forever(self):
                pass
            def server_close(self):
                order.append("close")

        class Thread:
            def __init__(self, **_kwargs):
                pass
            def start(self):
                order.append("start")

        store_type = jobs.DrawingJobStore

        def current_store(_root=None):
            order.append("reconcile")
            return store_type(tmp_path)

        monkeypatch.setattr(mcp, "ThreadedHTTPServer", HTTP)
        monkeypatch.setattr(mcp.threading, "Thread", Thread)
        monkeypatch.setattr(jobs, "DrawingJobStore", current_store)
        result = mcp.start_server("127.0.0.1", 27182)
        assert result["status"] == mcp.START_OK
        assert order == ["bind", "reconcile", "start"]
        assert old.finish("running", {"isError": False}) is False
        assert result["mcp"]._jobs().get("running")["status"] == "unresolved"
        assert jobs.get_store() is result["mcp"]._jobs()

    def test_unexpected_store_initialization_failure_closes_bound_socket(
            self, monkeypatch):
        mcp = load_mcp_server()
        made = []

        class HTTP:
            def __init__(self, _address, _handler):
                self.closed = False
                made.append(self)
            def server_close(self):
                self.closed = True

        monkeypatch.setattr(mcp, "ThreadedHTTPServer", HTTP)
        monkeypatch.setattr(
            mcp.drawing_jobs, "DrawingJobStore",
            lambda _root=None: (_ for _ in ()).throw(
                RuntimeError("store construction failed")))
        result = mcp.start_server("127.0.0.1", 27182)
        assert result["status"] == mcp.START_ERROR
        assert len(made) == 1 and made[0].closed is True


class TestDrawingStatus:
    def test_unknown_and_terminal_status_are_pure_store_reads(self, tmp_path, monkeypatch):
        mcp = load_mcp_server()
        store = mcp.drawing_jobs.DrawingJobStore(tmp_path)
        status = load_tool("drawing_get_status")
        monkeypatch.setattr(status.drawing_jobs, "get_store", lambda: store)
        assert json.loads(status.handler("missing")["content"][0]["text"])["status"] == "unknown"
        store.accept("done", "drawing_update", _arguments())
        store.claim_dispatch("done")
        store.mark_running("done")
        result = {"isError": False, "content": [{"type": "text", "text": "ok"}]}
        store.finish("done", result)
        row = json.loads(status.handler("done")["content"][0]["text"])
        assert row["status"] == "completed" and row["terminal_result"] == result
        assert "canonical_request" not in row and "owner_instance" not in row
        assert status.item.run_on_main_thread is False
