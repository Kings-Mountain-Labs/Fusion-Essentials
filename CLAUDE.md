# Working in Fusion-Essentials (MCP server)

This is the constitution for the MCP server under `commands/mcpServer/`: the kinds, the naming
schema, and the honesty contract every tool holds, no matter which file it lives in. Read
[CONTRIBUTING.md](CONTRIBUTING.md) for the full architecture and concepts. For the tool-authoring
recipe (adding a tool, the abstraction catalog, exemplars, the lints), read
[commands/mcpServer/tools/CLAUDE.md](commands/mcpServer/tools/CLAUDE.md) — it loads automatically
when you work in that directory. For writing a test, read [tests/CLAUDE.md](tests/CLAUDE.md). The
code is the source of truth — match the nearest existing tool when in doubt.

## Planning files

Keep `plans` sparse. `plans/backlog.md` is the single ledger for actionable work; update an existing row instead of creating another plan or status report.
Put agent scratch files, reports, evidence, exports and worker handoffs in the gitignored `outputs/` directory. Reserve `tests/live/evals/results/` for actual eval runs and `.cache` for tooling caches and isolated worktrees. Remove disposable scaffolding when a task finishes.

## Read vs Edit — the two kinds (and the read shapes)

There are **two real kinds**, split by the one thing that's machine-checkable: does the tool change
state? This is Command-Query Separation, and it *is* the `write=` flag.

| Kind | Does | `write=` | Examples |
|---|---|---|---|
| **Read** | Return information, change nothing. Safe to call blind. | read | `cam_get`, `find_geometry`, `model_measure_between`, `workspace_orient` |
| **Edit** | Act: mutate the model/data, or run an async operation. Gets the write guard; must verify its effect. | write / destructive | `model_extrude`, `joint_edit`, `doc_save`, `cam_edit_tools` |

