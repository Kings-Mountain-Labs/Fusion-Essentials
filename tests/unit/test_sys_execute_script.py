"""Unit tests for ``sys_execute_script``'s pure error-shaping helper.

The tool itself is a Fusion pass-through (deliberately not unit-tested; see tests/README.md), but
``_extract_script_error`` is pure logic worth pinning: a failing script surfaces as an OUTER
executeTextCommand traceback whose RuntimeError message embeds console noise ('MCP calling tool:
...' lines) plus the script's INNER traceback. The agent must receive the inner traceback - the
part that names the script bug - not the wrapper and the noise (live run: the signal was the last
3 lines of a 15-line blob).
"""

import json
import re

import pytest

from conftest import FakeApplication, FakeFusionDocument, load_tool

ses = load_tool("sys_execute_script")

_OUTER = '''Traceback (most recent call last):
  File "C:/source/tools/sys_execute_script.py", line 79, in handler
    res = app.executeTextCommand(f'Python.Run "{run_path}"')
  File "C:/adsk/core.py", line 5287, in executeTextCommand
    return _core.Application_executeTextCommand(self, command)
RuntimeError: 3 : MCP calling tool: doc_insert_occurrence
MCP calling tool: assembly_ground
MCP calling tool: joint_create
Traceback (most recent call last):
  File "<string>", line 29, in <module>
  File "<string>", line 17, in run
TypeError: in method 'Joints_createInput', argument 3 of type 'adsk::core::Ptr'
'''


class TestExtractScriptError:
    def test_returns_only_the_inner_traceback(self):
        out = ses._extract_script_error(_OUTER)
        assert out.startswith("Traceback (most recent call last):")
        assert "line 17, in run" in out
        assert "executeTextCommand" not in out  # outer wrapper frames dropped

    def test_console_noise_lines_stripped(self):
        out = ses._extract_script_error(_OUTER)
        assert "MCP calling tool" not in out

    def test_single_traceback_returned_whole(self):
        tb = 'Traceback (most recent call last):\n  File "x.py", line 1, in run\nValueError: boom'
        assert ses._extract_script_error(tb) == tb

    def test_non_traceback_text_passes_through(self):
        assert ses._extract_script_error("plain error") == "plain error"


class TestHandlerGuard:
    def test_script_without_run_function_is_rejected(self):
        res = ses.handler("print('hi')")
        assert res["isError"] is True
        assert "run" in res["content"][0]["text"]


class TestBulkMutationConstraint:
    """A script runs before any error of ours can reach the caller, so the requirements it must be
    WRITTEN against stay on the wire; each rule's reason is delivered on the failure that needs it."""

    def test_the_wire_states_the_three_requirements_a_script_is_written_against(self):
        desc = ses.tool.input_schema["properties"]["script"]["description"]
        assert "ONE mutation per call" in desc
        assert "run(context)" in desc
        assert "modal UI" in desc

    def test_description_prefers_a_typed_tool_and_claims_no_channel_death(self):
        # The silence once blamed on this channel was Fusion's own stdout shim overflowing, which
        # the prelude now clears each call - so the description no longer teaches a dying channel.
        desc = ses.TOOL_DESCRIPTION
        assert "prefer a typed tool" in desc
        assert "dying" not in desc

    def test_description_stays_under_the_wire_ceiling(self):
        # The description is the surface every connected agent pays for on every turn; the ceiling
        # is a hard lint, so a scoping fix that breaches it is not a fix.
        assert len(ses.TOOL_DESCRIPTION) <= 1300


class TestDrawingFailureAdvice:
    """The re-read advice fires on a FAILURE, and only on a drawing document - that is exactly when
    the caller cannot tell whether the script's earlier work survived."""

    def test_a_failure_on_a_drawing_carries_the_re_read_advice(self, monkeypatch):
        monkeypatch.setattr(ses._drawing_common, "active_drawing", lambda: object())
        res = ses._error_result("Traceback ... boom")
        text = res["content"][0]["text"]
        assert "rollback is not guaranteed either way here" in text
        assert "Re-read the sheets" in text
        assert res["message"] == "Script execution failed"   # the terse contract is unchanged

    def test_a_failure_on_a_design_document_stays_clean(self, monkeypatch):
        monkeypatch.setattr(ses._drawing_common, "active_drawing", lambda: None)
        text = ses._error_result("Traceback ... boom")["content"][0]["text"]
        assert text == "Traceback ... boom"
        assert "sheets" not in text

    def test_module_docstring_carries_the_same_non_guarantee_and_drawing_scope(self):
        # The docstring is where the next reader of this file forms their model, so the caught-error
        # non-guarantee has to hold THERE too - a correct description over a docstring still
        # promising rollback (or survival) teaches the wrong thing at the point of use. The drawing
        # scope rides with it for the same reason.
        doc = ses.__doc__
        assert "no guarantee" in doc.lower()
        assert "even when caught" in doc
        assert "DESIGN documents" in doc
        assert "DRAWING document" in doc
        assert "not guaranteed" in doc.lower()


