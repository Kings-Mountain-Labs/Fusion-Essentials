# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The eval proctor's decisions that do not need Fusion: what the executor receives, when a stage
refuses, which chain document is opened, when a run counts as stalled, and what the record says.
The Fusion wire is a stub returning (is_error, payload); nothing here launches an executor."""

import json
import os
import sys
import types

import pytest

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TESTS_DIR, "live", "evals"))
import proctor  # noqa: E402

_SCENARIO = """---
id: S99_Widget
fixture: none
---

## Prompt

Build a widget and save it into project {{PROJECT}}, folder {{FOLDER}}.

## Grader notes

A good widget has a hole. SECRET-GRADER-TEXT.
"""


def _wire(answers):
    """A stub call(tool, args) answering from a tool -> (is_error, payload) map and logging calls."""
    log = []

    def call(tool, args):
        log.append((tool, args))
        answer = answers[tool]
        return answer(args) if callable(answer) else answer
    call.log = log
    return call


class TestPrompt:
    @pytest.fixture
    def scenario(self, tmp_path):
        path = tmp_path / "S99_Widget.md"
        path.write_text(_SCENARIO, encoding="utf-8")
        preamble = tmp_path / "preamble.md"
        preamble.write_text("PREAMBLE RULES\n", encoding="utf-8")
        return path, preamble

    def test_prompt_is_preamble_then_brief_with_tokens_filled(self, scenario):
        path, preamble = scenario
        sid, fixture, body = proctor.read_scenario(str(path))
        prompt = proctor.build_prompt(body, "Proj", "Root/Eval-1", preamble_path=str(preamble))
        assert (sid, fixture) == ("S99_Widget", None)
        assert prompt.startswith("PREAMBLE RULES\n\nBuild a widget")
        assert "project Proj, folder Root/Eval-1." in prompt

    def test_grader_notes_never_reach_the_prompt(self, scenario):
        path, preamble = scenario
        _, _, body = proctor.read_scenario(str(path))
        prompt = proctor.build_prompt(body, "P", "F", preamble_path=str(preamble))
        assert "SECRET-GRADER-TEXT" not in prompt and "## Grader" not in prompt

    def test_skill_body_lands_after_the_brief(self, scenario, tmp_path, monkeypatch):
        path, preamble = scenario
        skills = tmp_path / "skills"
        (skills / "practice").mkdir(parents=True)
        (skills / "practice" / "SKILL.md").write_text("---\nname: practice\n---\n\nPLAN FIRST\n",
                                                      encoding="utf-8")
        monkeypatch.setattr(proctor, "SKILLS_DIR", str(skills))
        _, _, body = proctor.read_scenario(str(path))
        prompt = proctor.build_prompt(body, "P", "F", skill="practice", preamble_path=str(preamble))
        assert prompt.index("Build a widget") < prompt.index(proctor.SKILL_HEADER) < prompt.index("PLAN FIRST")
        assert "name: practice" not in prompt

    def test_a_chain_stage_receives_the_newest_prior_report_after_its_brief(self, scenario, tmp_path):
        path, preamble = scenario
        set_dir = tmp_path / "set"
        for nn, text in (("01", "old plan"), ("02", "newest plan")):
            (set_dir / f"S98_Base_{nn}").mkdir(parents=True)
            (set_dir / f"S98_Base_{nn}" / "report.txt").write_text(text, encoding="utf-8")
        (set_dir / "S98_Base_03").mkdir()
        assert proctor.prior_report(str(set_dir), "S98_Base") == "newest plan"
        assert proctor.prior_report(str(set_dir), None) == ""
        _, _, body = proctor.read_scenario(str(path))
        prompt = proctor.build_prompt(body, "P", "F", preamble_path=str(preamble),
                                      prior_report="newest plan")
        assert prompt.index("Build a widget") < prompt.index(proctor.REPORT_HEADER) < prompt.index("newest plan")

    def test_a_variant_file_keeps_the_scenario_id(self, tmp_path):
        path = tmp_path / "S99_Widget.B.md"
        path.write_text(_SCENARIO.replace("fixture: none", "fixture: S98_Base"), encoding="utf-8")
        sid, fixture, _ = proctor.read_scenario(str(path))
        assert (sid, fixture) == ("S99_Widget", "S98_Base")


class TestShippedScenarios:
    def test_every_scenario_parses_and_every_fixture_names_a_scenario(self):
        files = sorted(f for f in os.listdir(proctor.SCENARIOS) if f.endswith(".md"))
        ids = {}
        for name in files:
            sid, fixture, prompt = proctor.read_scenario(os.path.join(proctor.SCENARIOS, name))
            assert name.startswith(sid), f"{name}: id {sid!r} does not match its file"
            assert len(prompt) > 200, f"{name}: the prompt is too short to be a brief"
            assert "## Grader" not in prompt and "VERDICT:" not in prompt, name
            ids[name] = (sid, fixture)
        known = {sid for sid, _ in ids.values()}
        for name, (_, fixture) in ids.items():
            assert fixture is None or fixture in known, f"{name}: fixture {fixture!r} is no scenario"
        assert len(files) >= 16 and "S13_Surfaced-Bottle.B.md" in files


class TestProject:
    def test_missing_project_is_created_and_announced(self, capsys):
        call = _wire({"data_get": (False, {"projects": [{"name": "Other", "id": "p1"}]}),
                      "data_create_project": (False, {"created": True, "name": "Evals", "id": "p2"})})
        assert proctor.ensure_project("Evals", "Hub", call) == "Evals"
        assert call.log[-1] == ("data_create_project", {"name": "Evals"})
        assert "created project 'Evals' on hub 'Hub'" in capsys.readouterr().out

    def test_present_project_is_matched_case_insensitively_and_not_recreated(self):
        call = _wire({"data_get": (False, {"projects": [{"name": "MCP Evals", "id": "p1"}]})})
        assert proctor.ensure_project("mcp evals", "Hub", call) == "MCP Evals"
        assert [t for t, _ in call.log] == ["data_get"]

    def test_existing_set_folder_is_not_an_error(self):
        call = _wire({"data_create_folder": (True, "A folder named 'Eval-1' already exists at 'Root'")})
        assert proctor.ensure_folder("P", "Root", "Eval-1", call) == "Root/Eval-1"

    def test_other_folder_failures_stop_the_run(self):
        call = _wire({"data_create_folder": (True, "Project not found: P")})
        with pytest.raises(SystemExit):
            proctor.ensure_folder("P", "Root", "Eval-1", call)


class TestStaging:
    def _orient(self, bodies, occurrences):
        return (False, {"document": {"name": "Untitled"},
                        "design": {"bodies": bodies, "total_occurrences": occurrences}})

    def test_empty_stage_passes_on_zero_bodies_and_occurrences(self):
        call = _wire({"doc_new": (False, {"created": True}), "workspace_orient": self._orient(0, 0)})
        assert proctor.stage_empty(call) == "Untitled"

    def test_empty_stage_refuses_a_document_with_bodies(self):
        call = _wire({"doc_new": (False, {"created": True}), "workspace_orient": self._orient(82, 45)})
        with pytest.raises(SystemExit, match="bodies=82"):
            proctor.stage_empty(call)

    def test_empty_stage_refuses_an_unreadable_census(self):
        call = _wire({"doc_new": (False, {"created": True}), "workspace_orient": self._orient(None, 0)})
        with pytest.raises(SystemExit):
            proctor.stage_empty(call)

    def test_fixture_opens_the_newest_matching_document(self):
        files = [{"name": "S1_Foundation_01", "id": "urn:1"}, {"name": "S1_Foundation_03", "id": "urn:3"},
                 {"name": "S1_Foundation_02", "id": "urn:2"}, {"name": "S1_Foundation", "id": "urn:0"},
                 {"name": "S12_Dumbbell_01", "id": "urn:12"}]
        call = _wire({"data_get": (False, {"files": files}),
                      "doc_open": (False, {"opened": True}),
                      "workspace_orient": (False, {"document": {"name": "S1_Foundation_03 v1"}})})
        assert proctor.stage_fixture("S1_Foundation", "P", "F", call, sleep=lambda s: None) == "S1_Foundation_03"
        assert ("doc_open", {"file_id": "urn:3", "force_api_open": True}) in call.log

    def test_fixture_refuses_when_no_stage_output_is_in_the_set(self):
        call = _wire({"data_get": (False, {"files": [{"name": "S12_Dumbbell_01", "id": "urn:12"}]})})
        with pytest.raises(SystemExit, match="run S1_Foundation into this set first"):
            proctor.stage_fixture("S1_Foundation", "P", "F", call)

    def test_fixture_refuses_when_the_open_never_becomes_active(self, monkeypatch):
        clock = iter([0.0, 0.0, 200.0, 200.0])
        monkeypatch.setattr(proctor.time, "time", lambda: next(clock))
        call = _wire({"data_get": (False, {"files": [{"name": "S1_Foundation_01", "id": "urn:1"}]}),
                      "doc_open": (False, {"opened": True}),
                      "workspace_orient": (False, {"document": {"name": "Bench"}})})
        with pytest.raises(SystemExit, match="did not become the active document"):
            proctor.stage_fixture("S1_Foundation", "P", "F", call, wait_s=120, sleep=lambda s: None)


class TestWatch:
    _CALL = '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"x"}]}}\n'
    _THINK = '{"type":"system","subtype":"thinking_tokens","estimated_tokens":9000}\n'

    def test_thinking_does_not_reset_the_idle_clock_but_a_call_does(self):
        live = proctor.Liveness(0.0)
        live.read(self._CALL, 10.0)
        live.read(self._THINK, 500.0)
        assert live.idle_s(700.0) == 690.0
        live.read(self._CALL, 700.0)
        assert live.calls == 2 and live.idle_s(705.0) == 5.0
        assert live.call_times == [10, 700]

    def test_harness_label_names_only_the_switches_off_their_defaults(self):
        assert proctor.harness_label({"skill": None, "tool_search": False, "preamble": "preamble"}) == "-"
        assert proctor.harness_label({"skill": "practice", "tool_search": True,
                                      "preamble": "preamble-lean", "label": "bare"}) == \
            "skill:practice tool-search:on preamble:preamble-lean label:bare"

    def test_stall_limit_reads_the_env_and_falls_back(self, monkeypatch):
        monkeypatch.setenv("EVAL_STALL_S", "45")
        assert proctor.stall_limit_s() == 45
        monkeypatch.setenv("EVAL_STALL_S", "soon")
        assert proctor.stall_limit_s() == proctor.STALL_S_DEFAULT

    def _creds(self, tmp_path, expires_s):
        path = tmp_path / ".credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-1",
                                                      "expiresAt": int(expires_s * 1000)}}),
                        encoding="utf-8")
        return str(path)

    def test_oauth_token_is_read_when_it_has_time_left(self, tmp_path):
        assert proctor.oauth_token(self._creds(tmp_path, 10_000), now=0.0) == "tok-1"

    def test_oauth_token_refuses_when_close_to_expiry_or_absent(self, tmp_path):
        with pytest.raises(SystemExit, match="expires in"):
            proctor.oauth_token(self._creds(tmp_path, 600), now=0.0)
        with pytest.raises(SystemExit, match="claude login"):
            proctor.oauth_token(str(tmp_path / "missing.json"), now=0.0)

    def test_a_key_or_token_in_the_environment_passes_through_without_the_file(self, tmp_path):
        missing = str(tmp_path / "missing.json")
        assert proctor.executor_login({"ANTHROPIC_API_KEY": "sk-x"}, creds=missing) == {}
        assert proctor.executor_login({"CLAUDE_CODE_OAUTH_TOKEN": "t"}, creds=missing) == {}
        with pytest.raises(SystemExit, match="setup-token"):
            proctor.executor_login({}, creds=missing)

    def test_kill_tree_uses_taskkill_on_windows_and_kill_elsewhere(self, monkeypatch):
        ran = []
        monkeypatch.setattr(proctor.sys, "platform", "win32")
        monkeypatch.setattr(proctor, "subprocess",
                            types.SimpleNamespace(run=lambda cmd, **kw: ran.append(cmd)))
        proc = types.SimpleNamespace(pid=4242, kill=lambda: ran.append("kill"))
        proctor.kill_tree(proc)
        assert ran == [["taskkill", "/PID", "4242", "/T", "/F"]]
        monkeypatch.setattr(proctor.sys, "platform", "linux")
        proctor.kill_tree(proc)
        assert ran[-1] == "kill"


class TestRecord:
    def _transcript(self, tmp_path, lines):
        path = tmp_path / "t.jsonl"
        path.write_text("".join(json.dumps(ev) + "\n" for ev in lines), encoding="utf-8")
        return str(path)

    def test_audit_counts_fusion_calls_only_and_reads_the_result(self, tmp_path):
        path = self._transcript(tmp_path, [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "mcp__fusion-essentials__doc_get"},
                {"type": "tool_use", "name": "TodoWrite"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "mcp__fusion-essentials__model_extrude"}]}},
            {"type": "result", "result": "Built it.", "usage": {"output_tokens": 1234}}])
        assert proctor.audit(path) == (2, 1234, "Built it.")

    def test_save_reports_the_refusal_and_still_takes_the_screenshot(self, tmp_path):
        call = _wire({"doc_save_as": (True, "Fusion declined to save"),
                      "view_screenshot": (False, {"ok": True})})
        saved = proctor.save_result("S99_01", "P", "F", str(tmp_path), call)
        assert saved["error"] == "Fusion declined to save" and saved["screenshot"] == "iso.png"
        assert call.log[-1][0] == "view_screenshot"
        assert call.log[-1][1]["file_path"] == os.path.join(str(tmp_path), "iso.png")

    def test_save_waits_for_the_url_to_settle(self, tmp_path):
        reads = iter([(False, {"active": {"fusion_web_url": None}}),
                      (False, {"active": {"fusion_web_url": "https://x/9", "document_id": "urn:9"}})])
        call = _wire({"doc_save_as": (False, {"saved": True, "document_id": None}),
                      "doc_get": lambda args: next(reads),
                      "view_screenshot": (False, {})})
        naps = []
        saved = proctor.save_result("S99_01", "P", "F", str(tmp_path), call, sleep=naps.append)
        assert (saved["document_id"], saved["web_url"], saved["error"]) == ("urn:9", "https://x/9", None)
        assert naps == [5]

    def test_index_row_links_the_document_or_names_the_failure(self):
        rec = {"started": "10:00", "set": "Eval-S99", "variant": "S99_Widget.B",
               "model": "opus", "skill": None, "denied": [], "calls": 40, "output_tokens": 5000,
               "ended": "report", "saved": {"web_url": None, "error": "declined"}}
        row = proctor.index_row(rec)
        assert row.startswith("| 10:00 | Eval-S99 | S99_Widget.B | opus | - | - | 40 | 5000 | report | declined |")
        rec["saved"] = {"web_url": "https://x/1", "error": None}
        assert proctor.index_row(rec).rstrip().endswith("| https://x/1 |")

    def test_run_dirs_number_from_the_first_free_slot(self, tmp_path):
        os.makedirs(tmp_path / "S99_Widget_01")
        os.makedirs(tmp_path / "S99_Widget_02")
        assert proctor.next_run_dir(str(tmp_path), "S99_Widget") == (
            os.path.join(str(tmp_path), "S99_Widget_03"), "03")