Within **Read** there are three *shapes* — same kind, different job (they help you pick the tool's form,
not its permission):
- **Orient** (`orient`) — one cheap CROSS-domain read to situate ("where am I / what's broken / where
  next"). Call first, never floods. `workspace_orient`.
- **Disclose** (`get`) — progressively disclose ONE domain's structure: light default + `include=`/scope.
  The rich-read shape. `cam_get`, `design_get`, `doc_get`, `data_get`.
- **Acquire** (`find`/`measure`/`probe`/`inspect`/`compare`/`screenshot`/`section`/`compute`/`request`) —
  a read whose OUTPUT feeds an Edit: a handle, a measurement, an image, a user-pick, a diff. This is the
  seam the handle architecture runs on (`find_geometry` returns a handle, `joint_at_geometry` consumes it
  — see `_inputs.py`). Its one architectural rule: **an Acquire stays a SEPARATE tool from a
  Disclose read** — it has its own query params + returns handles, so don't fold it into a rich read.

## Naming schema

Every tool is `<domain>_<verb>[_<noun>]`, `<verb>` from a closed vocabulary, and **the verb's kind must
agree with `write=`** — a read-verb (`get`/`find`/`probe`/…) is read-only; an edit-verb mutates. So the
name *is* the type: `cam_get` reads, `model_compute_holder` acquires, `cam_edit_tools` writes; a name
that disagrees with `write=` is a mislabeled tool. Enforced by `test_tool_naming.py` (also the source of
the verb vocabulary). The exemptions (a poller like `cam_get_status`; a read-verb tool that still mutates,
e.g. `view_section`) and Edit packaging (`action=` dispatch, one-verb-per-file) live in
[commands/mcpServer/tools/CLAUDE.md](commands/mcpServer/tools/CLAUDE.md).

## Honesty contract (the rule that matters most)

- Use `ok(...)` / `error(...)` from `_common`. Wrap per-field READS in `safe(getter, default)` so one
  bad field doesn't sink the call — but let an actual MUTATION raise. A swallowed mutation that reports
  success is the cardinal sin: a failed delete/edit must return `isError`, never a false `ok`.
- After a write, verify the effect and report it. If the API returns success but nothing changed
  (it happens), treat that as failure. Surface partial success explicitly (what was done, what wasn't).
- A static read of `adsk.*` code cannot tell you what the API actually does. Before "fixing" a
  geometry/matrix/API bug you spotted by reading, reproduce it live (`sys_execute_script` against a
  scratch doc, or drive the tool and read the result back) — a plausible-looking bug is often correct
  code whose API contract you misread, and the "fix" is the regression. Confirm the defect exists, then
  confirm the fix, both against a live document.
- Guard inputs and report *why* a precondition failed, naming the offending value. Resolve references
  (occurrences, geometry) through the typed kinds in `_inputs.py` — they refuse ambiguity instead of
  grabbing the wrong instance; don't hand-roll a `name: str`.
- Resolving a name yourself: whether first-match is a bug depends on whether the name space is unique.
  A **scope-unique** name (a CAM setup/operation) resolves correctly by case-insensitive EXACT match,
  returning the available names on a miss (`_cam_common.find_operation`). A **non-unique** name (two
  sub-assemblies each holding a "Bolt:1") must **refuse** the ambiguity, never return the first
  substring/`.find()`/`[0]` hit — that silently targets the wrong entity. `test_no_first_match_resolvers`
  catches the always-wrong shapes (substring, indexed-first), but the unique-vs-not judgment is yours.

## Input kinds — use one BEFORE hand-rolling a `name`/`index` reference

To reference existing geometry/structure (face, edge, body, plane, axis, profile, occurrence, length,
fixed choice), use a typed kind from [`_inputs.py`](commands/mcpServer/tools/_inputs.py) — they refuse
ambiguity instead of grabbing the wrong instance; don't hand-roll a `name`/`index` resolver. Wire one
with `tool.add_input_property(*kind.as_property())`, resolve via `_inputs.resolve_inputs(...)`; if a
kind is close but missing a selector, extend the kind, not one tool's local copy. The full catalog —
every kind, plus the shared helpers to reuse — is the generated map in
[commands/mcpServer/tools/CLAUDE.md](commands/mcpServer/tools/CLAUDE.md), loaded when you author a tool.

<!-- BEGIN GENERATED FAMILIES (py -3 tests/gen_manifest.py) -->
**Tool families** (188 tools — `sys_find_tool <kw>` to search, `TOOL_MANIFEST.md` for the full list): `model`(33) `surface`(12) `mesh`(15) `sketch`(13) `cam`(24) `assembly`(9) `joint`(7) `design`(13) `doc`(14) `data`(10) `drawing`(9) `param`(5) `pmi`(4) `view`(6) `find`(1) `workspace`(1) `appearance`(1) `save`(1) `sys`(10)
<!-- END GENERATED FAMILIES -->

## Tool descriptions and agent-facing strings — pure ASCII, verified claims, budgeted

A tool's **description** + the `note`/`error` it returns are the ONLY thing a connected agent knows
about it, and they cross the wire JSON-serialized with `ensure_ascii` — so keep them pure ASCII (` - `
not `—`, `...` not `…`, `->` not `→`, `deg` not `°`), enforced by `test_wire_ascii.py`. Every claim about
an input's legal values must be backed by something that fails when it's false (a `Choice`/enum, a typed
kind's `resolve()`, a guard) — if you can't back it, type the input instead of asserting it.

The wire is paid for on every call, so it is budgeted (`test_prose_budget.py`): a description says what
the tool does and what to call next; a note says what was observed and the next step; an error names the
offending value and the remedy. None of them says that something was measured, why the platform behaves
that way, or how the code is built - that teaching goes in the error an agent meets at the moment it
matters, once. A rich read's deep `include=` returns the slice asked for, not the default slice again.

## What "done" means, and how much verification a change buys

A change is done when its live sweep row passes on the real Fusion session, and its receipt is restamped.
Mock unit tests prove handler logic and nothing about Fusion: they encode the builder's beliefs, and a
wrong belief pinned by a test is the most confident way to ship a lie. So: one test per plausible bug,
no test per function, no mutant theater in review (a reviewer runs a handful of mutants, not dozens), and
the reviewer drives the live session to check API claims whenever it can. The three defects found on
2026-09-02 (a false `ok` pinned by a test, a classifier whose docstring asserted the opposite of the
live shape, an unconsumable remedy) all came from thirty minutes of using the tools, none from 11,000
unit tests. Every wave ends with a cold eval run (`tests/live/evals/proctor.py`) with NO skill
appended, so the wire alone is what gets graded.

## Enforcement is a closed list

The files in `tests/lints/` ARE the closed list (36 on 2026-09-02): the wire contract (naming + verb/`write=`
agreement, write-status, strict schema, input names, output contracts, ASCII, wire budget and shape, prose
budget), the honesty contract (no fabricated fallbacks, bool returns checked, no first-match resolvers,
postconditions declared, units, native identity keys, export knob pre-read, frame disclosure, material
effect, rename adoption, no hand-cast product), and the structure (helper duplication denylist, no
duplicate defs, dead code, autodiscovery, unit-coverage-complete, tool-verify-complete, generated docs
current, doc citations, measured enums, fake shapes, evergreen). Adding a lint file needs the owner's
word; a new duplication class gets a denylist ENTRY, not a file. No ratchets, no per-file baselines, no
self-tests for lints, no lint that polices wording. The suite regrew twice after purges because every
review finding became a lint - a finding becomes a ledger row, and a fix, and a live act.

## Prose in code

A docstring is one line saying what the function returns or does; a comment is at most three lines
stating a present-tense fact the code cannot show (a platform trap at its point of use, with no history).
A helper's catalog blurb is one line: its symbols and when to reach for it. Everything longer is cut, and
`test_prose_budget.py` says where.

## Running commands — never `cd` to the repo root, never redirect stderr

Both shell tools already start in the repo root and already capture stderr, so `cd c:\Source\Fusion-Essentials`
and `2>&1` are redundant. Together they are worse than redundant: a `cd` combined with an output redirect
makes the permission layer ask the owner to approve the call by hand, **whatever the allow rules say** -
it cannot tell where the redirect target resolves after the `cd`. That one habit is the single largest
source of approval prompts in this repo, and every prompt is a human being interrupted.

So: `py -3 -m pytest tests/unit/test_sketch_add_geometry.py -q`, not
`cd c:\Source\Fusion-Essentials; py -3 -m pytest tests/unit/test_sketch_add_geometry.py -q 2>&1 | Select-String "passed|failed"`.
Pipe to a filter if you want one, just leave the `cd` and the `2>&1` out. Filter with `-q`/`--no-cov` and
the tail of the output rather than a regex, and prefer the file tools (Read/Grep/Edit/Write) over shell
equivalents for anything touching files.