_SCRIPT = "def run(context):\n    print('hello')\n"


class _TextCommandApp(FakeApplication):
    """The shared Application fake plus its text-command channel: every command is recorded in
    `_commands`, MCP.Execute answers a canned reply (or raises), and the temp file the read-only
    loader points at is read while it still exists into `_script_file_text` - that file's contents
    are what the real channel would exec."""

    def __init__(self, reply=None, raises=None):
        FakeApplication.__init__(self, active_document=FakeFusionDocument(name="Scratch"))
        self._commands = []
        self._logged = []
        self._script_file_text = None
        self._reply = reply
        self._raises = raises

    def executeTextCommand(self, command):
        self._commands.append(command)
        if command.startswith("MCP.Execute"):
            m = re.search(r"path = '([^']+)'", command)
            if m:
                with open(m.group(1), encoding="utf-8") as fh:
                    self._script_file_text = fh.read()
            if self._raises is not None:
                raise RuntimeError(self._raises)
            return self._reply
        return ""

    def log(self, text):
        self._logged.append(text)


class _RaisingApp(_TextCommandApp):
    """A channel whose Python.Run raises - the write path's own failure."""

    def executeTextCommand(self, command):
        self._commands.append(command)
        if command.startswith("Python.Run"):
            raise RuntimeError("3 : boom inside run")
        return ""


@pytest.fixture
def run_script(monkeypatch):
    def _run(reply=None, raises=None, script=_SCRIPT, read_only=True):
        app = _TextCommandApp(reply=reply, raises=raises)
        monkeypatch.setattr(ses, "app", app)
        monkeypatch.setattr(ses._drawing_common, "active_drawing", lambda: None)
        return app, ses.handler(script, read_only=read_only)
    return _run


def _payload_of(command):
    """The JSON object MCP.Execute was handed, unescaped back out of the quoted parameter."""
    assert command.startswith('MCP.Execute "') and command.endswith('"')
    return json.loads(command[len('MCP.Execute "'):-1].replace('\\"', '"'))


class TestReadOnlyRouting:
    def test_read_only_goes_through_mcp_execute_with_the_flag_set(self, run_script):
        app, _ = run_script(reply=json.dumps({"message": "", "success": True}))
        assert len(app._commands) == 1, app._commands
        payload = _payload_of(app._commands[0])
        assert payload["featureType"] == "script"
        assert payload["object"]["readOnly"] is True

    def test_read_only_opens_no_transaction(self, run_script):
        # There is no design change to group into one undo step, and PTransaction.Start on a
        # read-only run would be an undo step that can never contain anything.
        app, _ = run_script(reply=json.dumps({"message": "", "success": True}))
        assert not [c for c in app._commands if c.startswith("PTransaction")]

    def test_the_callers_script_is_never_inlined_into_the_text_command(self, run_script):
        # The parameter is a quoted JSON string whose inner quotes are backslash-escaped; a script
        # carrying its own quotes/backslashes would be at the mercy of that escaping. It stays in the
        # temp file and a fixed loader execs it, so only the loader crosses the parser.
        script = 'def run(context):\n    print("a \\" b \\\\ c")\n'
        app, _ = run_script(reply=json.dumps({"message": "", "success": True}), script=script)
        loader = _payload_of(app._commands[0])["object"]["script"]
        assert 'a \\" b' not in loader
        assert "exec(compile(" in loader

    def test_the_file_the_loader_execs_is_the_sentinel_wrapped_script(self, run_script):
        app, _ = run_script(reply=json.dumps({"message": "", "success": True}))
        text = app._script_file_text
        assert text.startswith(ses._UNJAM_PRELUDE + 'print("%s")' % ses._RUN_SENTINEL)
        assert _SCRIPT in text
        assert text.endswith("run(None)")

    def test_the_prelude_unjams_a_truncated_shim_and_points_the_outer_one_at_the_console(self, monkeypatch):
        # The measured state: shims nested one per read_only call, the innermost past its 1 MiB
        # lifetime and dropping every write. After the prelude the outer shim forwards straight to
        # the console and counts from zero, so this run's prints reach the text Python.Run returns.
        import sys as real_sys

        class CatchOut:
            def __init__(self):
                self.seen = []

            def write(self, text):
                self.seen.append(text)

        class _NsSanitizedWriter:
            def __init__(self, original):
                self._original, self._written, self._truncated = original, 0, False

            def write(self, text):
                if self._truncated:
                    return
                self._written += len(text)
                self._original.write(text)

        console = CatchOut()
        jammed = _NsSanitizedWriter(console)
        jammed._truncated, jammed._written = True, 1048576
        outer = _NsSanitizedWriter(_NsSanitizedWriter(jammed))
        outer.write("lost")
        assert console.seen == []
        monkeypatch.setattr(real_sys, "stdout", outer)
        exec(ses._UNJAM_PRELUDE, {})
        outer.write("found")
        assert console.seen == ["found"]
        assert outer._original is console and outer._written == len("found")

    def test_write_mode_still_uses_python_run_inside_a_transaction(self, run_script):
        app, res = run_script(reply=None, read_only=False)
        assert res["isError"] is False
        assert any(c.startswith('Python.Run "') for c in app._commands)
        assert "PTransaction.Start \"Fusion-Essentials MCP Script\"" in app._commands
        assert "PTransaction.Commit" in app._commands
        assert not any(c.startswith("MCP.Execute") for c in app._commands)


