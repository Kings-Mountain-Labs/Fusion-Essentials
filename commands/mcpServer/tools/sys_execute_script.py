# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.
# Adapted from Autodesk's Fusion MCP add-in sample (MIT-licensed).

"""High-risk MCP tool: execute arbitrary Fusion API Python in the live session. NOT registered
unless the user enables it (mcpServer settings -> allow_execute_api_script, default False).
On DESIGN documents an uncaught raise rolls the whole script back, and catching an error is
no guarantee the earlier work survived - some API errors take the command down even when caught.
On a DRAWING document rollback is not guaranteed in either direction."""

import json
import os
import re
import tempfile
import traceback

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from . import _drawing_common

app = adsk.core.Application.get()


def handler(script: str, read_only: bool = False) -> dict:
    """See TOOL_DESCRIPTION."""
    # Require a `run` function taking a single argument (the Fusion script idiom).
    if not re.search(r'def\s+run\s*\(\s*(\w+)\s*\):', script):
        return _refusal("Script must define a 'run' function taking one argument, e.g. def run(context):")
    if _PRODUCTS_READ.search(script) and _drawing_common.active_drawing() is not None:
        return _refusal(_PRODUCTS_ON_DRAWING)

    temp_file = None
    transaction_started = False
    transacted_doc = None
    try:
        # Python.Run executes the file but does not call run(). Its return text embeds the console
        # ACCUMULATED since the last run, so the sentinel print marks where this run's output starts.
        script = _UNJAM_PRELUDE + f'print("{_RUN_SENTINEL}")\n' + script + "\nrun(None)"

        with tempfile.NamedTemporaryFile(mode='w', prefix='fe_mcp_script', suffix='.py',
                                         delete=False, encoding='utf-8') as f:
            f.write(script)
            temp_file = f.name

        # Python.Run parses the path out of a quoted string, where a backslash can be mis-handled;
        # Fusion accepts forward slashes on both platforms, and the quotes preserve spaces.
        run_path = temp_file.replace('\\', '/')

        if read_only:
            return _run_read_only(run_path)

        # The transaction groups the script's changes into ONE undo step; it does not decide what
        # survives a raise.
        try:
            transacted_doc = app.activeDocument
        except Exception:
            transacted_doc = None
        if transacted_doc:
            app.executeTextCommand('PTransaction.Start "Fusion-Essentials MCP Script"')
            transaction_started = True

        res = app.executeTextCommand(f'Python.Run "{run_path}"')

        if transaction_started and transacted_doc.isValid:
            current_doc = app.activeDocument
            if current_doc is transacted_doc:
                app.executeTextCommand('PTransaction.Commit')
            else:
                # Active document changed mid-script; commit against the original.
                transacted_doc.activate()
                app.executeTextCommand('PTransaction.Commit')
                current_doc.activate()

        return _ok_result(_clean_output(res))

    except Exception as e:
        if transaction_started and transacted_doc and transacted_doc.isValid:
            try:
                current_doc = app.activeDocument
                if current_doc is transacted_doc:
                    app.executeTextCommand('PTransaction.Abort')
                else:
                    transacted_doc.activate()
                    app.executeTextCommand('PTransaction.Abort')
                    current_doc.activate()
            except Exception:
                pass  # if abort itself fails, nothing more we can do
        tb = traceback.format_exc()
        app.log(f"Fusion-Essentials MCP sys_execute_script error: {e}\n{tb}")
        return _error_result(_extract_script_error(tb), mutated=not read_only)
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.unlink(temp_file)
            except Exception:
                pass


# Python.Run's RuntimeError message embeds the accumulated console text, so the add-in's own log
# lines show up INSIDE the script-failure traceback.
_CONSOLE_NOISE = re.compile(r"^MCP calling tool: .*$", re.MULTILINE)
_TB_MARKER = "Traceback (most recent call last):"
# Printed as the script's FIRST statement; console text before it accumulated during EARLIER calls.
_RUN_SENTINEL = "<<FE-SCRIPT-OUTPUT>>"
# Every MCP.Execute call wraps the interpreter's sys.stdout in a sanitizing shim it never removes,
# so the shims NEST one per read_only call and the innermost drops every print once 1 MiB has passed
# through it in its lifetime (measured: 15 deep, the bottom jammed). The prelude un-jams the outer.
_UNJAM_PRELUDE = (
    "import sys as _fe_sys\n"
    "_fe_out = _fe_sys.stdout\n"
    "_fe_bottom = _fe_out\n"
    "while type(_fe_bottom).__name__ == '_NsSanitizedWriter':\n"
    "    _fe_bottom = _fe_bottom._original\n"
    "if _fe_out is not _fe_bottom:\n"
    "    _fe_out._truncated = False\n"
    "    _fe_out._written = 0\n"
    "    _fe_out._original = _fe_bottom\n"
)
# A read of any document's .products, the form measured killing the call on a DRAWING.
_PRODUCTS_READ = re.compile(r"\.products\b")
_PRODUCTS_ON_DRAWING = (
    "The active document is a DRAWING and this script reads '.products': that read has been "
    "measured killing the call, so nothing was run. Read app.activeProduct instead.")
# What executeTextCommand reports for a text command this Fusion build does not carry.
_NO_SUCH_COMMAND = "There is no command MCP.Execute"
_NO_READ_ONLY_CHANNEL = (
    "read_only is unavailable in this Fusion build: it runs through the MCP.Execute text command, "
    "and this build reports no such command. Re-run without read_only - that path runs with no "
    "read-only enforcement, so verify the state afterwards."
)
_UNREADABLE_RESULT = (
    "The read-only channel returned a result this tool could not read (no 'success' field), so "
    "whether the script ran is unknown. Read the state back before assuming either way. Raw result:"
)


