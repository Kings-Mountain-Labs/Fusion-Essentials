# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The tool_verify receipt: the source hash + VERIFIED_TOOLS.md stamp binding a green live run to the
exact tool source it exercised.

``source_hash()`` must be OS-portable - relative paths hashed with '/' separators, CRLF
normalized to LF - because the receipt is written on one machine and checked on another (and by
git checkouts with different line-ending config). ``check()`` is the offline gate check_all
runs: red when the receipt is missing, stampless, or the source has moved since the stamp.
"""

import ast
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request

import pytest

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TESTS_DIR, "live"))
import tool_verify  # noqa: E402
import verify_core  # noqa: E402  probe_capabilities/capability_skip_reason read their tables here
import verify_runner  # noqa: E402  source_hash reads SRC_ROOT/_HERE off ITS namespace, not the facade

_ATTESTATION = {"implementation_fingerprint": "a" * 64, "schema_fingerprint": "b" * 64,
                "load_id": "fixture-load", "session_id": "fixture-session"}


def _registered(names, writes=()):
    writes = set(writes)
    rows = [{"name": name, "annotations": {"readOnlyHint": name not in writes},
             "inputSchema": {"properties": (
                 {"expect_document": {}} if name in writes else {})}}
            for name in names]
    def registered(_health=None, include_rows=False):
        return (list(names), tuple(rows)) if include_rows else list(names)
    return registered


def _known_wire(tool, args):
    if tool == "doc_get":
        return False, {"active": {"name": "Untitled", "document_id": None,
                                   "document_handle": "session:test"}}
    if tool == "design_get":
        return False, {"feature_count": 1}
    return False, {"n": 1}


def _attested_health(identity=None):
    identity = identity or _ATTESTATION
    return {"server": tool_verify.SERVER_NAME, "version": "t",
            "session_id": identity["session_id"],
            "attestation": {"complete": True, "loaded_matches_source": True,
                            "implementation_fingerprint": identity["implementation_fingerprint"],
                            "schema_fingerprint": identity["schema_fingerprint"],
                            "load_id": identity["load_id"]}}


def _reloaded_attestation():
    return dict(_ATTESTATION, load_id="fixture-load-2", session_id="fixture-session-2")


def _tree(tmp_path, files):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return str(tmp_path)


class TestSourceHash:
    def test_same_tree_hashes_identically(self, tmp_path):
        root = _tree(tmp_path, {"a.py": b"x = 1\n", "sub/b.py": b"y = 2\n"})
        assert tool_verify.source_hash(root) == tool_verify.source_hash(root)

    def test_content_change_changes_hash(self, tmp_path):
        root = _tree(tmp_path, {"a.py": b"x = 1\n"})
        before = tool_verify.source_hash(root)
        (tmp_path / "a.py").write_bytes(b"x = 2\n")
        assert tool_verify.source_hash(root) != before

    def test_rename_changes_hash(self, tmp_path):
        a = _tree(tmp_path / "one", {"a.py": b"x = 1\n"})
        b = _tree(tmp_path / "two", {"b.py": b"x = 1\n"})
        assert tool_verify.source_hash(a) != tool_verify.source_hash(b)

    def test_crlf_and_lf_hash_identically(self, tmp_path):
        crlf = _tree(tmp_path / "crlf", {"a.py": b"x = 1\r\nif x:\r\n    pass\r\n"})
        lf = _tree(tmp_path / "lf", {"a.py": b"x = 1\nif x:\n    pass\n"})
        assert tool_verify.source_hash(crlf) == tool_verify.source_hash(lf)

    def test_non_py_and_pycache_are_ignored(self, tmp_path):
        root = _tree(tmp_path, {"a.py": b"x = 1\n"})
        before = tool_verify.source_hash(root)
        _tree(tmp_path, {"notes.md": b"prose\n", "data.json": b"{}\n",
                         "__pycache__/a.cpython-312.pyc": b"\x00",
                         "sub/__pycache__/b.py": b"cached = True\n"})
        assert tool_verify.source_hash(root) == before

    def test_paths_hash_with_forward_slashes(self, tmp_path):
        # Pins the separator normalization: the digest must be computable with '/' joined
        # relative paths regardless of the OS the receipt was written on.
        content = b"z = 3\n"
        root = _tree(tmp_path, {"sub/deep/c.py": content})
        expected = hashlib.sha256(b"sub/deep/c.py" + b"\0" + content + b"\0").hexdigest()
        assert tool_verify.source_hash(root) == expected


class TestHarnessSideOfTheHash:
    """Which tests/live modules the receipt binds: the SWEEP's, and only those."""

    @staticmethod
    def _rig(tmp_path, monkeypatch):
        src = _tree(tmp_path / "src", {"a.py": b"x = 1\n"})
        live = tmp_path / "live"
        live.mkdir()
        (live / "measure_api.py").write_bytes(b"ROWS = []\n")
        (live / "verify_core.py").write_bytes(b"EXCLUDED = {}\n")
        repo = tmp_path / "repo"
        _tree(repo, {"Fusion-Essentials.py": b"from . import commands\n",
                     "lib/loaded_attestation.py": b"def begin(): pass\n"})
        monkeypatch.setattr(verify_runner, "SRC_ROOT", src)
        monkeypatch.setattr(verify_runner, "REPO_ROOT", str(repo))
        monkeypatch.setattr(verify_runner, "_HERE", str(live))
        return src, live

    def test_capture_bootstrap_and_helper_are_bound_into_the_source_hash(
            self, tmp_path, monkeypatch):
        src, _live = self._rig(tmp_path, monkeypatch)
        before = tool_verify.source_hash(src)
        helper = Path(verify_runner.REPO_ROOT) / "lib/loaded_attestation.py"
        helper.write_bytes(b"def begin(): return False\n")
        assert tool_verify.source_hash(src) != before

    def test_a_facts_harness_edit_leaves_the_hash_where_a_predicate_edit_moves_it(
            self, tmp_path, monkeypatch):
        # measure_api.py judges no step of the sweep, so a row-only edit to it must not force a
        # seven-minute re-run; verify_core.py holds the predicates the receipt exists to bind.
        src, live = self._rig(tmp_path, monkeypatch)
        before = tool_verify.source_hash(src)
        (live / "measure_api.py").write_bytes(b"ROWS = [{'id': 'new-row'}]\n")
        assert tool_verify.source_hash(src) == before
        (live / "verify_core.py").write_bytes(b"EXCLUDED = {'model_loft': 'skipped'}\n")
        assert tool_verify.source_hash(src) != before

    def test_no_module_the_sweep_imports_is_left_out_of_the_hash(self):
        # The excluded names are safe only while the sweep does not read them: a predicate reached
        # through one of these would then ride under a green receipt that never saw it change.
        skipped = {fn[:-3] for fn in verify_runner._NOT_THE_SWEEP}
        importers = []
        for fn in sorted(os.listdir(tool_verify._HERE)):
            if not fn.endswith(".py") or fn in verify_runner._NOT_THE_SWEEP:
                continue
            with open(os.path.join(tool_verify._HERE, fn), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=fn)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                importers += [f"{fn} imports {n}" for n in names if n in skipped]
        assert not importers, ("the receipt skips a module the sweep reads: "
                               + ", ".join(importers))


class TestVerifiedReceipt:
    _LEDGER = [("appearance_set", "covered"),
               ("doc_open", "skipped: opens cloud files"),
               ("model_loft", "PENDING (no step yet)")]

    def test_round_trip_write_then_check_is_current(self, tmp_path, capsys):
        root = _tree(tmp_path / "src", {"a.py": b"x = 1\n"})
        receipt = str(tmp_path / "VERIFIED_TOOLS.md")
        tool_verify.write_verified(self._LEDGER, "2704.1.23", "2026-07-11",
                                   tool_verify.source_hash(root), path=receipt, attestation=_ATTESTATION)
        assert tool_verify.check(root=root, verified_path=receipt, attestation=_ATTESTATION) == 0
        assert "2026-07-11" in capsys.readouterr().out

    def test_check_goes_red_when_source_changes_after_stamp(self, tmp_path, capsys):
        root = _tree(tmp_path / "src", {"a.py": b"x = 1\n"})
        receipt = str(tmp_path / "VERIFIED_TOOLS.md")
        tool_verify.write_verified(self._LEDGER, "2704.1.23", "2026-07-11",
                                   tool_verify.source_hash(root), path=receipt, attestation=_ATTESTATION)
        (tmp_path / "src" / "a.py").write_bytes(b"x = 2\n")
        assert tool_verify.check(root=root, verified_path=receipt, attestation=_ATTESTATION) == 1
        out = capsys.readouterr().out
        assert "changed since" in out and "tool_verify.py" in out

    def test_check_goes_red_without_a_receipt(self, tmp_path, capsys):
        root = _tree(tmp_path / "src", {"a.py": b"x = 1\n"})
        assert tool_verify.check(root=root, verified_path=str(tmp_path / "VERIFIED_TOOLS.md")) == 1
        assert "tool_verify.py" in capsys.readouterr().out

    def test_check_goes_red_when_the_loaded_stamp_is_missing_or_changed(self, tmp_path, capsys):
        root = _tree(tmp_path / "src", {"a.py": b"x = 1\n"})
        receipt = str(tmp_path / "VERIFIED_TOOLS.md")
        tool_verify.write_verified(self._LEDGER, "2704.1.23", "fixture",
                                   tool_verify.source_hash(root), path=receipt,
                                   attestation=_ATTESTATION)
        original = Path(receipt).read_text(encoding="utf-8")
        Path(receipt).write_text("\n".join(
            line for line in original.splitlines() if not line.startswith("Loaded:")) + "\n",
            encoding="utf-8")
        assert tool_verify.check(root=root, verified_path=receipt) == 1
        Path(receipt).write_text(original, encoding="utf-8")
        changed = dict(_ATTESTATION, schema_fingerprint="c" * 64)
        assert tool_verify.check(root=root, verified_path=receipt, attestation=changed) == 1
        output = capsys.readouterr().out
        assert "source/loaded stamp" in output and "differs" in output

    def test_check_goes_red_on_a_stampless_receipt(self, tmp_path, capsys):
        root = _tree(tmp_path / "src", {"a.py": b"x = 1\n"})
        receipt = tmp_path / "VERIFIED_TOOLS.md"
        receipt.write_text("# Live tool verification\n\nno stamp here\n", encoding="utf-8")
        assert tool_verify.check(root=root, verified_path=str(receipt)) == 1
        assert "stamp" in capsys.readouterr().out

    def test_receipt_carries_stamp_counts_and_ledger_rows(self, tmp_path):
        root = _tree(tmp_path / "src", {"a.py": b"x = 1\n"})
        receipt = str(tmp_path / "VERIFIED_TOOLS.md")
        src_hash = tool_verify.source_hash(root)
        tool_verify.write_verified(self._LEDGER, "2704.1.23", "2026-07-11", src_hash, path=receipt, attestation=_ATTESTATION)
        with open(receipt, encoding="utf-8") as fh:
            text = fh.read()
        m = tool_verify._STAMP_RE.search(text)
        assert m and m.groups() == (src_hash, "2704.1.23", "2026-07-11")
        assert "1 covered / 0 called / 0 refusals-only / 1 skipped(reason) / 1 pending" in text
        assert "| appearance_set | covered |" in text
        assert "| doc_open | skipped: opens cloud files |" in text
        assert "| model_loft | PENDING (no step yet) |" in text

    def test_bare_ok_row_lands_in_called_and_a_value_row_in_covered(self, tmp_path):
        # The split the receipt exists to make visible: 'covered' counts only the tools whose step
        # read a value off the payload; a bare-ok tool is counted and labelled separately.
        root = _tree(tmp_path / "src", {"a.py": b"x = 1\n"})
        receipt = str(tmp_path / "VERIFIED_TOOLS.md")
        tool_verify.write_verified([("model_extrude", "covered"), ("model_create_component", "called")],
                                   "2704.1.23", "2026-07-11", tool_verify.source_hash(root),
                                   path=receipt, attestation=_ATTESTATION)
        with open(receipt, encoding="utf-8") as fh:
            text = fh.read()
        assert "1 covered / 1 called / 0 refusals-only / 0 skipped(reason) / 0 pending" in text
        assert "| model_extrude | covered |" in text
        assert "| model_create_component | called |" in text
        # both terms are defined in the header, or the numbers are unreadable
        assert "- covered:" in text and "- called:" in text

    def test_a_parked_row_carries_its_reason_and_still_counts_as_called(self, tmp_path):
        # A parked step's reason otherwise lives only in a source comment: the receipt would show a
        # plain 'called' and no reader could tell a missing predicate from a deliberately held one.
        # The reason is RENDERING, not a third bucket - the count line must not drift from it.
        root = _tree(tmp_path / "src", {"a.py": b"x = 1\n"})
        receipt = str(tmp_path / "VERIFIED_TOOLS.md")
        tool_verify.write_verified([("model_extrude", "covered"),
                                    ("model_draft", "called (effect read unreadable - DR-1)")],
                                   "2704.1.23", "2026-07-11", tool_verify.source_hash(root),
                                   path=receipt, attestation=_ATTESTATION)
        with open(receipt, encoding="utf-8") as fh:
            text = fh.read()
        assert "1 covered / 1 called / 0 refusals-only / 0 skipped(reason) / 0 pending" in text
        assert "| model_draft | called (effect read unreadable - DR-1) |" in text