class TestReadOnlyResultHonesty:
    def test_success_returns_the_output_after_this_runs_sentinel(self, run_script):
        reply = json.dumps({"message": "MCP calling tool: sys_execute_script\nstale banner\n"
                                       + ses._RUN_SENTINEL + "\nhello\n", "success": True})
        _, res = run_script(reply=reply)
        assert res["isError"] is False
        assert res["content"][0]["text"] == "hello"

    def test_only_the_output_after_the_LAST_sentinel_is_returned(self, run_script):
        # The cut is rfind, not find. The console text a channel hands back ACCUMULATES across
        # calls, so one reply can carry a preceding call's sentinel AND this call's. Cutting at the
        # FIRST ships the preceding call's output - stale results presented as this script's, which
        # is worse than no output at all because it looks like an answer.
        reply = json.dumps({"message": ses._RUN_SENTINEL + "\nSTALE from an earlier call\n"
                                       + ses._RUN_SENTINEL + "\nfresh\n", "success": True})
        _, res = run_script(reply=reply)
        assert res["content"][0]["text"] == "fresh"
        assert "STALE" not in res["content"][0]["text"]

    def test_a_failed_run_is_an_error_not_a_silent_ok(self, run_script):
        # The channel reports a script failure as success=false WITH a payload, not as a raise
        # (measured: a NameError came back that way). Reading only the text would report ok for a
        # script that never ran.
        reply = json.dumps({"error": ses._RUN_SENTINEL + "\nTraceback...\nNameError: nope",
                            "success": False})
        _, res = run_script(reply=reply)
        assert res["isError"] is True
        assert "NameError: nope" in res["content"][0]["text"]

    def test_an_unreadable_result_shape_is_an_error_not_an_ok(self, run_script):
        _, res = run_script(reply="not json at all")
        assert res["isError"] is True
        assert "could not read" in res["content"][0]["text"]
        assert "not json at all" in res["content"][0]["text"]

    def test_a_json_result_without_success_is_an_error(self, run_script):
        _, res = run_script(reply=json.dumps({"message": "hello"}))
        assert res["isError"] is True
        assert "could not read" in res["content"][0]["text"]

    def test_a_build_without_the_channel_says_so_instead_of_leaking_a_traceback(self, run_script):
        _, res = run_script(raises="3 : There is no command MCP.Execute. Use ? to get help")
        assert res["isError"] is True
        text = res["content"][0]["text"]
        assert "read_only is unavailable in this Fusion build" in text
        assert "Traceback" not in text

    def test_the_enforcement_refusal_reaches_the_caller_as_the_script_error(self, run_script):
        # The measured shape of a blocked mutation: executeTextCommand raises. That is the script's
        # own error and must not be swallowed by the missing-command branch.
        _, res = run_script(raises="3 : Cannot modify the design from a read-only context")
        assert res["isError"] is True
        assert "Cannot modify the design from a read-only context" in res["content"][0]["text"]
        assert "read_only is unavailable" not in res["content"][0]["text"]