def _clean_output(res: str) -> str:
    """Reduce a channel's return text to THIS run's own printed output: cut at this run's sentinel,
    then strip the per-call log lines."""
    res = res or ""
    cut = res.rfind(_RUN_SENTINEL)
    if cut != -1:
        res = res[cut + len(_RUN_SENTINEL):]
    return re.sub(r"\n{3,}", "\n\n", _CONSOLE_NOISE.sub("", res)).strip()


def _ok_result(cleaned: str) -> dict:
    result = {"isError": False, "message": "Script executed successfully"}
    result["content"] = [{"type": "text", "text": cleaned or _NO_OUTPUT}]
    return result


def _run_read_only(run_path: str) -> dict:
    """Run the prepared script file under Fusion's read-only context, via MCP.Execute."""
    # MCP.Execute takes ONE quoted JSON parameter, so the caller's script stays in the temp file and
    # a fixed loader execs it - no caller text has to survive the text-command parser.
    loader = ("def run(_context):\n"
              "    path = " + repr(run_path) + "\n"
              "    with open(path, encoding='utf-8') as fh:\n"
              "        src = fh.read()\n"
              "    exec(compile(src, path, 'exec'), {'__name__': '__fe_read_only_script__'})\n")
    payload = json.dumps({"featureType": "script",
                          "object": {"readOnly": True, "script": loader}},
                         separators=(',', ':'))
    try:
        res = app.executeTextCommand('MCP.Execute "' + payload.replace('"', '\\"') + '"')
    except Exception as e:
        if _NO_SUCH_COMMAND in str(e):
            return _error_result(_NO_READ_ONLY_CHANNEL)
        raise

    try:
        parsed = json.loads(res)
    except Exception:
        parsed = None
    if not isinstance(parsed, dict) or "success" not in parsed:
        return _error_result(_UNREADABLE_RESULT + "\n\n" + _clean_output(res)[:2000])
    cleaned = _clean_output(parsed.get("message") or parsed.get("error") or "")
    if parsed["success"] is not True:
        return _error_result(cleaned or "The read-only channel reported failure with no detail.")
    return _ok_result(cleaned)


def _extract_script_error(tb: str) -> str:
    """Reduce a script-failure traceback to the SCRIPT's own error - the LAST traceback block, noise
    stripped; a single-traceback text is returned whole. The full text still goes to app.log."""
    cut = tb.rfind(_RUN_SENTINEL)
    if cut != -1:
        tb = tb[cut + len(_RUN_SENTINEL):]
    cleaned = _CONSOLE_NOISE.sub("", tb)
    first = cleaned.find(_TB_MARKER)
    last = cleaned.rfind(_TB_MARKER)
    if first != -1 and last > first:
        cleaned = cleaned[last:]
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


# Appended to a FAILURE result on a drawing document only.
_DRAWING_ROLLBACK_ADVICE = (
    "\n\nThe active document is a DRAWING: rollback is not guaranteed either way here - a failing "
    "script has been measured both leaving the sheet it added behind and cleaning it up. Re-read the "
    "sheets before assuming this call changed nothing."
)

# Appended to a failure that could have MUTATED a design - the reason one mutation per call is the
# rule, delivered where a caller has to decide what to re-read.
_DESIGN_ROLLBACK_ADVICE = (
    "\n\nThe active document is a DESIGN: an UNCAUGHT raise always rolls the WHOLE script back, and "
    "catching one is NO GUARANTEE the earlier work survived - some errors take the command down even "
    "when caught. There is no per-item failure isolation, so this call cannot say WHICH item of a "
    "batch failed. Re-read the state before assuming what landed."
)

_NO_OUTPUT = "The script printed nothing - print() is what returns a value."


def _refusal(text: str) -> dict:
    """The failure shape for a script that never ran: no rollback advice, nothing to re-read."""
    # NOT _common.error: that mirrors one text into content AND message; here message stays terse
    # while content carries the whole traceback.
    return {
    "content": [{"type": "text", "text": text}],
    "isError": True,
    "message": "Script execution failed",
    }


def _error_result(text: str, mutated: bool = False) -> dict:
    if _drawing_common.active_drawing() is not None:
        text += _DRAWING_ROLLBACK_ADVICE
    elif mutated:
        text += _DESIGN_ROLLBACK_ADVICE
    return _refusal(text)


TOOL_DESCRIPTION = (
    "Run Fusion API Python in the live session; prefer a typed tool (sys_find_tool)."
)

tool = Tool.create_with_string_input(
    name="sys_execute_script",
    description=TOOL_DESCRIPTION,
    input_param_name="script",
    input_param_description="Must define run(context); no modal UI; ONE mutation per call.",
).add_input_property(
    "read_only",
    {"type": "boolean",
     "description": "A design change RAISES; file writes still land."},
).strict_schema()

# enforce_timeout=False: a long script cannot be interrupted mid-run and still COMMITs, so the
# server's task timeout would only report a false failure for a change that applied.
item = Item.create_tool_item(tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
                             enforce_timeout=False,
                             verification=Verification(kind="dynamic"))


def register_tool():
    """Register this tool. Called only when the user has enabled it (gated)."""
    register(item)