class _Clock:
    """The time module as the runner sees it, with one elapsed reading: the first time() read is the
    run's start and every later one is `elapsed` past it."""

    def __init__(self, elapsed):
        self.elapsed, self.reads = elapsed, 0

    def time(self):
        self.reads += 1
        return 0.0 if self.reads == 1 else self.elapsed

    def sleep(self, seconds):
        pass

    def strftime(self, fmt):
        return "2026-07-11"


class _Harness:
    """run() over a stubbed wire, clock and results directory. What each test varies is the act
    program, the run id and what the wire answers; everything else is held still."""

    ACTS = [("ACT A", None, [("a_get", {}, "ok", ("k", lambda p: p["n"]))], []),
            # ACT B's ARGUMENTS read a ctx value ACT A saved - the thing a chunk boundary has to
            # carry, and the one whose loss shows up as a blocked step rather than an exception.
            ("ACT B", None,
             [("b_get", lambda ctx: {"x": ctx["k"]}, lambda p: p["n"] == 1, None)], [])]

    def __init__(self, monkeypatch, tmp_path, acts=None, elapsed=1.0, document="Untitled",
                 document_handle="session:fixture-document", features=7, src_hash="0" * 64,
                 answer=None, poll_after=None):
        self.wrote, self.seen = {}, []
        self.document, self.document_handle = document, document_handle
        self.features, self.answer = features, answer
        monkeypatch.setattr(tool_verify, "call", self._call)
        monkeypatch.setattr(verify_runner, "time", _Clock(elapsed))
        monkeypatch.setattr(verify_runner, "RESULTS_DIR", str(tmp_path))
        monkeypatch.setattr(tool_verify, "health_gate", _attested_health)
        monkeypatch.setattr(tool_verify, "registered_tools", _registered(["a_get", "b_get"]))
        monkeypatch.setattr(tool_verify, "source_hash", lambda *a, **k: src_hash)
        monkeypatch.setattr(tool_verify, "write_verified", self._write)
        reloaded = dict(_ATTESTATION, load_id="fixture-load-2",
                        session_id="fixture-session-2")
        monkeypatch.setattr(tool_verify, "reload_smoke",
                            lambda rows, notes, valued=None, **kw:
                            (self.seen.append(("reload beat", {})), reloaded)[1])
        monkeypatch.setattr(tool_verify, "POLL_AFTER", poll_after or {})
        monkeypatch.setattr(tool_verify, "EXCLUDED", {})
        monkeypatch.setattr(tool_verify, "STORY", {})
        monkeypatch.setattr(tool_verify, "ACT_NEEDS", {})
        monkeypatch.setattr(tool_verify, "ACTS", acts if acts is not None else self.ACTS)
        tool_verify._RECALL.clear()

    def _call(self, tool, args):
        self.seen.append((tool, dict(args)))
        if tool == "doc_get":
            return False, {"active": {"name": self.document, "document_id": None,
                                      "document_handle": self.document_handle}}
        if tool == "design_get":
            return False, {"feature_count": self.features}
        return False, self.answer if self.answer is not None else {"n": 1}

    def _write(self, rows, version, date, src_hash, path=None, notes=None, act_modes=None, attestation=None):
        self.wrote.update(ledger=dict(rows), src_hash=src_hash, attestation=attestation)
        return "VERIFIED_TOOLS.md"

    def tools_called(self):
        return [tool for tool, _args in self.seen]

    def run(self, **kw):
        return tool_verify.run(write_json=False, **kw)


class TestTheReceiptContract:
    """What a receipt depends on: the steps, and nothing else. A run is timed and the timing is
    reported, but a long run is a slow run - not a failed one."""

    def test_a_run_far_longer_than_a_shell_call_still_stamps(self, monkeypatch, tmp_path, capsys):
        # the sweep grew past 600 s of wall clock; capping it would refuse a receipt for a run whose
        # every step passed, which is the state this contract exists to end.
        h = _Harness(monkeypatch, tmp_path, elapsed=4000.0)
        assert h.run() == 0 and set(h.wrote["ledger"]) == {"a_get", "b_get"}
        out = capsys.readouterr().out
        assert "OVER BUDGET" not in out and "4000s" in out

    def test_a_failing_step_is_what_blocks_the_receipt(self, monkeypatch, tmp_path, capsys):
        acts = [("ACT A", None, [("a_get", {}, lambda p: p["n"] == 99, None)], [])]
        h = _Harness(monkeypatch, tmp_path, acts=acts)
        assert h.run() == 1 and not h.wrote
        assert "NOT rewritten" in capsys.readouterr().out

    def test_an_acts_own_poll_budget_reaches_the_generation_poll(self, monkeypatch, tmp_path):
        # a census act launches sixteen operations at once; the runner's default budget would end
        # the poll while the work is still running and fail an act that is only slow.
        polled = []
        monkeypatch.setattr(tool_verify, "poll_generation",
                            lambda rows, notes, setup, valued=None, max_polls=40:
                            polled.append((setup, max_polls)))
        acts = [("ACT P", None, [("a_get", {}, "ok", None)], []),
                ("ACT Q", None, [("a_get", {}, "ok", None)], [])]
        h = _Harness(monkeypatch, tmp_path, acts=acts, poll_after={
            "ACT P": {"narrative": "Mill", "fallback": [], "max_polls": 90},
            "ACT Q": {"narrative": "Turn", "fallback": []}})
        h.run()
        assert polled == [("Mill", 90), ("Turn", 40)]