class TestRunSentinelCut:
    def test_console_text_before_the_sentinel_is_cut_from_a_failure(self):
        # Python.Run's message embeds console text ACCUMULATED since the last run - an earlier
        # call's error banner included (measured: a timed-out cam_create_machine traceback arrived
        # inside a later script's result). Text before THIS run's sentinel never ships.
        stale = ("===== Error =====\nMCP tool 'cam_create_machine'\n"
                 "Traceback (most recent call last):\n  File \"old\", line 1\nException: timeout\n")
        fresh = ("Traceback (most recent call last):\n  File \"script\", line 3, in run\n"
                 "NameError: name 'x' is not defined")
        tb = stale + ses._RUN_SENTINEL + "\n" + fresh
        out = ses._extract_script_error(tb)
        assert "cam_create_machine" not in out
        assert "NameError" in out

    def test_a_traceback_without_a_sentinel_is_handled_whole(self):
        tb = "Traceback (most recent call last):\n  File \"s\", line 1\nValueError: boom"
        assert "ValueError: boom" in ses._extract_script_error(tb)


class TestDesignFailureAdvice:
    """A failure that could have MUTATED a design is where the rollback rules matter: what an
    uncaught raise did, what catching one does not promise, and that no item can be named."""

    def test_a_mutating_failure_on_a_design_carries_the_rollback_rules(self, monkeypatch):
        # Two measured facts: an UNCAUGHT raise takes the whole script down, and catching one
        # guarantees NOTHING - one caught API error left its mutation standing while another rolled
        # three back. "A caught exception lets the earlier mutations commit" is wrong either way.
        monkeypatch.setattr(ses._drawing_common, "active_drawing", lambda: None)
        text = ses._error_result("Traceback ... boom", mutated=True)["content"][0]["text"]
        assert "UNCAUGHT" in text and "always rolls the WHOLE script back" in text
        assert "NO GUARANTEE" in text and "even when caught" in text
        assert "WHICH item" in text                    # the reason for one mutation per call
        assert "lets execution continue" not in text

    def test_a_drawing_failure_promises_neither_outcome(self, monkeypatch):
        # The rollback claim is measured BOTH ways on a drawing - one rig left the ghost sheet an
        # aborted script added, another cleaned it up. So the DESIGN promise is scoped off a
        # drawing, and the drawing's own advice claims neither outcome.
        monkeypatch.setattr(ses._drawing_common, "active_drawing", lambda: object())
        text = ses._error_result("Traceback ... boom", mutated=True)["content"][0]["text"]
        assert "not guaranteed either way" in text
        assert "always rolls the WHOLE script back" not in text
        assert "leaves the sheet" not in text

    def test_a_failing_write_path_script_reaches_the_design_rules(self, monkeypatch):
        app = _RaisingApp()
        monkeypatch.setattr(ses, "app", app)
        monkeypatch.setattr(ses._drawing_common, "active_drawing", lambda: None)
        res = ses.handler(_SCRIPT, read_only=False)
        assert res["isError"] is True
        assert "always rolls the WHOLE script back" in res["content"][0]["text"]

    def test_a_failing_read_only_script_promises_nothing_about_rollback(self, run_script):
        # read_only blocks the design change, so there is no mutation to have been rolled back.
        _, res = run_script(raises="3 : Cannot modify the design from a read-only context")
        assert "always rolls the WHOLE script back" not in res["content"][0]["text"]


class TestProductsReadOnADrawing:
    """Measured on a DRAWING: a script reading a document's .products dies at the channel, so the
    call that would have taught the caller never returns. A refusal is the only delivery left."""

    _READS_PRODUCTS = "def run(context):\n    p = context.products\n"

    def test_a_products_read_is_refused_on_a_drawing_before_anything_runs(self, monkeypatch):
        app = _TextCommandApp()
        monkeypatch.setattr(ses, "app", app)
        monkeypatch.setattr(ses._drawing_common, "active_drawing", lambda: object())
        res = ses.handler(self._READS_PRODUCTS)
        assert res["isError"] is True
        text = res["content"][0]["text"]
        assert ".products" in text and "app.activeProduct" in text
        assert app._commands == []                  # refused before the channel was touched
        assert "Re-read the sheets" not in text     # nothing ran, so nothing to re-read

    def test_the_same_script_runs_on_a_design_document(self, monkeypatch):
        app = _TextCommandApp()
        monkeypatch.setattr(ses, "app", app)
        monkeypatch.setattr(ses._drawing_common, "active_drawing", lambda: None)
        res = ses.handler(self._READS_PRODUCTS, read_only=False)
        assert res["isError"] is False
        assert any(c.startswith('Python.Run "') for c in app._commands)


class TestNoOutputTeachesPrint:
    def test_a_success_that_printed_nothing_says_how_a_value_comes_back(self, run_script):
        # A script that RETURNS a value instead of printing one reports success with nothing in it;
        # this is the only moment the caller can be told which way values travel.
        _, res = run_script(reply=json.dumps({"message": "", "success": True}))
        assert res["isError"] is False
        assert "print()" in res["content"][0]["text"]
