# Evals: hand a design brief to a blind agent and look at what it built

An eval is a scenario file under `scenarios/`: a brief a capable designer would be given, sent
verbatim to an agent that holds only the Fusion MCP tools. The proctor (`proctor.py`) stages the
document, runs the agent, saves the result into your project, takes a screenshot, and records the
run. It grades nothing. You open the saved document beside the agent's report and judge it.

## Running one

```
py -3 tests/live/evals/proctor.py S13_Surfaced-Bottle
py -3 tests/live/evals/proctor.py S13_Surfaced-Bottle --runs 3 --model opus
```

Needs Fusion up with the add-in loaded (its MCP server on 127.0.0.1:27182), the `claude` CLI on
PATH with a login the executor can use (a `claude login` on Windows or Linux, or
`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, or `ANTHROPIC_API_KEY`, on any platform), and
the untracked `tests/live/cloud_config.local.json` naming your hub, project and folder (the proctor
prints the file's shape when it is missing). `py -3` is the Windows launcher; use `python3`
elsewhere. The proctor makes that hub
active, finds the project (creating it and saying so when it is missing), and makes one cloud folder
per eval set, `Eval-<date>-<set>` under your folder, where every document a run saves or starts
from lives. `--set NAME` lets a chain share one folder: S2a opens the newest `S1_Foundation_<nn>` in
its set, S2b the newest S2a output, and so on (each scenario's `fixture:` names the stage it starts
from). A scenario with `fixture: none` starts from a new document the proctor reads back empty.

Every run lands in `results/<set>/<scenario>_<nn>/`: `prompt.txt` (the exact bytes sent),
`transcript.jsonl`, `report.txt` (the agent's final message), `iso.png`, and `run.json` (calls,
output tokens, how the run ended, the saved document's URN and web link). One line per run is
appended to `results/index.md`. The saved document is named `<scenario>_<nn>` in the set folder.

## A/B

Three axes, one switch each, all recorded in `run.json` and the index:

- **The prompt.** A variant is another scenario file: `S13_Surfaced-Bottle.B.md` beside the
  original, same `id:` in its frontmatter, a different `## Prompt`. Run both, compare the documents.
- **The tooling.** `--deny mcp__fusion-essentials__<tool>` withholds one tool for a run (the control
  arm of "does this tool matter").
- **The harness.** `--model`; `--skill <name>` (appends a design-practice skill's body after the
  brief); `--tool-search` (the CLI defers tool schemas, a few per turn instead of all of them; a
  spawn that comes up with no Fusion tools ends the run at once); `--preamble <file>` (another
  harness-rules file, such as `preamble-lean.md`, which drops the cold-start reads). The CLI's
  thinking cap is not a switch: it does not bound a turn. `run.json` records the seconds at which
  every tool call landed, so a long first pause is visible.

## Scenario format

```
---
id: S13_Surfaced-Bottle
fixture: none
---

## Prompt

<the brief, sent verbatim; {{PROJECT}} and {{FOLDER}} are filled in>

## Grader notes

<never sent: what a good result looks like, which axis the scenario discriminates, the first A/B
to run on it>
```

`preamble.md` carries the harness rules once (only agent, only Fusion tools, cold start, end with a
report) and is prepended to every prompt identically, so the scenario files hold design briefs only.

## Watching a run

The proctor prints a heartbeat each minute with the tool-call count. No tool call for `EVAL_STALL_S`
seconds (default 600; a foundation-sized brief pauses over six minutes to plan before it builds)
kills the executor's process tree and records the run as stalled. A run is never retried by the
proctor; run it again.

Every run bills the account the CLI is signed into, on the model you pass, and each executor turn
carries the whole tool schema (about 135k cached input tokens). A long run is a real spend: run one
at a time, prefer `--model sonnet` for prompt A/B, and keep opus for capability questions.

## Privacy

A transcript captures workspace_orient output, which carries the hub name and project URNs; the
results tree is gitignored, never commit or paste it. The executor's login is your CLI's current
access token, handed over by environment for the run (never copied to disk); the proctor refuses to
start on a token with under thirty minutes left.