class TestResumableRun:
    """A run walked in CHUNKS, because the shell a chunk is launched from is killed at 600 s. Each
    chunk saves what the next one reads; the receipt is stamped from the union, once."""

    def test_a_chunk_saves_the_program_it_walked_and_stamps_nothing(self, monkeypatch, tmp_path,
                                                                    capsys):
        h = _Harness(monkeypatch, tmp_path)
        assert h.run(run_id="r1", acts_spec="ACT A") == 0
        assert not h.wrote, "a chunk that has not walked the program cannot stamp its receipt"
        state = tool_verify.load_run_state("r1")
        assert state["acts_done"] == ["ACT A"] and state["ctx"] == {"k": 1}
        assert state["rows"] == [["a_get", "pass", ""]]
        # the document is saved as the chunk leaves it, so the next chunk can refuse another one
        assert state["document"]["name"] == "Untitled"
        assert "--run r1 --resume" in capsys.readouterr().out

    def test_the_resume_carries_the_ctx_and_stamps_from_the_union(self, monkeypatch, tmp_path):
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1", acts_spec="ACT A")
        second = _Harness(monkeypatch, tmp_path)
        assert second.run(run_id="r1", resume=True) == 0
        # only the act that had not run is walked...
        assert ("b_get", {"x": 1}) in second.seen and "a_get" not in second.tools_called()
        # ...and the receipt carries BOTH acts' tools, from the union of the two chunks' ledgers
        assert second.wrote["ledger"] == {"a_get": "called", "b_get": "covered"}
        # the reload beat belongs to the whole run: the first chunk must not fire it, the last must
        assert "reload beat" not in first.tools_called()
        assert "reload beat" in second.tools_called()

    def test_a_same_handle_survives_document_rename(self, monkeypatch, tmp_path):
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1", acts_spec="ACT A")
        second = _Harness(monkeypatch, tmp_path, document="Renamed document", features=8)
        assert second.run(run_id="r1", resume=True) == 0
        assert second.wrote["ledger"] == {"a_get": "called", "b_get": "covered"}

    def test_a_resume_with_missing_current_handle_is_refused(self, monkeypatch, tmp_path, capsys):
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1", acts_spec="ACT A")
        second = _Harness(monkeypatch, tmp_path, document_handle=None)
        assert second.run(run_id="r1", resume=True) == 1
        assert not second.wrote and "no complete document handle" in capsys.readouterr().out

    def test_a_resume_after_the_source_moved_is_refused(self, monkeypatch, tmp_path, capsys):
        # the receipt binds ONE hash: a chunk judged under different source would ride under a stamp
        # no chunk was measured against.
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1", acts_spec="ACT A")
        second = _Harness(monkeypatch, tmp_path, src_hash="f" * 64)
        assert second.run(run_id="r1", resume=True) == 1
        assert "b_get" not in second.tools_called() and not second.wrote
        assert "tool source changed" in capsys.readouterr().out

    def test_a_resume_against_another_document_is_refused(self, monkeypatch, tmp_path, capsys):
        # the remaining acts stand on the world the earlier ones built; run them against a fresh
        # document and every read is against geometry that was never made.
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1", acts_spec="ACT A")
        second = _Harness(monkeypatch, tmp_path, document="Untitled",
                          document_handle="session:other-document")
        assert second.run(run_id="r1", resume=True) == 1
        assert "b_get" not in second.tools_called() and not second.wrote
        out = capsys.readouterr().out
        assert "not the active one" in out and "session:other-document" in out

    @pytest.mark.parametrize("bad_handle", ["session:a b", "session:", "session: ", True, 1,
                                             {"handle": "session:valid"}],
                             ids=["internal-whitespace", "bare-prefix", "trailing-whitespace",
                                  "boolean", "integer", "mapping"])
    def test_malformed_equal_handles_are_refused_before_remaining_acts(
            self, monkeypatch, tmp_path, capsys, bad_handle):
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1", acts_spec="ACT A")
        state = tool_verify.load_run_state("r1")
        state["document"]["document_handle"] = bad_handle
        tool_verify.save_run_state("r1", state)
        second = _Harness(monkeypatch, tmp_path, document_handle=bad_handle)
        assert second.run(run_id="r1", resume=True) == 1
        assert "b_get" not in second.tools_called() and not second.wrote
        assert "no complete document handle" in capsys.readouterr().out

    def test_a_legacy_identity_without_handle_is_refused(self, monkeypatch, tmp_path, capsys):
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1", acts_spec="ACT A")
        state = tool_verify.load_run_state("r1")
        state["document"] = {"name": "Untitled", "document_id": None, "feature_count": 7}
        tool_verify.save_run_state("r1", state)
        second = _Harness(monkeypatch, tmp_path)
        assert second.run(run_id="r1", resume=True) == 1
        assert "b_get" not in second.tools_called() and not second.wrote
        assert "no complete document handle" in capsys.readouterr().out

    def test_a_resume_of_an_unknown_run_is_refused_naming_the_file(self, monkeypatch, tmp_path,
                                                                   capsys):
        h = _Harness(monkeypatch, tmp_path)
        assert h.run(run_id="nosuch", resume=True) == 1
        assert "run-nosuch.json" in capsys.readouterr().out and not h.wrote

    @staticmethod
    def _spy_saves(monkeypatch):
        """Every state a chunk saves, in order - the first is what a kill after act one leaves."""
        saved, real = [], verify_runner.save_run_state
        monkeypatch.setattr(verify_runner, "save_run_state",
                            lambda run_id, state: (saved.append(dict(state)), real(run_id, state))[1])
        return saved

    def test_a_first_chunk_saves_the_document_at_its_very_first_boundary(self, monkeypatch,
                                                                        tmp_path):
        # The false-receipt path this closes: a --run walk with no --acts is killed by the shell at
        # 600 s, so it never reaches its own ending. Every boundary save it made in the meantime has
        # to carry the document, or the resume has nothing to refuse a different one against.
        saved = self._spy_saves(monkeypatch)
        h = _Harness(monkeypatch, tmp_path)
        h.run(run_id="r1")
        assert saved, "a --run walk saves after every act"
        assert (saved[0].get("document") or {}).get("name") == "Untitled"
        assert all((s.get("document") or {}).get("name") == "Untitled" for s in saved)

    def test_that_killed_chunks_state_refuses_a_resume_in_another_document(self, monkeypatch,
                                                                          tmp_path, capsys):
        # the same state, resumed where the sweep's document is NOT the active one: the acts that
        # remain would run against a world nothing built, so the chunk must change nothing.
        saved = self._spy_saves(monkeypatch)
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1")
        verify_runner.save_run_state("r1", dict(saved[0], complete=False))   # as a kill left it
        second = _Harness(monkeypatch, tmp_path, document="A Totally Different Document",
                          document_handle="session:other-document")
        assert second.run(run_id="r1", resume=True) == 1
        assert not second.wrote and "b_get" not in second.tools_called()
        assert "session:other-document" in capsys.readouterr().out

    def test_a_state_with_no_document_identity_is_refused(self, monkeypatch, tmp_path, capsys):
        # the belt to that brace: a state saved before any boundary (or by an older run) names no
        # document, and "nothing to compare" must read as a refusal, not as a pass.
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1", acts_spec="ACT A")
        verify_runner.save_run_state("r1", dict(tool_verify.load_run_state("r1"), document=None))
        second = _Harness(monkeypatch, tmp_path)
        assert second.run(run_id="r1", resume=True) == 1
        assert not second.wrote and "no complete document handle" in capsys.readouterr().out

    def test_a_chunk_that_drove_no_act_says_so_on_the_stamp_line(self, monkeypatch, tmp_path,
                                                                capsys):
        # a chunk killed after its LAST boundary save leaves every act done and no completion flag:
        # the resume walks nothing, fires the reload beat and stamps from the saved ledger. It may
        # stamp - the acts did run - but the line that announces the receipt has to say what this
        # invocation contributed.
        saved = self._spy_saves(monkeypatch)
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1")
        verify_runner.save_run_state("r1", dict(saved[-1], complete=False))  # as a kill left it
        second = _Harness(monkeypatch, tmp_path)
        assert second.run(run_id="r1", resume=True) == 0 and second.wrote
        out = capsys.readouterr().out
        assert "0 acts driven this chunk - the reload beat only" in out
        assert "a_get" not in second.tools_called() and "b_get" not in second.tools_called()

    def test_a_resume_of_a_finished_run_is_refused(self, monkeypatch, tmp_path, capsys):
        # every act of a spent id is already done, so a resume would walk nothing and stamp the
        # receipt from the saved ledger - a receipt for a run that drove nothing this time.
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1")
        assert first.wrote
        second = _Harness(monkeypatch, tmp_path)
        assert second.run(run_id="r1", resume=True) == 1
        assert not second.wrote and "already walked the whole program" in capsys.readouterr().out

    def test_a_development_walk_reruns_the_named_act_with_the_saved_ctx(self, monkeypatch,
                                                                       tmp_path, capsys):
        # iterating on one act: the saved world's ctx feeds ACT B's arguments, only ACT B runs,
        # nothing stamps, and the state is left exactly as the chunk saved it.
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1", acts_spec="ACT A")
        before = tool_verify.load_run_state("r1")
        second = _Harness(monkeypatch, tmp_path)
        assert second.run(run_id="r1", resume=True, acts_spec="ACT B") == 0
        assert ("b_get", {"x": 1}) in second.seen and "a_get" not in second.tools_called()
        assert not second.wrote and "reload beat" not in second.tools_called()
        assert tool_verify.load_run_state("r1") == before
        assert "development walk" in capsys.readouterr().out

    def test_a_development_walk_runs_an_act_already_done_and_tolerates_a_moved_source(
            self, monkeypatch, tmp_path):
        # the act was edited since the chunk ran it: the hash moved, and that is the point.
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1", acts_spec="ACT A")
        second = _Harness(monkeypatch, tmp_path, src_hash="f" * 64)
        assert second.run(run_id="r1", resume=True, acts_spec="ACT A") == 0
        assert "a_get" in second.tools_called() and not second.wrote
        assert tool_verify.load_run_state("r1")["acts_done"] == ["ACT A"]

    def test_a_development_walk_in_another_document_is_still_refused(self, monkeypatch, tmp_path,
                                                                     capsys):
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1", acts_spec="ACT A")
        second = _Harness(monkeypatch, tmp_path, document="Something Else",
                          document_handle="session:other-document")
        assert second.run(run_id="r1", resume=True, acts_spec="ACT B") == 1
        assert "b_get" not in second.tools_called() and "not the active one" in capsys.readouterr().out

    def test_starting_a_run_id_that_holds_state_is_refused_before_any_act(self, monkeypatch,
                                                                         tmp_path, capsys):
        # the trap: --run <id> --acts without --resume on a used id ran the acts with an empty ctx
        # and rewrote the saved progress; now it stops before the first wire call.
        first = _Harness(monkeypatch, tmp_path)
        first.run(run_id="r1", acts_spec="ACT A")
        before = tool_verify.load_run_state("r1")
        second = _Harness(monkeypatch, tmp_path)
        assert second.run(run_id="r1", acts_spec="ACT B") == 1
        assert second.seen == [] and not second.wrote
        assert tool_verify.load_run_state("r1") == before
        assert "already holds state" in capsys.readouterr().out

    def test_a_single_invocation_is_unchanged_by_the_run_id_machinery(self, monkeypatch, tmp_path):
        # the path everyone runs: no run id, no state file, one stamp at the end.
        h = _Harness(monkeypatch, tmp_path)
        assert h.run() == 0 and h.wrote["ledger"] == {"a_get": "called", "b_get": "covered"}
        assert not os.path.exists(tool_verify.run_state_path("r1"))

    def test_a_value_a_predicate_recalls_survives_the_boundary(self, monkeypatch, tmp_path):
        # _RECALL is module state, not ctx: a predicate that recalls a name saved two acts ago reads
        # nothing in a fresh process unless the chunk carried it.
        acts = [("ACT A", None,
                 [("a_get", {}, "ok", ("k", tool_verify._recall("landed", lambda p: p["n"])))], []),
                ("ACT B", None, [("b_get", {}, lambda p: p["n"] == 1, None)], [])]
        first = _Harness(monkeypatch, tmp_path, acts=acts)
        first.run(run_id="r1", acts_spec="ACT A")
        assert tool_verify.load_run_state("r1")["recall"] == {"landed": 1}
        second = _Harness(monkeypatch, tmp_path, acts=acts)      # clears _RECALL, as a new process does
        second.run(run_id="r1", resume=True)
        assert tool_verify._RECALL["landed"] == 1

    def test_a_ctx_value_that_cannot_be_saved_is_refused_by_name(self, monkeypatch, tmp_path):
        # the failure this replaces is silent: a value stringified into the state file resumes into
        # a step that fails on a bad reference, three acts later.
        with pytest.raises(TypeError) as refused:
            tool_verify.save_run_state("r1", {"ctx": {"good": 1, "handle": object()}})
        assert "handle" in str(refused.value) and "good" not in str(refused.value)

    def test_an_interrupted_serialization_preserves_the_previous_checkpoint(
            self, monkeypatch, tmp_path):
        monkeypatch.setattr(verify_runner, "RESULTS_DIR", str(tmp_path))
        previous = {"ctx": {"saved": 1}, "recall": {}, "acts_done": ["ACT A"]}
        path = Path(tool_verify.save_run_state("r1", previous))
        before = path.read_bytes()

        real_dump = verify_runner.json.dump

        def interrupted_dump(value, stream, indent=None):
            if indent == 2:
                stream.write('{"truncated":')
                raise OSError("write interrupted")
            return real_dump(value, stream, indent=indent)

        monkeypatch.setattr(verify_runner.json, "dump", interrupted_dump)
        with pytest.raises(OSError, match="interrupted"):
            tool_verify.save_run_state("r1", {"ctx": {"new": 2}, "recall": {}})
        assert path.read_bytes() == before
        assert list(tmp_path.iterdir()) == [path]

    @pytest.mark.parametrize("failure", ["write", "sync", "replace"])
    @pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
    def test_a_failed_checkpoint_preserves_the_previous_state(
            self, monkeypatch, tmp_path, failure, existing):
        monkeypatch.setattr(verify_runner, "RESULTS_DIR", str(tmp_path))
        path = Path(tool_verify.run_state_path("r1"))
        before = None
        if existing:
            tool_verify.save_run_state("r1", {
                "ctx": {"saved": 1}, "recall": {}, "acts_done": ["ACT A"]})
            before = path.read_bytes()
        if failure == "write":
            real_fdopen = verify_runner.os.fdopen

            class FailedWriter:
                def __init__(self, *args, **kwargs):
                    self.file = real_fdopen(*args, **kwargs)

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self.file.close()

                def write(self, value):
                    raise OSError("write failed")

            monkeypatch.setattr(verify_runner.os, "fdopen", FailedWriter)
        elif failure == "sync":
            monkeypatch.setattr(verify_runner.os, "fsync",
                                lambda fd: (_ for _ in ()).throw(OSError("sync failed")))
        else:
            monkeypatch.setattr(verify_runner.os, "replace",
                                lambda source, target: (_ for _ in ()).throw(OSError("replace failed")))
        with pytest.raises(OSError, match="failed"):
            tool_verify.save_run_state("r1", {"ctx": {"new": 2}, "recall": {}})
        if existing:
            assert path.read_bytes() == before
            assert list(tmp_path.iterdir()) == [path]
        else:
            assert not path.exists()
            assert list(tmp_path.iterdir()) == []

    def test_a_capability_skipped_act_is_checkpointed_before_the_next_act(
            self, monkeypatch, tmp_path):
        acts = [
            ("ACT G", None, [("a_get", {}, "ok", None)], []),
            ("ACT B", None, [("b_get", {}, lambda payload: payload["n"] == 1, None)], []),
        ]
        first = _Harness(monkeypatch, tmp_path, acts=acts)
        monkeypatch.setattr(tool_verify, "ACT_NEEDS", {"ACT G": "fake_tier"})
        probes = []
        monkeypatch.setattr(verify_runner, "probe_capabilities",
                            lambda capabilities: probes.append(tuple(capabilities))
                            or {"fake_tier": False})
        first_call = first._call

        def interrupted_call(tool, args):
            if tool == "b_get":
                raise RuntimeError("chunk interrupted")
            return first_call(tool, args)

        monkeypatch.setattr(tool_verify, "call", interrupted_call)
        with pytest.raises(RuntimeError, match="chunk interrupted"):
            first.run(run_id="r1")
        saved = tool_verify.load_run_state("r1")
        assert saved["acts_done"] == ["ACT G"]
        assert saved["act_modes"] == [["ACT G", "skipped(fake_tier not entitled)"]]
        assert saved["gated"] == {"a_get": "fake_tier not entitled"}
        assert saved["entitlements"] == {"fake_tier": False}
        assert saved["source_hash"] == "0" * 64
        assert saved["attestation"] == _ATTESTATION
        assert saved["document"]["name"] == "Untitled"

        second = _Harness(monkeypatch, tmp_path, acts=acts)
        monkeypatch.setattr(tool_verify, "ACT_NEEDS", {"ACT G": "fake_tier"})
        monkeypatch.setattr(verify_runner, "probe_capabilities",
                            lambda capabilities: (_ for _ in ()).throw(
                                AssertionError("completed gated act was repeated")))
        assert second.run(run_id="r1", resume=True) == 0
        assert "a_get" not in second.tools_called()
        assert second.wrote["ledger"] == {
            "a_get": "skipped: fake_tier not entitled", "b_get": "covered"}


class TestSourceDriftDuringRun:
    def test_source_change_during_act_without_run_id_blocks_receipt(self, monkeypatch, tmp_path):
        h = _Harness(monkeypatch, tmp_path)
        digest = ["0" * 64]
        monkeypatch.setattr(tool_verify, "source_hash", lambda *a, **k: digest[0])
        real_call = h._call
        def call(tool, args):
            result = real_call(tool, args)
            if tool == "a_get":
                digest[0] = "f" * 64
            return result
        monkeypatch.setattr(tool_verify, "call", call)
        assert h.run() == 1
        assert not h.wrote and "b_get" not in h.tools_called()

    def test_source_change_during_act_blocks_receipt_and_checkpoint(self, monkeypatch, tmp_path):
        h = _Harness(monkeypatch, tmp_path)
        digest = ["0" * 64]
        monkeypatch.setattr(tool_verify, "source_hash", lambda *a, **k: digest[0])
        real_call = h._call
        def call(tool, args):
            result = real_call(tool, args)
            if tool == "a_get":
                digest[0] = "f" * 64
            return result
        monkeypatch.setattr(tool_verify, "call", call)
        assert h.run(run_id="drift") == 1
        assert not h.wrote and not os.path.exists(verify_runner.run_state_path("drift"))

    def test_source_change_during_reload_blocks_receipt(self, monkeypatch, tmp_path):
        h = _Harness(monkeypatch, tmp_path)
        digest = ["0" * 64]
        monkeypatch.setattr(tool_verify, "source_hash", lambda *a, **k: digest[0])
        def reload(*args, **kwargs):
            digest[0] = "f" * 64
        monkeypatch.setattr(tool_verify, "reload_smoke", reload)
        assert h.run() == 1
        assert not h.wrote

    def test_stable_hash_is_saved_at_checkpoint(self, monkeypatch, tmp_path):
        h = _Harness(monkeypatch, tmp_path)
        assert h.run(run_id="stable", acts_spec="ACT A") == 0
        assert tool_verify.load_run_state("stable")["source_hash"] == "0" * 64

    def test_source_change_on_gated_act_blocks_checkpoint(self, monkeypatch, tmp_path):
        h = _Harness(monkeypatch, tmp_path, acts=[("ACT G", None, [("a_get", {}, "ok", None)], [])])
        digest = ["0" * 64]
        monkeypatch.setattr(tool_verify, "source_hash", lambda *a, **k: digest[0])
        monkeypatch.setattr(tool_verify, "ACT_NEEDS", {"ACT G": "fake_tier"})
        def probe(_caps):
            digest[0] = "f" * 64
            return {"fake_tier": False}
        monkeypatch.setattr(verify_runner, "probe_capabilities", probe)
        assert h.run(run_id="gated") == 1
        assert not h.wrote and not os.path.exists(verify_runner.run_state_path("gated"))


class TestPredicateKind:
    """The classifier behind the split: it reads the step's EXPECTATION object, nothing else."""

    def test_bare_ok_string_is_a_call(self):
        assert tool_verify.predicate_kind("ok") == "call"

    def test_a_lambda_reading_the_payload_is_a_value(self):
        assert tool_verify.predicate_kind(lambda p: p.get("result_bodies")) == "value"

    def test_a_named_predicate_function_is_a_value(self):
        assert tool_verify.predicate_kind(tool_verify._repair_no_op) == "value"

    def test_a_constant_lambda_is_a_call_not_a_value(self):
        # 'lambda p: True' wears a predicate's shape and inspects nothing - counting it as covered
        # is exactly the inflation this split exists to prevent.
        assert tool_verify.predicate_kind(lambda p: True) == "call"

    def test_a_lambda_reading_a_closure_but_not_the_payload_is_a_call(self):
        want = 3
        assert tool_verify.predicate_kind(lambda p: want > 2) == "call"

    def test_refusal_strings_and_fragment_refusals_are_refusals(self):
        assert tool_verify.predicate_kind("refused") == "refusal"
        assert tool_verify.predicate_kind(tool_verify._refused("no such body")) == "refusal"

    def test_run_steps_returns_one_row_per_step_in_order(self, monkeypatch):
        # The receipt's covered/called split pairs each result row back with the EXPECTATION that
        # judged it by position, so a step that produced no row (or two) would attribute a value
        # predicate to the wrong tool. A blocked step still gets its row.
        monkeypatch.setattr(tool_verify, "call", _known_wire)
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        steps = [
            ("a_get", {}, "ok", None),
            ("b_get", {}, lambda p: p.get("n") == 1, None),
            ("c_get", lambda ctx: {"x": ctx["missing"]}, "ok", None),   # blocked: no ctx key
            ("d_get", {}, lambda p: p.get("n") == 2, None),             # predicate fails
        ]
        rows = tool_verify.run_steps(steps, {})
        assert [r[0] for r in rows] == ["a_get", "b_get", "c_get", "d_get"]
        assert [r[1] for r in rows] == ["pass", "pass", "blocked", "FAIL"]

    def test_the_step_engine_names_its_call_before_making_it(self, monkeypatch, capsys):
        # A redirected sweep log buffers per act, so the line naming a step has to be flushed
        # BEFORE the call: a step that kills the Fusion process leaves no line printed after it.
        def _call(tool, args):
            print(f"CALLED {tool}")
            return False, {"n": 1}
        monkeypatch.setattr(tool_verify, "call", _call)
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        tool_verify.run_steps([("a_get", {}, "ok", None)], {}, act="ACT 3")
        printed = [ln.strip() for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert printed == ["-> a_get ACT 3", "CALLED a_get"]

    def test_judged_steps_and_run_steps_agree_on_every_step_kind(self, monkeypatch):
        # The by-position pairing needs these two lists to be the same length, and each decides
        # what yields a row through _leaves_no_row. A second row-less step kind taught to one site
        # alone would shift the pairing again with every other test here still green.
        monkeypatch.setattr(tool_verify, "call", _known_wire)
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        steps = [
            ("bare_ok", {}, "ok", None),
            tool_verify._dwell(0),
            ("value_pred", {}, lambda p: p["n"] == 1, None),
            ("refusal", {}, "refused", None),
            ("blocked", lambda ctx: {"x": ctx["missing"]}, "ok", None),
            ("failing", {}, lambda p: p["n"] == 99, None),
            ("with_save", {}, "ok", ("k", lambda p: p["n"])),
            tool_verify._dwell(0),
        ]
        assert len(tool_verify.judged_steps(steps)) == len(tool_verify.run_steps(steps, {}))

    def test_a_dwell_does_not_shift_predicate_attribution(self, monkeypatch):
        # A _dwell is judged by nothing and leaves no row, so it is the one step that can break the
        # by-position pairing: run over the act's raw list and every expectation after a dwell is
        # credited to a LATER step's tool - a bare "ok" reads as covered and a value predicate is
        # lost. judged_steps is what the pairing runs over.
        monkeypatch.setattr(tool_verify, "call", _known_wire)
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        steps = [
            ("a_get", {}, "ok", None),
            tool_verify._dwell(0),
            ("b_get", {}, "ok", None),
            ("c_get", {}, lambda p: p["n"] == 1, None),
        ]
        rows = tool_verify.run_steps(steps, {})
        paired = [(row[0], tool_verify.predicate_kind(step[2]))
                  for step, row in zip(tool_verify.judged_steps(steps), rows)]
        assert paired == [("a_get", "call"), ("b_get", "call"), ("c_get", "value")]

    def test_run_credits_covered_to_the_tool_whose_own_step_read_a_value(self, monkeypatch):
        # The pairing AT ITS CALL SITE, over a one-act program holding the shape that breaks it: a
        # dwell between the bare-ok rows and the value predicate. Whichever list run() pairs with
        # the rows decides the ledger, so this is what a revert to zip(steps, ...) has to fail.
        ledger = {}

        def fake_write(rows, version, date, src_hash, path=None, notes=None, act_modes=None, attestation=None):
            ledger.update(rows)
            return "VERIFIED_TOOLS.md"

        monkeypatch.setattr(tool_verify, "call", _known_wire)
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        monkeypatch.setattr(tool_verify, "health_gate", _attested_health)
        monkeypatch.setattr(tool_verify, "registered_tools", _registered(["a_get", "b_get", "c_get"]))
        monkeypatch.setattr(tool_verify, "source_hash", lambda *a, **k: "0" * 64)
        monkeypatch.setattr(tool_verify, "write_verified", fake_write)
        monkeypatch.setattr(tool_verify, "reload_smoke",
                            lambda rows, notes, valued=None, **kw: _reloaded_attestation())
        monkeypatch.setattr(tool_verify, "POLL_AFTER", {})
        monkeypatch.setattr(tool_verify, "EXCLUDED", {})
        monkeypatch.setattr(tool_verify, "STORY", {})
        monkeypatch.setattr(tool_verify, "ACTS", [("ACT T", None, [
            ("a_get", {}, "ok", None),
            tool_verify._dwell(0),
            ("b_get", {}, "ok", None),
            ("c_get", {}, lambda p: p["n"] == 1, None),
        ], None)])

        assert tool_verify.run(write_json=False) == 0
        assert ledger == {"a_get": "called", "b_get": "called", "c_get": "covered"}

    def test_every_steps_row_classifies_and_no_constant_predicate_hides_in_them(self):
        # A callable in STEPS that never READS INTO its payload would be counted 'covered' on this
        # run while proving nothing; there are none, and this is what keeps it that way. Every
        # callable expectation in STEPS therefore has to satisfy the guard below.
        kinds = [tool_verify.predicate_kind(step[2]) for step in tool_verify.STEPS]
        assert set(kinds) <= {"value", "call", "refusal"}
        constants = [step[0] for step in tool_verify.STEPS
                     if callable(tool_verify._unparked(step[2]))
                     and not isinstance(tool_verify._unparked(step[2]), tool_verify._Refusal)
                     and tool_verify.predicate_kind(step[2]) == "call"]
        assert not constants, ("STEPS rows whose predicate never reads the payload: "
                               + ", ".join(sorted(set(constants))))


def _reads_a_key(payload):
    """A module-level helper that DOES read the payload - the callee side of `lambda p: helper(p)`."""
    return payload.get("result_bodies")


class TestValuePredicateGuard:
    """Loading the argument is not the same as reading it. Each shape below TOUCHES the payload and
    inspects nothing in it, and each would otherwise be bucketed 'covered' while proving no more
    than a bare "ok" - the exact inflation the covered/called split exists to prevent."""

    def test_a_truthiness_test_on_the_payload_is_a_call(self):
        assert tool_verify.predicate_kind(lambda p: p and True) == "call"

    def test_bool_of_the_payload_is_a_call(self):
        assert tool_verify.predicate_kind(lambda p: bool(p)) == "call"

    def test_a_none_check_on_the_payload_is_a_call(self):
        assert tool_verify.predicate_kind(lambda p: p is not None) == "call"

    def test_an_attribute_read_off_the_payload_is_a_value(self):
        assert tool_verify.predicate_kind(lambda p: p.get("moved") is True) == "value"

    def test_a_subscript_of_the_payload_is_a_value(self):
        assert tool_verify.predicate_kind(lambda p: p["joints"][0]["name"] == "Yaw") == "value"

    def test_handing_the_payload_to_a_reading_helper_is_a_value(self):
        assert tool_verify.predicate_kind(lambda p: _reads_a_key(p)) == "value"

    def test_a_payload_captured_by_an_inner_comprehension_is_still_a_value(self):
        # Closing over the payload inside the predicate makes it a CELL, so every read of it
        # compiles to LOAD_DEREF instead of LOAD_FAST - a real read either way.
        assert tool_verify.predicate_kind(
            lambda p: all(r.get("name") for r in (p.get("joints") or []))) == "value"


class TestParkedSteps:
    """Parked carries a ledger REASON, never a judgement."""

    def test_predicate_kind_reads_through_the_marker(self):
        assert tool_verify.predicate_kind(tool_verify.Parked("held")) == "call"
        assert tool_verify.predicate_kind(
            tool_verify.Parked("held", lambda p: p.get("n"))) == "value"
        assert tool_verify.predicate_kind(tool_verify.Parked("held", "refused")) == "refusal"

    def test_a_parked_step_is_judged_by_the_expectation_inside_it(self, monkeypatch):
        monkeypatch.setattr(tool_verify, "call", _known_wire)
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        rows = tool_verify.run_steps([
            ("a_get", {}, tool_verify.Parked("held at a bare ok"), None),
            ("b_get", {}, tool_verify.Parked("held", lambda p: p.get("n") == 2), None),
        ], {})
        assert [r[1] for r in rows] == ["pass", "FAIL"]


class TestCapabilityProbe:
    """The machining_extension probe reads workspace_orient's own entitlement block. Entitled means
    every sentinel flag answered TRUE; anything it could not read is None, which routes as unmet."""

    @staticmethod
    def _wire(monkeypatch, answer):
        monkeypatch.setattr(tool_verify, "call", lambda tool, args: answer)

    def test_all_four_sentinels_true_is_entitled(self, monkeypatch):
        self._wire(monkeypatch, (False, {"machining_capabilities": {"observed_generation": {
            "steep_and_shallow": True, "multiaxis_finishing": True,
            "swarf": True, "probe_geometry": True}}}))
        assert tool_verify._machining_extension_probe() is True

    def test_one_sentinel_false_is_not_entitled(self, monkeypatch):
        self._wire(monkeypatch, (False, {"machining_capabilities": {"observed_generation": {
            "steep_and_shallow": True, "multiaxis_finishing": True,
            "swarf": False, "probe_geometry": True}}}))
        assert tool_verify._machining_extension_probe() is False

    def test_a_null_flag_is_unreadable_not_entitled(self, monkeypatch):
        # null is 'the probe could not read it'. Reading it as False would report an entitlement
        # verdict nothing measured; reading it as True would run rows this licence cannot generate.
        self._wire(monkeypatch, (False, {"machining_capabilities": {"observed_generation": {
            "steep_and_shallow": True, "multiaxis_finishing": None,
            "swarf": True, "probe_geometry": True}}}))
        assert tool_verify._machining_extension_probe() is None

    def test_a_missing_block_and_a_failed_read_are_both_unreadable(self, monkeypatch):
        self._wire(monkeypatch, (False, {"document": {"name": "X"}}))
        assert tool_verify._machining_extension_probe() is None
        self._wire(monkeypatch, (True, "no active document"))
        assert tool_verify._machining_extension_probe() is None


def _licence_partition(acts, act_needs, opt_in):
    """(held, free) over an act program: the tools a LICENCE capability holds back, and the tools
    some ungated step drives.

    An OPT-IN TIER's steps are in NEITHER set. They are not licence-held; and reading them as
    ungated would let a cloud step stand in as a licence-gated tool's base-licence variant - a
    variant that runs on no default sweep, which is the opposite of what the invariant asks for."""
    tiered_acts = {a for a, cap in act_needs.items() if cap in opt_in}
    gated_acts = set(act_needs) - tiered_acts
    held, free = set(), set()
    for name, _pre, narr, fb in acts:
        for step in (list(narr) + list(fb or [])):
            if step[0] == tool_verify._DWELL:
                continue
            cap = tool_verify.step_capability(step[2])
            if name in tiered_acts or cap in opt_in:
                continue
            (held if (name in gated_acts or cap is not None) else free).add(step[0])
    return held, free


class TestCapabilityTier:
    """The tier itself: what a declaration means, and which receipt bucket an unmet one lands in."""

    _FAKE = {"yes": lambda: True, "no": lambda: False, "dunno": lambda: None}

    def test_each_declared_capability_is_probed_once(self):
        calls = []
        probes = {"yes": lambda: calls.append("yes") or True}
        assert tool_verify.probe_capabilities(["yes", "yes"], probes) == {"yes": True}
        assert calls == ["yes"]

    def test_an_unregistered_capability_answers_unreadable(self):
        # a typo in a declaration must not read as entitled - there is no probe to say it is.
        assert tool_verify.probe_capabilities(["nosuch"], self._FAKE) == {"nosuch": None}

    def test_only_a_true_probe_is_met(self):
        ent = tool_verify.probe_capabilities(["yes", "no", "dunno"], self._FAKE)
        assert tool_verify.capability_met(ent, "yes") is True
        assert tool_verify.capability_met(ent, "no") is False
        assert tool_verify.capability_met(ent, "dunno") is False
        assert tool_verify.capability_met(ent, None) is True

    def test_the_skip_reason_separates_not_entitled_from_unreadable(self):
        ent = {"no": False, "dunno": None}
        assert tool_verify.capability_skip_reason("no", ent) == "no not entitled"
        assert "probe did not read" in tool_verify.capability_skip_reason("dunno", ent)

    def test_a_capability_detail_rides_the_skip_reason(self):
        # The detail is what an operator reading a skipped row acts on. A capability with no entry
        # keeps the bare verdict, so the two shapes cannot merge.
        assert tool_verify.capability_skip_reason("no", {"no": False}) == "no not entitled"
        reason = tool_verify.capability_skip_reason(tool_verify.CLOUD_TIER, {"cloud_tier": False})
        assert reason.startswith("cloud_tier not entitled")
        assert "cloud_config.local.json" in reason and "hub, project, folder" in reason

    @staticmethod
    def _detailed(monkeypatch):
        """A capability whose probe answers false and whose skip row carries a detail sentence."""
        monkeypatch.setitem(verify_core.CAPABILITY_PROBES, "fake_tier", lambda: False)
        monkeypatch.setitem(verify_core.CAPABILITY_DETAIL, "fake_tier", "opt-in: write the config")

    def test_a_gated_acts_ledger_row_carries_the_capability_detail(self, monkeypatch):
        # consumer one: the ACT-level skip in run(). A row saying only 'not entitled' leaves the
        # reader with no way to turn the tier on.
        self._detailed(monkeypatch)
        acts = [("ACT F", None, [("a_get", {}, "ok", None)], [("fb_get", {}, "ok", None)])]
        ledger, _seen, modes = self._run(monkeypatch, acts, {"ACT F": "fake_tier"}, entitled=False,
                                         tools=["a_get", "fb_get"])
        assert ledger["a_get"] == "skipped: fake_tier not entitled (opt-in: write the config)"
        assert ledger["fb_get"] == ledger["a_get"]
        assert modes == [("ACT F", "skipped(fake_tier not entitled (opt-in: write the config))")]

    def test_a_gated_steps_ledger_row_carries_the_capability_detail(self, monkeypatch):
        # consumer two: the per-STEP skip in run(), which words its bucket through the same helper.
        self._detailed(monkeypatch)
        acts = [("ACT S", None, [
            ("a_get", {}, "ok", None),
            ("gated_get", {}, tool_verify._needs("fake_tier", lambda p: p["n"] == 1), None),
        ], [])]
        ledger, seen, _modes = self._run(monkeypatch, acts, {}, entitled=False,
                                         tools=["a_get", "gated_get"])
        assert ledger["gated_get"] == ("skipped: fake_tier not entitled "
                                       "(opt-in: write the config)")
        assert seen == ["a_get"]

    def test_the_declaration_is_read_through_both_wrappers(self):
        needs = tool_verify._needs("yes", lambda p: p.get("n"))
        assert tool_verify.step_capability(needs) == "yes"
        assert tool_verify.step_capability(tool_verify.Parked("held", needs)) == "yes"
        assert tool_verify.step_capability(tool_verify.Parked("held")) is None
        assert tool_verify.step_capability("ok") is None

    def test_a_gate_changes_the_bucket_and_never_the_judgement(self):
        # Needs carries ledger routing only: the expectation inside is what judges the step, so the
        # covered/called/refusal split must read straight through it.
        assert tool_verify.predicate_kind(tool_verify._needs("yes")) == "call"
        assert tool_verify.predicate_kind(tool_verify._needs("yes", lambda p: p["n"])) == "value"
        assert tool_verify.predicate_kind(tool_verify._needs("yes", "refused")) == "refusal"
        assert tool_verify.predicate_kind(
            tool_verify._needs("yes", tool_verify.Parked("held", lambda p: p["n"]))) == "value"

    def test_a_parked_reason_survives_a_gate_around_it(self):
        assert tool_verify.parked_reason(
            tool_verify._needs("yes", tool_verify.Parked("held at a bare ok"))) == "held at a bare ok"
        assert tool_verify.parked_reason("ok") is None

    @staticmethod
    def _run(monkeypatch, acts, act_needs, entitled, tools, poll_after=None):
        """run() over a stubbed wire, returning (ledger, the tools the wire was actually called
        with, the act modes)."""
        out = {}
        seen = []

        def fake_write(rows, version, date, src_hash, path=None, notes=None, act_modes=None, attestation=None):
            out["ledger"] = dict(rows)
            out["act_modes"] = list(act_modes or [])
            return "VERIFIED_TOOLS.md"

        def call(tool, args):
            seen.append(tool)
            if tool == "workspace_orient":
                flags = {n: entitled for n in ("steep_and_shallow", "multiaxis_finishing",
                                               "swarf", "probe_geometry")}
                return False, {"machining_capabilities": {"observed_generation": flags}}
            if tool == "doc_get":
                return False, {"active": {"name": "Untitled", "document_id": None,
                                           "document_handle": "session:test"}}
            if tool == "design_get":
                return False, {"feature_count": 1}
            if tool == "doc_new":
                return False, {"created": True, "document_handle": "session:test"}
            return False, {"n": 1}

        monkeypatch.setattr(tool_verify, "call", call)
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        monkeypatch.setattr(tool_verify, "health_gate", _attested_health)
        monkeypatch.setattr(
            tool_verify, "registered_tools",
            _registered(sorted(tools), {"doc_new"} if "doc_new" in tools else ()))
        monkeypatch.setattr(tool_verify, "source_hash", lambda *a, **k: "0" * 64)
        monkeypatch.setattr(tool_verify, "write_verified", fake_write)
        # the post-run reload beat is another act's business; stubbed so 'seen' is this tier's
        monkeypatch.setattr(tool_verify, "reload_smoke",
                            lambda rows, notes, valued=None, **kw: _reloaded_attestation())
        monkeypatch.setattr(tool_verify, "POLL_AFTER", poll_after or {})
        monkeypatch.setattr(tool_verify, "EXCLUDED", {})
        monkeypatch.setattr(tool_verify, "STORY", {})
        monkeypatch.setattr(tool_verify, "ACT_NEEDS", act_needs)
        monkeypatch.setattr(tool_verify, "ACTS", acts)
        assert tool_verify.run(write_json=False) == 0
        return out["ledger"], seen, out["act_modes"]

    def test_a_gated_act_runs_nothing_and_banks_the_capability_bucket(self, monkeypatch):
        # BOTH lanes bank the bucket: neither ran, so a tool the fallback alone drives is skipped
        # for the same reason - reported PENDING, it would read as never scripted.
        acts = [("ACT E", ("pre_get", {}), [("a_get", {}, lambda p: p["n"] == 1, None)],
                 [("fb_get", {}, "ok", None)])]
        ledger, seen, modes = self._run(
            monkeypatch, acts, {"ACT E": "machining_extension"}, entitled=False,
            tools=["a_get", "fb_get", "pre_get"])
        assert ledger["a_get"] == "skipped: machining_extension not entitled"
        assert ledger["fb_get"] == "skipped: machining_extension not entitled"
        # not even the act's PRECONDITION read fires - it decides between two lanes neither of
        # which may run, and a wire call for that is a call for nothing.
        assert seen == ["workspace_orient"]
        assert modes == [("ACT E", "skipped(machining_extension not entitled)")]

    def test_the_same_act_runs_where_the_capability_is_entitled(self, monkeypatch):
        acts = [("ACT E", None, [("a_get", {}, lambda p: p["n"] == 1, None)], [])]
        ledger, seen, modes = self._run(
            monkeypatch, acts, {"ACT E": "machining_extension"}, entitled=True, tools=["a_get"])
        assert ledger["a_get"] == "covered"
        assert seen == ["workspace_orient", "a_get"]
        assert modes == [("ACT E", "narrative")]

    def test_a_gated_step_is_dropped_without_shifting_the_rows_after_it(self, monkeypatch):
        # The bug this exists to catch is silent and green: a gated step that still yielded a row
        # would pair every later expectation with the wrong tool, so a bare "ok" would read as
        # covered and a value predicate would be lost.
        acts = [("ACT T", None, [
            ("a_get", {}, "ok", None),
            ("gated_get", {}, tool_verify._needs("machining_extension", lambda p: p["n"] == 1), None),
            ("c_get", {}, lambda p: p["n"] == 1, None),
        ], [])]
        ledger, seen, _modes = self._run(
            monkeypatch, acts, {}, entitled=False, tools=["a_get", "gated_get", "c_get"])
        assert ledger == {"a_get": "called",
                          "gated_get": "skipped: machining_extension not entitled",
                          "c_get": "covered"}
        assert seen == ["workspace_orient", "a_get", "c_get"]

    def test_the_probe_waits_for_the_first_act_that_declares_a_capability(self, monkeypatch):
        # workspace_orient errors with no document open, so a probe taken before the opening act
        # reads unreadable and skips every gated row on an ENTITLED machine. The first act declares
        # nothing: its steps run first, and the probe lands between them and the gated act's.
        acts = [("ACT OPEN", None, [("doc_new", {}, "ok", None)], []),
                ("ACT E", None, [("a_get", {}, lambda p: p["n"] == 1, None)], [])]
        ledger, seen, _modes = self._run(
            monkeypatch, acts, {"ACT E": "machining_extension"}, entitled=True,
            tools=["a_get", "doc_new"])
        assert seen == ["doc_get", "design_get", "doc_new", "doc_get", "design_get",
                        "workspace_orient", "a_get"]
        assert ledger["a_get"] == "covered"

    def test_a_gated_act_polls_nothing(self, monkeypatch):
        # the boundary poll certifies a generation the act launched; a held-back act launched none,
        # and polling for one would fail the run on an act that deliberately did not happen.
        polled = []
        monkeypatch.setattr(tool_verify, "poll_generation",
                            lambda rows, notes, setup, valued=None: polled.append(setup))
        acts = [("ACT E", None, [("a_get", {}, "ok", None)], [])]
        self._run(monkeypatch, acts, {"ACT E": "machining_extension"}, entitled=False,
                  tools=["a_get", "workspace_orient"],
                  poll_after={"ACT E": {"narrative": "SwarfSetup", "fallback": "SwarfSetup"}})
        assert polled == []

    def test_a_poll_target_list_certifies_each_setup_in_turn(self, monkeypatch):
        # one act can leave SEVERAL setups generating; a list that only polled its first element
        # would leave the rest uncertified while the run still read green.
        polled = []
        monkeypatch.setattr(tool_verify, "poll_generation",
                            lambda rows, notes, setup, valued=None: polled.append(setup))
        acts = [("ACT G", None, [("a_get", {}, "ok", None)], [])]
        self._run(monkeypatch, acts, {}, entitled=True, tools=["a_get", "workspace_orient"],
                  poll_after={"ACT G": {"narrative": ["One", "Two"], "fallback": ["One", "Two"]}})
        assert polled == ["One", "Two"]

    def test_an_unreadable_probe_holds_the_steps_back_and_says_so(self, monkeypatch):
        def call(tool, args):
            return (False, {}) if tool == "workspace_orient" else (False, {"n": 1})
        monkeypatch.setattr(tool_verify, "call", call)
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        monkeypatch.setattr(tool_verify, "health_gate", _attested_health)
        monkeypatch.setattr(tool_verify, "registered_tools", _registered(["a_get", "workspace_orient"]))
        monkeypatch.setattr(tool_verify, "source_hash", lambda *a, **k: "0" * 64)
        ledger = {}
        monkeypatch.setattr(tool_verify, "write_verified",
                            lambda rows, v, d, h, **kw: ledger.update(rows))
        monkeypatch.setattr(tool_verify, "reload_smoke",
                            lambda rows, notes, valued=None, **kw: _reloaded_attestation())
        monkeypatch.setattr(tool_verify, "POLL_AFTER", {})
        monkeypatch.setattr(tool_verify, "EXCLUDED", {})
        monkeypatch.setattr(tool_verify, "STORY", {})
        monkeypatch.setattr(tool_verify, "ACT_NEEDS", {"ACT E": "machining_extension"})
        monkeypatch.setattr(tool_verify, "ACTS",
                            [("ACT E", None, [("a_get", {}, "ok", None)], [])])
        assert tool_verify.run(write_json=False) == 0
        assert ledger["a_get"] == ("skipped: machining_extension not entitled - the capability "
                                   "probe did not read")

    def test_every_declared_capability_in_the_real_program_has_a_probe(self):
        # a declaration with no probe routes as unmet forever, so the acts it gates would never run
        # again on any installation - and nothing else in the harness would say why.
        declared = set(tool_verify.ACT_NEEDS.values()) | {
            cap for step in tool_verify.STEPS
            for cap in [tool_verify.step_capability(step[2])] if cap}
        assert declared, "no capability is declared - the tier has no consumer"
        assert declared <= set(tool_verify.CAPABILITY_PROBES), (
            "capabilities declared with no probe registered: "
            + ", ".join(sorted(declared - set(tool_verify.CAPABILITY_PROBES))))

    def test_every_gated_act_name_is_a_real_act(self):
        names = {name for name, _p, _n, _f in tool_verify.ACTS}
        assert set(tool_verify.ACT_NEEDS) <= names, (
            "ACT_NEEDS names acts the program does not run: "
            + ", ".join(sorted(set(tool_verify.ACT_NEEDS) - names)))

    def test_every_extension_only_act_keeps_a_base_licence_variant(self):
        # A LICENCE capability may not take a TOOL's only step with it: every tool its acts and steps
        # drive must also be driven by an ungated step, or an unentitled installation loses that
        # tool's coverage rather than one strategy's.
        held, free = _licence_partition(tool_verify.ACTS, tool_verify.ACT_NEEDS,
                                        tool_verify.OPT_IN_TIERS)
        assert held, "no act declares a licence capability - this invariant has no subject"
        assert not (held - free), (
            "tools whose every step rides a LICENCE capability: " + ", ".join(sorted(held - free)))

    def test_an_opt_in_step_is_not_a_licence_gated_tools_base_variant(self):
        # The rule the real program cannot currently exercise: a tool whose only ungated-looking step
        # lives in an OPT-IN act has no base-licence variant at all - that step runs on no default
        # sweep. Counting an opt-in step as 'free' would report this program clean.
        acts = [("ACT LIC", None, [("x_get", {}, "ok", None)], []),
                ("ACT TIER", None, [("x_get", {}, "ok", None)], [])]
        needs = {"ACT LIC": "machining_extension", "ACT TIER": "fake_tier"}
        held, free = _licence_partition(acts, needs, frozenset({"fake_tier"}))
        assert held == {"x_get"} and free == set()
        assert held - free == {"x_get"}

    def test_a_step_level_opt_in_declaration_is_read_the_same_way(self):
        acts = [("ACT LIC", None, [("x_get", {}, tool_verify._needs("machining_extension"), None),
                                   ("x_get", {}, tool_verify._needs("fake_tier"), None)], [])]
        held, free = _licence_partition(acts, {}, frozenset({"fake_tier"}))
        assert held == {"x_get"} and free == set()

    def test_an_opt_in_tier_is_the_only_thing_holding_its_own_tools(self):
        # The other half of the pair: the cloud tier's tools ARE meant to have no ungated step - that
        # is what makes an unconfigured run skip them. A tool that drifted into an ungated act would
        # be driven against an operator's hub by a default sweep, which is exactly what the tier is
        # for, so the drift is caught here rather than live.
        tiered = {a for a, cap in tool_verify.ACT_NEEDS.items() if cap in tool_verify.OPT_IN_TIERS}
        assert tiered, "no act declares an opt-in tier - the tier has no consumer"
        held, free = set(), set()
        for name, _pre, narr, fb in tool_verify.ACTS:
            for step in (list(narr) + list(fb or [])):
                if step[0] == tool_verify._DWELL:
                    continue
                (held if name in tiered else free).add(step[0])
        # the tools the tier alone drives - the data/doc/drawing families that write to a real hub
        cloud_only = held - free
        assert {"data_upload_file", "data_delete_file", "doc_save_as", "drawing_export"} <= cloud_only
        assert not any(t.startswith("data_") for t in free), (
            "a data_* tool is driven by an act no opt-in tier gates: "
            + ", ".join(sorted(t for t in free if t.startswith("data_"))))


class TestFacadeLateBinding:
    """The call-time facade() sites with no other offline pin: each must see what a consumer
    stubs ON tool_verify at CALL time. An import-time binding of its own copy leaves the stub
    unread while every other offline test stays green, so these assertions are each site's one
    offline bite."""

    def test_precondition_holds_reads_the_stubbed_wire(self, monkeypatch):
        seen = []
        monkeypatch.setattr(tool_verify, "call",
                            lambda tool, args: (seen.append((tool, args)), (False, {}))[1])
        assert tool_verify._precondition_holds(("sketch_get", {"sketch_name": "X"})) is True
        assert seen == [("sketch_get", {"sketch_name": "X"})]
        monkeypatch.setattr(tool_verify, "call", lambda tool, args: (True, "down"))
        assert tool_verify._precondition_holds(("sketch_get", {})) is False

    def test_run_steps_pins_guarded_writes_and_rejects_authored_divergence(self, monkeypatch):
        seen = []
        monkeypatch.setattr(tool_verify, "call",
                            lambda tool, args: (seen.append((tool, dict(args))), (False, {}))[1])
        pin = {"state": "known", "handle": "session:pinned", "snapshot": {}}
        rows = tool_verify.run_steps(
            [("a_get", {}, "ok", None),
             ("b_get", {"expect_document": "session:authored"}, "ok", None),
             ("c_get", {"expect_document": None}, "ok", None),
             ("d_get", {"expect_document": " "}, "ok", None)],
            {}, sleep_s=0, document_pin=pin,
            guarded_tools={"a_get", "b_get", "c_get", "d_get"})
        assert [r[1] for r in rows] == ["pass", "blocked", "pass", "pass"]
        assert seen == [
            ("a_get", {"expect_document": "session:pinned"}),
            ("c_get", {"expect_document": "session:pinned"}),
            ("d_get", {"expect_document": "session:pinned"}),
        ]

    def test_deliberate_guard_refusal_preserves_the_retained_pin(self, monkeypatch):
        seen = []

        def call(tool, args):
            seen.append((tool, dict(args)))
            return (True, "document mismatch") if tool == "doc_activate" else (False, {})
        monkeypatch.setattr(tool_verify, "call", call)
        pin = {"state": "known", "handle": "session:pinned",
               "snapshot": {"state": "known", "document_handle": "session:pinned"},
               "known_documents": {}}
        rows = tool_verify.run_steps(
            [("doc_activate", {"name": "missing",
                               "expect_document": "session:wrong"}, "refused", None),
             ("a_get", {}, "ok", None)], {}, sleep_s=0, document_pin=pin,
            guarded_tools={"doc_activate", "a_get"})
        assert [row[1] for row in rows] == ["expected-refusal", "pass"]
        assert seen[-1] == ("a_get", {"expect_document": "session:pinned"})

    def test_unreadable_active_document_blocks_doc_new_before_dispatch(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            tool_verify, "call",
            lambda tool, args: (seen.append((tool, dict(args))), (True, "no active document"))[1])
        pin = {"state": "unknown", "handle": None, "snapshot": None,
               "known_documents": {}}
        rows = tool_verify.run_steps(
            [("doc_new", {}, "ok", None)], {}, sleep_s=0, document_pin=pin,
            guarded_tools={"doc_new"})
        assert rows[0][1] == "blocked"
        assert seen == [("doc_get", {})]

    def test_failed_initial_pin_blocks_later_doc_get_capture_and_write(self, monkeypatch):
        judged, captured, seen = [], [], []
        doc_reads = iter([
            (True, "active document unreadable"),
            (False, {"active": {"name": "A", "document_handle": "session:a"}}),
        ])

        def call(tool, args):
            seen.append((tool, dict(args)))
            if tool == "doc_get":
                return next(doc_reads)
            return False, {"changed": True}

        monkeypatch.setattr(tool_verify, "call", call)
        pin = {"state": "unknown", "handle": None, "snapshot": None,
               "known_documents": {}}
        rows = tool_verify.run_steps(
            [("doc_get", {}, lambda p: judged.append(p) or True,
              ("captured", lambda p: captured.append(p) or "session:a")),
             ("model_write", {}, "ok", None)],
            {}, sleep_s=0, document_pin=pin, guarded_tools={"model_write"})
        assert [row[1] for row in rows] == ["blocked", "blocked"]
        assert judged == [] and captured == []
        assert not any(tool == "model_write" for tool, _args in seen)

    def test_mismatch_block_survives_a_later_matching_doc_get(self, monkeypatch):
        judged, captured, seen = [], [], []
        doc_reads = iter([
            (False, {"active": {"name": "B", "document_handle": "session:b"}}),
            (False, {"active": {"name": "A", "document_handle": "session:a"}}),
        ])

        def call(tool, args):
            seen.append((tool, dict(args)))
            if tool == "doc_get":
                return next(doc_reads)
            return False, {"changed": True}

        monkeypatch.setattr(tool_verify, "call", call)
        pin = {"state": "known", "handle": "session:a",
               "snapshot": {"state": "known", "document_handle": "session:a"},
               "known_documents": {"session:a": "session:a"}}
        rows = tool_verify.run_steps(
            [("doc_get", {}, lambda p: judged.append(p) or True,
              ("first", lambda p: captured.append(p) or "session:b")),
             ("doc_get", {}, lambda p: judged.append(p) or True,
              ("second", lambda p: captured.append(p) or "session:a")),
             ("model_write", {}, "ok", None)],
            {}, sleep_s=0, document_pin=pin, guarded_tools={"model_write"})
        assert [row[1] for row in rows] == ["blocked", "blocked", "blocked"]
        assert judged == [] and captured == []
        assert not any(tool == "model_write" for tool, _args in seen)

    def test_pending_activation_is_settled_before_the_following_write(self, monkeypatch):
        seen = []
        active = ["session:pinned"]

        def call(tool, args):
            seen.append((tool, dict(args)))
            if tool == "doc_activate":
                active[0] = "session:target"
                return False, {"activated": "pending"}
            if tool == "doc_get":
                return False, {"active": {"name": "Target", "document_id": "urn:target",
                                           "document_handle": active[0]}}
            if tool == "design_get":
                return False, {"feature_count": 2}
            return False, {}
        monkeypatch.setattr(tool_verify, "call", call)
        pin = {"state": "known", "handle": "session:pinned",
               "snapshot": {"state": "known", "document_handle": "session:pinned"},
               "known_documents": {}}
        rows = tool_verify.run_steps(
            [("doc_activate", {"name": "session:target"}, "ok", None),
             ("a_get", {}, "ok", None)], {}, sleep_s=0, document_pin=pin,
            guarded_tools={"doc_activate", "a_get"})
        assert [r[1] for r in rows] == ["pass", "pass"]
        assert seen[0] == ("doc_activate", {
            "name": "session:target", "expect_document": "session:pinned"})
        assert seen[-1] == ("a_get", {"expect_document": "session:target"})

    def test_unsettled_activation_blocks_the_following_write(self, monkeypatch):
        seen = []

        def call(tool, args):
            seen.append((tool, dict(args)))
            if tool == "doc_activate":
                return False, {"activated": "pending"}
            if tool == "doc_get":
                return False, {"active": {"name": "Pinned", "document_id": "urn:pinned",
                                           "document_handle": "session:pinned"}}
            if tool == "design_get":
                return False, {"feature_count": 2}
            return False, {}
        monkeypatch.setattr(tool_verify, "call", call)
        monkeypatch.setattr(verify_runner.time, "sleep", lambda _seconds: None)
        pin = {"state": "known", "handle": "session:pinned",
               "snapshot": {"state": "known", "document_handle": "session:pinned"},
               "known_documents": {}}
        rows = tool_verify.run_steps(
            [("doc_activate", {"name": "session:target"}, "ok", None),
             ("a_get", {}, "ok", None)], {}, sleep_s=0, document_pin=pin,
            guarded_tools={"doc_activate", "a_get"})
        assert [r[1] for r in rows] == ["blocked", "blocked"]
        assert [tool for tool, _args in seen].count("doc_activate") == 1
        assert not any(tool == "a_get" for tool, _args in seen)

    def test_shoot_reads_the_stubbed_wire_and_names_the_file(self, monkeypatch, tmp_path):
        seen = []
        monkeypatch.setattr(tool_verify, "call",
                            lambda tool, args: (seen.append((tool, args)), (False, {}))[1])
        path = tool_verify._shoot("Frame:1", str(tmp_path), 3)
        assert seen and seen[0][0] == "view_screenshot"
        assert path == seen[0][1]["file_path"] and "0003_Frame_1" in path
        monkeypatch.setattr(tool_verify, "call", lambda tool, args: (True, "no viewport"))
        assert tool_verify._shoot("x", str(tmp_path), 4) is None

    def test_poll_generation_reads_the_stubbed_wire_and_story(self, monkeypatch):
        monkeypatch.setattr(tool_verify, "call",
                            lambda tool, args: (False, {"completed": True,
                                                        "live_states": {"valid": 4},
                                                        "empty_toolpaths": []}))
        monkeypatch.setattr(tool_verify, "STORY", {"cam_get_status": "stubbed story line"})
        rows, notes, valued = [], {}, set()
        tool_verify.poll_generation(rows, notes, "DemoSetup", valued=valued)
        assert rows == [("cam_get_status", "pass", "4 valid, non-empty toolpaths")]
        assert notes["cam_get_status"] == "stubbed story line"
        assert valued == {"cam_get_status"}

    def test_check_reads_the_stubbed_source_hash(self, monkeypatch, tmp_path):
        receipt = tmp_path / "VERIFIED_TOOLS.md"
        stamp = ("Stamp: source " + "ab" * 32 + " | Fusion 2705.1.4 | verified fixture\n"
                 + "Loaded: implementation " + "a" * 64 + " | schema " + "b" * 64
                 + " | load fixture-load | session fixture-session\n")
        receipt.write_text(stamp, encoding="utf-8")
        monkeypatch.setattr(tool_verify, "source_hash", lambda root=None: "ab" * 32)
        assert tool_verify.check(verified_path=str(receipt)) == 0
        monkeypatch.setattr(tool_verify, "source_hash", lambda root=None: "cd" * 32)
        assert tool_verify.check(verified_path=str(receipt)) == 1


_SCHEDULED = ("Reload scheduled. Make your next tool call after ~3 seconds - the connection "
              "reconnects automatically.")


def _reload_wire(reload_answer=(False, _SCHEDULED), found=("sys_reload_addin", "doc_get")):
    """The two wire calls the reload beat makes, stubbed: sys_reload_addin's own answer, and the
    registry search the smoke reads."""
    def call(tool, args):
        if tool == "sys_reload_addin":
            if isinstance(reload_answer, Exception):
                raise reload_answer
            return reload_answer
        return False, {"query": args.get("query"), "tool_count": len(found),
                       "tools": [{"tool": name} for name in found]}
    return call


def _health(*states):
    """A stubbed /health probe answering `states` in order and then holding the last one, so a
    poll that keeps asking sees a server that stays where the sequence left it."""
    seq = list(states)
    return lambda timeout=None: seq.pop(0) if len(seq) > 1 else seq[0]


def _urlopen(body=None, raises=None):
    """A stand-in for urllib.request.urlopen: a context manager whose read() answers `body` as a
    JSON document, or a raise standing in for a socket that answers nothing."""
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(body).encode("utf-8")

    def urlopen(url, timeout=None):
        if raises is not None:
            raise raises
        return _Response()
    return urlopen


class TestReloadBeat:
    """The post-run beat that drives sys_reload_addin. It claims a covered row ONLY for a restart
    it watched happen: the reload is deferred, so the server is still answering when the call
    returns, and every path below that cannot see /health go down and come back leaves the tool in
    its skipped bucket instead of banking a row."""

    @staticmethod
    def _drive(monkeypatch, call, answers, story="stubbed reload story"):
        monkeypatch.setattr(tool_verify, "call", call)
        monkeypatch.setattr(tool_verify, "_server_answers", answers)
        monkeypatch.setattr(tool_verify, "STORY", {"sys_reload_addin": story})
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        rows, notes, valued = [], {}, set()
        tool_verify.reload_smoke(rows, notes, valued=valued, down_polls=3, up_polls=3)
        return rows, notes, valued

    def test_a_watched_restart_banks_one_covered_row(self, monkeypatch):
        rows, notes, valued = self._drive(
            monkeypatch, _reload_wire(), _health(True, False, True))
        assert [(t, s) for t, s, _ in rows] == [("sys_reload_addin", "pass")]
        assert "answered again" in rows[0][2] and "sys_reload_addin among them" in rows[0][2]
        assert notes["sys_reload_addin"] == "stubbed reload story"
        assert valued == {"sys_reload_addin"}

    def test_a_server_that_never_goes_down_banks_nothing(self, monkeypatch):
        # The false positive the down-then-up watch exists to refuse: the reload is DEFERRED, so a
        # /health read taken when the call returns answers healthy whether or not the reload ever
        # fires. A beat that only waited for /health to answer would call this a restart.
        rows, notes, valued = self._drive(monkeypatch, _reload_wire(), _health(True))
        assert rows == [] and notes == {} and valued == set()

    def test_a_server_that_does_not_come_back_banks_nothing(self, monkeypatch):
        rows, _notes, valued = self._drive(monkeypatch, _reload_wire(), _health(True, False))
        assert rows == [] and valued == set()

    def test_a_reload_that_was_not_scheduled_banks_nothing(self, monkeypatch):
        # the tool's own refusal path (its deferred-reload event is not installed): nothing was
        # scheduled, so nothing restarts and the beat must not go on to watch for one.
        rows, _notes, valued = self._drive(
            monkeypatch, _reload_wire(reload_answer=(True, "Reload NOT scheduled: ...")),
            _health(True, False, True))
        assert rows == [] and valued == set()

    def test_an_ok_that_does_not_say_scheduled_banks_nothing(self, monkeypatch):
        rows, _notes, valued = self._drive(
            monkeypatch, _reload_wire(reload_answer=(False, "something else entirely")),
            _health(True, False, True))
        assert rows == [] and valued == set()

    def test_a_call_cut_off_by_the_teardown_banks_nothing(self, monkeypatch):
        # the response can be cut mid-flush; the beat reports that rather than raising through run
        rows, _notes, valued = self._drive(
            monkeypatch, _reload_wire(reload_answer=OSError("connection reset")),
            _health(True, False, True))
        assert rows == [] and valued == set()

    def test_a_registry_missing_the_tool_banks_nothing(self, monkeypatch):
        # /health answering is the SERVER being back; the registry answering with the tool is the
        # add-in being back. A restart that loaded no tools passes the first and fails here.
        rows, _notes, valued = self._drive(
            monkeypatch, _reload_wire(found=()), _health(True, False, True))
        assert rows == [] and valued == set()

    def test_the_smoke_read_failing_banks_nothing(self, monkeypatch):
        def call(tool, args):
            return (False, _SCHEDULED) if tool == "sys_reload_addin" else (True, "no such tool")
        rows, _notes, valued = self._drive(monkeypatch, call, _health(True, False, True))
        assert rows == [] and valued == set()

    def test_the_probe_answers_true_only_for_this_server(self, monkeypatch):
        # /health answering is not the add-in being back: another server holding the port answers
        # it too - the state health_gate hard-exits the run on - and reading that as the restart
        # would bank a covered row for a reload nobody observed.
        monkeypatch.setattr(urllib.request, "urlopen",
                            _urlopen({"server": tool_verify.SERVER_NAME, "version": "t"}))
        assert tool_verify._server_answers() is True
        monkeypatch.setattr(urllib.request, "urlopen",
                            _urlopen({"server": "Autodesk Fusion 360"}))
        assert tool_verify._server_answers() is False

    def test_a_probe_that_cannot_be_read_answers_not_up(self, monkeypatch):
        # connection-refused is the state the down-watch waits FOR. Reading a failed probe as 'up'
        # would leave that watch unable to fire, and the beat unable to record anything, silently.
        monkeypatch.setattr(urllib.request, "urlopen",
                            _urlopen(raises=OSError("connection refused")))
        assert tool_verify._server_answers() is False

    def test_poll_health_reads_the_stubbed_probe_and_gives_up_on_its_budget(self, monkeypatch):
        # the facade site: _poll_health must read the probe a consumer stubs ON tool_verify, and a
        # budget that runs out is False - never the state it was waiting for.
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        monkeypatch.setattr(tool_verify, "_server_answers", _health(True, True, False))
        assert tool_verify._poll_health(False, 3) is True
        monkeypatch.setattr(tool_verify, "_server_answers", _health(True))
        assert tool_verify._poll_health(False, 3) is False

    def test_run_fires_the_beat_after_every_act(self, monkeypatch):
        # the call SITE: the beat runs once the acts are done (it restarts the server, so nothing
        # can follow it) and its covered row reaches the ledger.
        ledger = {}

        def fake_write(rows, version, date, src_hash, path=None, notes=None, act_modes=None, attestation=None):
            ledger.update(rows)
            return "VERIFIED_TOOLS.md"

        seen = []
        wire = _reload_wire()

        def call(tool, args):
            seen.append(tool)
            if tool == "a_get":
                return False, {"n": 1}
            if tool == "doc_get":
                return False, {"active": {"name": "Untitled", "document_id": None,
                                           "document_handle": "session:test"}}
            if tool == "design_get":
                return False, {"feature_count": 1}
            return wire(tool, args)

        monkeypatch.setattr(tool_verify, "call", call)
        monkeypatch.setattr(tool_verify, "_server_answers", _health(True, False, True))
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        health_rows = iter([_attested_health(), _attested_health(),
                            _attested_health(_reloaded_attestation())])
        monkeypatch.setattr(tool_verify, "health_gate", lambda: next(health_rows))
        registry_health = []

        def registered(health=None, include_rows=False):
            registry_health.append(health)
            names = ["a_get", "sys_reload_addin"]
            rows = [{"name": name, "annotations": {"readOnlyHint": True},
                     "inputSchema": {"properties": {"expect_document": {}}}}
                    for name in names]
            return (names, tuple(rows)) if include_rows else names

        monkeypatch.setattr(tool_verify, "registered_tools", registered)
        monkeypatch.setattr(tool_verify, "source_hash", lambda *a, **k: "0" * 64)
        monkeypatch.setattr(tool_verify, "write_verified", fake_write)
        monkeypatch.setattr(tool_verify, "POLL_AFTER", {})
        monkeypatch.setattr(tool_verify, "EXCLUDED", {"sys_reload_addin": "not confirmed"})
        monkeypatch.setattr(tool_verify, "STORY", {})
        monkeypatch.setattr(tool_verify, "ACTS",
                            [("ACT T", None, [("a_get", {}, lambda p: p["n"] == 1, None)], None)])

        assert tool_verify.run(write_json=False) == 0
        assert ledger == {"a_get": "covered", "sys_reload_addin": "covered"}
        assert seen == ["a_get", "sys_reload_addin", "sys_find_tool"]
        assert [row["session_id"] for row in registry_health] == [
            "fixture-session", "fixture-session-2"]

    def test_an_unconfirmed_beat_blocks_the_receipt(self, monkeypatch):
        # A run cannot publish when its deliberate reload did not establish a new session.
        ledger = {}

        def fake_write(rows, version, date, src_hash, path=None, notes=None, act_modes=None, attestation=None):
            ledger.update(rows)
            return "VERIFIED_TOOLS.md"

        wire = _reload_wire()
        monkeypatch.setattr(tool_verify, "call",
                            lambda tool, args: ((False, {"n": 1}) if tool == "a_get"
                                                else wire(tool, args)))
        monkeypatch.setattr(tool_verify, "_server_answers", _health(True))   # never goes down
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        monkeypatch.setattr(tool_verify, "health_gate", _attested_health)
        monkeypatch.setattr(tool_verify, "registered_tools",
                            _registered(["a_get", "sys_reload_addin"]))
        monkeypatch.setattr(tool_verify, "source_hash", lambda *a, **k: "0" * 64)
        monkeypatch.setattr(tool_verify, "write_verified", fake_write)
        monkeypatch.setattr(tool_verify, "POLL_AFTER", {})
        monkeypatch.setattr(tool_verify, "EXCLUDED", {"sys_reload_addin": "not confirmed"})
        monkeypatch.setattr(tool_verify, "STORY", {})
        monkeypatch.setattr(tool_verify, "ACTS",
                            [("ACT T", None, [("a_get", {}, lambda p: p["n"] == 1, None)], None)])

        assert tool_verify.run(write_json=False) == 1
        assert ledger == {}


class TestActSelection:
    """--acts walks a SLICE of the story against the document a prior --keep-open run left open."""

    _ACTS = [("ACT 0 - OVERTURE", None, [], []),
             ("ACT 10a - CAM: JOB", None, [], []),
             ("ACT 10b - CAM: DELIVERABLES", None, [], []),
             ("ACT 10b2 - CAM: COMPONENT SCOPE", None, [], []),
             ("FINALE", None, [], [])]

    def test_one_selector_picks_exactly_its_act(self):
        assert tool_verify.select_acts(self._ACTS, "ACT 10a") == ["ACT 10a - CAM: JOB"]
        # a selector is a name prefix ending at a WORD BREAK, so 10b is not also 10b2 - a plain
        # startswith would select both and silently run an act nobody asked for.
        assert tool_verify.select_acts(self._ACTS, "ACT 10b") == ["ACT 10b - CAM: DELIVERABLES"]

    def test_a_comma_list_comes_back_in_acts_order(self):
        assert tool_verify.select_acts(self._ACTS, "FINALE, ACT 0") == ["ACT 0 - OVERTURE", "FINALE"]

    def test_a_range_spans_every_act_between_its_ends(self):
        assert tool_verify.select_acts(self._ACTS, "ACT 10a..ACT 10b2") == [
            "ACT 10a - CAM: JOB", "ACT 10b - CAM: DELIVERABLES", "ACT 10b2 - CAM: COMPONENT SCOPE"]

    def test_an_unknown_selector_and_an_empty_selection_are_refused_naming_the_acts(self):
        with pytest.raises(ValueError) as unknown:
            tool_verify.select_acts(self._ACTS, "ACT 99")
        assert "ACT 99" in str(unknown.value) and "ACT 10b2 - CAM: COMPONENT SCOPE" in str(unknown.value)
        with pytest.raises(ValueError) as empty:
            tool_verify.select_acts(self._ACTS, " ")
        assert "selects no act" in str(empty.value)

    # ACT 0 saves the ctx key ACT 10b's arguments recall, so a slice that skips it lands blocked.
    _WALK = [("ACT 0 - OVERTURE", None, [("a_get", {}, "ok", ("k", lambda p: p["n"]))], []),
             ("ACT 10a - CAM: JOB", None, [("b_get", {}, "ok", None)], []),
             ("ACT 10b - CAM: DELIVERABLES", None,
              [("c_get", lambda ctx: {"x": ctx["k"]}, "ok", None)], []),
             ("FINALE", None, [("doc_close", {}, "ok", None)], [])]

    @staticmethod
    def _run(monkeypatch, tools, **kw):
        """run() over the _WALK program on a stubbed wire -> (tools the wire saw, receipt written?,
        exit code)."""
        seen, wrote = [], []
        monkeypatch.setattr(tool_verify, "call",
                            lambda tool, args: (seen.append(tool), _known_wire(tool, args))[1])
        monkeypatch.setattr(tool_verify.time, "sleep", lambda s: None)
        monkeypatch.setattr(tool_verify, "health_gate", _attested_health)
        monkeypatch.setattr(tool_verify, "registered_tools", _registered(sorted(tools)))
        monkeypatch.setattr(tool_verify, "source_hash", lambda *a, **k: "0" * 64)
        monkeypatch.setattr(tool_verify, "write_verified",
                            lambda *a, **k: wrote.append(True) or "VERIFIED_TOOLS.md")
        monkeypatch.setattr(tool_verify, "reload_smoke",
                            lambda rows, notes, valued=None, **kw2: None)
        monkeypatch.setattr(tool_verify, "POLL_AFTER", {})
        monkeypatch.setattr(tool_verify, "EXCLUDED", {})
        monkeypatch.setattr(tool_verify, "STORY", {})
        monkeypatch.setattr(tool_verify, "ACT_NEEDS", {})
        monkeypatch.setattr(tool_verify, "ACTS", TestActSelection._WALK)
        return seen, wrote, tool_verify.run(write_json=False, **kw)

    def test_the_runner_walks_only_the_selected_acts(self, monkeypatch, capsys):
        seen, _wrote, code = self._run(monkeypatch, ["a_get", "b_get", "c_get", "doc_close"],
                                       acts_spec="ACT 10a")
        assert seen == ["b_get"] and code == 0
        # the acts that did not run are named once, up front
        out = capsys.readouterr().out
        assert "3 act(s) not run - ACT 0 - OVERTURE, ACT 10b - CAM: DELIVERABLES, FINALE" in out

    def test_a_recall_an_unrun_act_would_have_saved_lands_the_step_blocked(self, monkeypatch,
                                                                          capsys):
        seen, _wrote, code = self._run(monkeypatch, ["c_get"], acts_spec="ACT 10b")
        assert seen == [] and code == 1
        assert "blocked" in capsys.readouterr().out

    def test_a_partial_run_writes_no_receipt_and_says_so_in_its_last_line(self, monkeypatch,
                                                                         capsys):
        _seen, wrote, code = self._run(monkeypatch, ["b_get"], acts_spec="ACT 10a")
        assert not wrote and code == 0
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert lines[-1] == ("partial run (--acts ACT 10a): receipt not written; the story "
                             "document is left open")

    def test_the_document_is_left_open_unless_the_finale_is_selected_and_free_to_close_it(
            self, monkeypatch, capsys):
        _seen, _wrote, _code = self._run(monkeypatch, ["b_get", "doc_close"],
                                         acts_spec="ACT 10a,FINALE")
        assert capsys.readouterr().out.splitlines()[-1] == (
            "partial run (--acts ACT 10a,FINALE): receipt not written")
        # --keep-open drops the FINALE's doc_close in a partial run exactly as in a full one
        seen, _wrote, _code = self._run(monkeypatch, ["doc_close"], acts_spec="FINALE",
                                        keep_open=True)
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert seen == [] and lines[-1].endswith("; the story document is left open")



class _DocumentWire:
    """A session whose exact handles and active tab change like the document tools report."""

    def __init__(self, drift_after_write=False, drift_after_doc_get=None,
                 invalid_handle_tool=None):
        self.documents = {
            "session:home": {"name": "Home", "document_id": "urn:home"},
            "session:intruder": {"name": "Intruder", "document_id": "urn:intruder"},
        }
        self.active = "session:home"
        self.drift_after_write = drift_after_write
        self.drift_after_doc_get = drift_after_doc_get
        self.invalid_handle_tool = invalid_handle_tool
        self.seen = []
        self.created = 0
        self.doc_reads = 0

    def __call__(self, tool, args):
        self.seen.append((tool, dict(args)))
        if tool == "doc_get":
            self.doc_reads += 1
            active = dict(self.documents[self.active], document_handle=self.active)
            rows = [{"name": value["name"], "document_handle": handle,
                     "open_index": index, "is_active": handle == self.active}
                    for index, (handle, value) in enumerate(self.documents.items())]
            payload = {"active": active, "open_documents": rows,
                       "open_count": len(rows), "truncated": False}
            if self.drift_after_doc_get == self.doc_reads:
                self.active = "session:intruder"
            return False, payload
        if tool == "design_get":
            return False, {"feature_count": 3}
        if tool == "doc_new":
            self.created += 1
            handle = "session:new%d" % self.created
            self.documents[handle] = {"name": "Untitled", "document_id": None}
            self.active = handle
            payload = {"created": True, "document_handle": handle}
            if self.invalid_handle_tool == tool:
                payload.pop("document_handle")
            return False, payload
        if tool == "doc_open":
            self.created += 1
            handle = "session:open%d" % self.created
            self.documents[handle] = {"name": "Drawing", "document_id": args["file_id"]}
            self.active = handle
            payload = {"opened": True, "document_handle": handle}
            if self.invalid_handle_tool == tool:
                payload["document_handle"] = "open:2"
            return False, payload
        if tool == "doc_activate":
            target = args["name"]
            if target not in self.documents:
                return True, "not open"
            self.active = target
            return False, {"activated": True,
                           "document_name": self.documents[target]["name"]}
        if tool == "doc_close":
            target = args.get("name") or self.active
            if target not in self.documents:
                return True, "not open"
            name = self.documents.pop(target)["name"]
            if self.active == target:
                self.active = list(self.documents)[-1]
            return False, {"closed": [name], "closed_count": 1, "errors": []}
        if tool in {"doc_new", "doc_open", "doc_activate", "doc_close",
                    "model_write", "view_set", "view_screenshot"}:
            expected = args.get("expect_document")
            if expected and expected != self.active:
                return True, "document mismatch"
        if tool == "model_write":
            if self.drift_after_write:
                self.active = "session:intruder"
                self.drift_after_write = False
            return False, {"changed": True}
        return False, {"ok": True}


def _run_document_program(monkeypatch, tmp_path, wire, acts, writes, run_id=None,
                          shots_dir=None):
    wrote = {}
    names = sorted({step[0] for _name, _pre, narrative, fallback in acts
                    for step in list(narrative) + list(fallback or [])
                    if step[0] != tool_verify._DWELL})
    monkeypatch.setattr(tool_verify, "call", wire)
    monkeypatch.setattr(verify_runner, "time", _Clock(1.0))
    monkeypatch.setattr(verify_runner, "RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(tool_verify, "health_gate", _attested_health)
    monkeypatch.setattr(tool_verify, "registered_tools", _registered(names, writes))
    monkeypatch.setattr(tool_verify, "source_hash", lambda *a, **k: "0" * 64)
    monkeypatch.setattr(
        tool_verify, "write_verified",
        lambda rows, *a, **k: wrote.update(ledger=dict(rows)) or "VERIFIED_TOOLS.md")
    monkeypatch.setattr(tool_verify, "reload_smoke",
                        lambda rows, notes, valued=None, **kw: _reloaded_attestation())
    monkeypatch.setattr(tool_verify, "POLL_AFTER", {})
    monkeypatch.setattr(tool_verify, "EXCLUDED", {})
    monkeypatch.setattr(tool_verify, "STORY", {})
    monkeypatch.setattr(tool_verify, "ACT_NEEDS", {})
    monkeypatch.setattr(tool_verify, "ACTS", acts)
    tool_verify._RECALL.clear()
    code = tool_verify.run(write_json=False, run_id=run_id, shots_dir=shots_dir)
    return code, wrote


class TestWithinActDocumentPin:
    def test_home_address_requires_the_active_row_exact_handle(self):
        payload = {
            "active": {"name": "Home", "document_handle": "session:home"},
            "open_count": 1,
            "open_documents": [
                {"name": "Home", "open_index": 0, "is_active": True,
                 "document_handle": "session:home"},
            ],
        }
        assert verify_core._home_document(payload) is True
        assert verify_core._home_address(payload) == "session:home"
        payload["active"]["document_handle"] = "session:other"
        with pytest.raises(AssertionError, match="exact session handle"):
            verify_core._home_document(payload)
        with pytest.raises(ValueError, match="exact session handle"):
            verify_core._home_address(payload)

    def test_run_pins_before_the_first_context_read(self, monkeypatch, tmp_path):
        judged, captured = [], []

        def predicate(payload):
            judged.append(payload)
            return True

        def extract(payload):
            captured.append(payload)
            return payload["active"]["document_handle"]

        wire = _DocumentWire(drift_after_doc_get=1)
        acts = [("ACT INITIAL PIN", None, [
            ("doc_get", {}, predicate, ("captured", extract)),
            ("model_write", {}, lambda p: p["changed"] is True, None),
        ], [])]
        code, wrote = _run_document_program(
            monkeypatch, tmp_path, wire, acts, {"model_write"}, run_id="initial-drift")
        assert code == 1 and not wrote
        assert judged == [] and captured == []
        assert not any(tool == "model_write" for tool, _args in wire.seen)
        assert not Path(tool_verify.run_state_path("initial-drift")).exists()

    @pytest.mark.parametrize("transition", ["doc_new", "doc_open"])
    def test_invalid_transition_handle_blocks_later_write_and_checkpoint(
            self, monkeypatch, tmp_path, transition):
        wire = _DocumentWire(invalid_handle_tool=transition)
        args = {} if transition == "doc_new" else {
            "file_id": "urn:drawing", "force_api_open": True}
        key = "created" if transition == "doc_new" else "opened"
        acts = [("ACT INVALID TRANSITION", None, [
            (transition, args, lambda p: p[key] is True, None),
            ("model_write", {}, lambda p: p["changed"] is True, None),
        ], [])]
        run_id = "invalid-" + transition
        code, wrote = _run_document_program(
            monkeypatch, tmp_path, wire, acts, {transition, "model_write"}, run_id=run_id)
        assert code == 1 and not wrote
        assert [tool for tool, _args in wire.seen].count(transition) == 1
        assert not any(tool == "model_write" for tool, _args in wire.seen)
        assert not Path(tool_verify.run_state_path(run_id)).exists()

    def test_authored_positive_guard_cannot_bypass_the_run_pin(
            self, monkeypatch, tmp_path):
        wire = _DocumentWire()
        acts = [("ACT AUTHORED GUARD", None, [
            ("model_write", {"expect_document": "session:intruder"},
             lambda p: p["changed"] is True, None),
        ], [])]
        code, wrote = _run_document_program(
            monkeypatch, tmp_path, wire, acts, {"model_write"}, run_id="authored")
        assert code == 1 and not wrote
        assert not any(tool == "model_write" for tool, _args in wire.seen)

    def test_intruder_doc_get_blocks_save_write_and_checkpoint(self, monkeypatch, tmp_path):
        judged, captured = [], []
        wire = _DocumentWire(drift_after_write=True)
        acts = [("ACT PIN", None, [
            ("model_write", {}, lambda p: p["changed"] is True, None),
            ("doc_get", {}, lambda p: judged.append(p) or True,
             ("captured", lambda p: captured.append(p) or p["active"]["document_handle"])),
            ("doc_activate", {"name": "session:home"}, "ok", None),
            ("model_write", {}, lambda p: p["changed"] is True, None),
        ], [])]
        code, wrote = _run_document_program(
            monkeypatch, tmp_path, wire, acts, {"model_write", "doc_activate"},
            run_id="intruder")
        calls = [(tool, args) for tool, args in wire.seen if tool == "model_write"]
        assert code == 1 and not wrote and len(calls) == 1
        assert calls[0][1]["expect_document"] == "session:home"
        assert judged == [] and captured == []
        assert not any(tool == "doc_activate" for tool, _args in wire.seen)
        assert not Path(tool_verify.run_state_path("intruder")).exists()

    def test_active_closes_recover_only_through_exact_activation(self, monkeypatch, tmp_path):
        wire = _DocumentWire()
        wire.documents.pop("session:intruder")
        acts = [("ACT DOCUMENTS", None, [
            ("doc_get", {}, lambda p: p["active"]["name"] == "Home",
             ("home", lambda p: p["active"]["document_handle"])),
            ("doc_new", {}, lambda p: p["created"] is True, None),
            ("model_write", {}, lambda p: p["changed"] is True, None),
            ("doc_get", {}, lambda p: p["active"]["name"] == "Untitled",
             ("source", lambda p: p["active"]["document_handle"])),
            ("doc_open", {"file_id": "urn:drawing", "force_api_open": True},
             lambda p: p["opened"] is True, None),
            ("view_set", {"focus": ["drawing"]}, "ok", None),
            ("doc_get", {}, lambda p: p["active"]["name"] == "Drawing",
             ("drawing", lambda p: p["active"]["document_handle"])),
            ("doc_activate", lambda c: {"name": c["source"]}, "ok", None),
            ("doc_activate", {"name": "urn:drawing"}, "ok", None),
            ("doc_close", lambda c: {"name": c["drawing"]}, "ok", None),
            ("doc_close", lambda c: {"name": c["source"]}, "ok", None),
            ("doc_activate", lambda c: {"name": c["home"]}, "ok", None),
            ("model_write", {}, lambda p: p["changed"] is True, None),
        ], [])]
        writes = {"doc_new", "doc_open", "view_set", "doc_close", "doc_activate",
                  "model_write"}
        code, wrote = _run_document_program(
            monkeypatch, tmp_path, wire, acts, writes, run_id="recovery",
            shots_dir=str(tmp_path / "shots"))
        assert code == 0 and wrote
        guarded = [(tool, args["expect_document"]) for tool, args in wire.seen
                   if tool in writes]
        assert guarded == [
            ("doc_new", "session:home"),
            ("model_write", "session:new1"),
            ("doc_open", "session:new1"),
            ("view_set", "session:open2"),
            ("doc_activate", "session:open2"),
            ("doc_activate", "session:new1"),
            ("doc_close", "session:open2"),
            ("doc_close", "session:new1"),
            ("doc_activate", "session:home"),
            ("model_write", "session:home"),
        ]
        shots = [args for tool, args in wire.seen if tool == "view_screenshot"]
        assert shots and shots[0]["expect_document"] == "session:open2"
        saved = tool_verify.load_run_state("recovery")
        assert saved["complete"] is True
        assert saved["document"]["document_handle"] == "session:home"
        assert saved["document"]["feature_count"] == 3

class TestActualSchemaBinding:
    def test_identity_requires_a_load_id_and_session_id(self):
        missing_load = _attested_health()
        missing_load["attestation"].pop("load_id")
        empty_session = _attested_health()
        empty_session["session_id"] = ""
        assert verify_core.attestation_identity(missing_load) is None
        assert verify_core.attestation_identity(empty_session) is None

    def test_registry_rows_must_match_the_health_schema_and_session(self, monkeypatch):
        rows = [{"name": "a_get", "description": "read", "inputSchema": {"type": "object"}}]
        response = {"result": {"tools": rows}}
        identity = dict(_ATTESTATION, schema_fingerprint=verify_core._schema_fingerprint(rows))
        health = _attested_health(identity)
        monkeypatch.setattr(verify_core, "_post",
                            lambda payload, with_session=False: (response, identity["session_id"]))
        assert verify_core.registered_tools(health) == ["a_get"]

        wrong_schema = _attested_health(dict(identity, schema_fingerprint="c" * 64))
        with pytest.raises(SystemExit, match="schema fingerprint"):
            verify_core.registered_tools(wrong_schema)
        monkeypatch.setattr(verify_core, "_post",
                            lambda payload, with_session=False: (response, "another-session"))
        with pytest.raises(SystemExit, match="session changed"):
            verify_core.registered_tools(health)
