# Contributing to Fusion-Essentials

This guide is for **developers** extending the add-in (mainly its MCP server). It opens with the
*concepts* — what the construct is and the ideas that make it work — then the *operational* how-to
(layout, hard rules, the dev loop, adding a tool). For **users**, see the [README](README.md) and the
[MCP Server README](commands/mcpServer/README.md). The code is the authoritative description of
behavior; this doc explains the *why* and points at where each idea lives, rather than transcribing it.

---

## Understand the construct (the concepts)

**What MCP is here.** The add-in hosts a local [Model Context Protocol](https://modelcontextprotocol.io)
server inside Fusion. An AI agent connects over loopback and receives a list of *tools* — each a
`{name, description, inputSchema}`. The agent never sees the implementation; **the schema and
description ARE the API contract.** It picks a tool, calls it, and the server runs the tool's handler on
Fusion's main thread and returns a JSON result. That's the whole loop: discovery is the schema,
execution is a marshalled handler. (Launch/registration lives in `commands/mcpServer/entry.py`; the
primitives in `commands/mcpServer/mcp_primitives/`.)

A handful of ideas give the tool set its character. Each is a convention you should follow when adding
a tool — they are what make an agent able to drive Fusion *deterministically*, with few calls and few
blind spots.

- **Tools are building blocks; skills compose them.** A tool is one verb (`model_extrude`,
  `assembly_get`). A *skill* (`.claude/skills/`) is a markdown procedure that chains tools into a
  repeatable workflow — the sentence built from the verbs. Reliability comes from each step being a
  tested tool with its own guards, not from a brittle macro. If a workflow needs a capability no tool
  provides, that is the signal to **add a tool**, not to hand-roll `sys_execute_script` inside a skill.

- **Progressive disclosure — orient broadly, then drill cheaply.** An agent arrives at a document blind,
  and reading an entire large design does not scale. So the posture is: one cheap broad read first
  (`workspace_orient` — what's open, its health, whether CAM exists, the major pieces, and *pointers* to
  the right narrow tool), then scoped refinement on demand (`design_get` scoped to a component,
  `find_geometry(target=…)`, `assembly_get`). A new read tool should fit this shape: cheap and broad,
  or scoped and deep — and say which.

- **Geometry-as-values.** An agent has no eyes, so selecting geometry by a magic snap-string is
  ambiguous the moment a part has two cylinders. Instead, `find_geometry` returns each face/edge/vertex
  as a stable `entityToken` *handle* (filterable by radius/proximity — numbers, not pixels), and
  consumers take that handle via the `GeometryHandle` input kind. Geometry becomes a first-class **value
  that flows between calls** rather than tribal knowledge re-derived each time. `assembly_get` is the
  same idea for kinematic state (positions/grounding/joints as JSON, not a cluttered render).

- **Return the IDs the next call needs — unprompted.** A tool that creates or identifies something
  should put its stable id in the result even when not asked: `find_geometry` returns a handle,
  `doc_get` a data-model URN, `assembly_get` exact occurrence names, `model_inspect`
  measured extents. This is what makes a chain deterministic — the next target is an id the previous step
  *minted*, not a name the agent hopes resolves.

- **The code guides correct use; the description carries the contract.** Because the agent only sees the
  schema, dependencies and failure modes must be legible up front. Two mechanisms in
  `_inputs.py` do this in code rather than prose: a typed **input kind**
  (`GeometryHandle`/`BodyRef`/`PlaneRef`/`AxisRef`/`Choice`/…) bundles schema + resolution + validation +
  an auto-generated contract line, so an input that needs a face can *only* take a handle, never a bare
  coordinate; and a **`ModeGuard`** derives its error from the requirement, so a precondition rejection
  can't point the wrong way. Prefer these over hand-rolling a `name: str` — that is how blind spots get
  reintroduced. The **producer-side mirror** is `_outputs.py`: a tool declares `RETURNS = [...]`
  of typed **output kinds** (`ReturnsHandle`/`ReturnsUrn`/`ReturnsName`/`ReturnsValue`), which generate the
  description's `PRODUCES:` line AND back a test that asserts the handler actually mints the declared key —
  so a renamed id field fails the suite instead of silently lying to every consumer that reads it. A mutation
  should never hide behind `safe()` (that turns a swallowed failure into a false success); read tools probe
  with `safe`, write tools let failures raise and report honestly.

- **The live add-in is a development surface.** `sys_reload_addin` hot-reloads without restarting Fusion,
  and `sys_get_api_doc` + `sys_execute_script` let you prototype an `adsk.*` call against the live API
  before committing it as a tool. A team can keep their own tools and skills on a fork — version
  controlled together, hot-reloadable, gated per-tool where the blast radius warrants.

**The layering** (innermost first): the response/value substrate (`_common.py` — the `ok`/`error`
contract, `safe`, the design/component/sketch resolvers, unit scaling); the typed input/output kinds
(`_inputs.py` + `_outputs.py`); the MCP primitives (`mcp_primitives/` — the `Tool` builder, the `Item` that binds
a primitive to its handler + execution metadata, the registry); and the tool module itself
(`tools/<domain_verb>.py`), which holds only domain logic. Read any one tool file (e.g.
`workspace_orient.py` or `find_geometry.py`) to see the whole pattern in one place.

> Most of these conventions are now *enforced* rather than merely encouraged: write-status is a structured
> annotation (linted), enum inputs are typed `Choice`/`UnitField` kinds, occurrence/geometry references are
> typed kinds that refuse ambiguity, and outputs are declared via `_outputs.py` with a lint that asserts
> the ids are actually minted.

---

## Contributor setup and verification

Install/run/update/remove the add-in using the [README](README.md#installation). Fusion runs the
add-in with its embedded Python; running `Fusion-Essentials.py` in a terminal or installing an
unrelated `adsk` package does not provide a Fusion session. The Windows contributor commands below
use a separate Python 3.13 installation with the `py` launcher, pip and venv. Git is needed for a
contributor checkout; Node and a separately installed MCP SDK are not needed for these tests.

From the repository root in PowerShell, create an isolated environment without activating it:

```powershell
py -3.13 -m venv .cache/contributor-python
.\.cache\contributor-python\Scripts\python.exe -m pip install pytest
.\.cache\contributor-python\Scripts\python.exe -m pytest tests/unit -q
```

Dependency installation needs access to a Python package source; the unit-test command itself runs
offline, without Fusion or an MCP client. `tests/conftest.py` supplies the fake `adsk` objects and the
committed API facts. Keep the same interpreter for subsequent commands. To iterate on one tool,
replace `tests/unit` with its test file. Coverage and cold-agent eval dependencies are separate,
optional workflows, not prerequisites for this route.
Put agent scratch files and task artifacts in the gitignored `outputs/` directory; reserve
`tests/live/evals/results/` for actual eval runs.

| Check | What it requires and establishes |
|---|---|
| `python -m pytest tests/unit -q` | Handler and framework regressions against fakes; no native geometry or live acceptance claim. |
| `python tests/check_all.py --offline` | Generator checks, full unit/lint suite and source-matched live receipt; still requires installed Fusion binding files. Only the final live gate is skipped. |
| `python tests/check_all.py` | Maintainer acceptance with installed bindings, a matching live receipt and a reachable, correctly loaded Fusion add-in. |

In this table, `python` means the environment's `.cache/contributor-python/Scripts/python.exe`.
Plain `pytest` without the `tests/unit` path also collects lints that consult installed bindings.
A contributor without Fusion can submit the unit result and identify pending native checks; do not
refresh or edit generated facts or receipts to make that checkout appear live-verified. Tool source
changes invalidate the receipt until the maintainer loads and exercises that code. `--offline` is
not a workaround for absent binding files or a stale receipt.

Windows is the validated development platform. The manifest also targets macOS; that declaration
does not establish an equivalent tested contributor or native execution path. Keep production code
under `commands/mcpServer/` modular and dual-licensed (MIT/Apache headers). Read
[tests/CLAUDE.md](tests/CLAUDE.md) before adding tests and the
[tool-authoring guide](commands/mcpServer/tools/CLAUDE.md) before adding tools.

## Add-in command convention (how features are structured)

- Each feature is a `commands/<name>/` package with an `entry.py` exposing module-level
  `CMD_ID`, `CMD_NAME`, and `start()` / `stop()`. The `__init__.py` files are empty; the
  real code lives in `entry.py`.
- Register a feature by adding it to the `commands` list in `commands/__init__.py`.
- IDs follow `f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_...'` (COMPANY_NAME = `GTF`).
- Settings: a module calls `shared_state.load_settings_init(GROUP_ID, name, DEFAULTS, icon)`
  to get its own Settings tab, and `shared_state.load_settings(GROUP_ID)` to read it back.
  The settings UI renders **every key in a group as a checkbox/dropdown** — there is no
  hidden-field concept, so do not store internal/bookkeeping state in a settings group.
- Enablement and settings changes take effect on **reload**, not live (the live-toggle code
  in `commands/settings/entry.py` is commented out upstream).

## MCP server (`commands/mcpServer/`)

### Layout

- `mcp_server.py` (in `server/`) — HTTP + JSON-RPC server, **Streamable HTTP** transport (2025-03-26).
- `task_manager.py` (in `server/`) — marshals work onto Fusion's **main thread** via a custom event.
- `mcp_primitives/` — Tool / Item schema classes plus the registry.
- `tools/` — one module per tool, named exactly after the tool it registers (`<family>_<verb>.py`;
  `TOOL_MANIFEST.md` is the authoritative per-tool list). Each
  has a `handler(...)` (the logic; its parameters are the tool inputs), a `TOOL_DESCRIPTION`, a
  `tool = Tool.create_...`, an `item = Item.create_tool_item(...)`, and a `register_tool()`.
  Modules are **auto-discovered** by a `pkgutil` sweep — drop the file in, no registry edits.
  `_`-prefixed modules (`_common`, `_inputs`, `_outputs`, `_data_common`) are shared helpers.
- `entry.py` — starts/stops the server, runs the tool-discovery sweep, gates `sys_execute_script`.
- `README.md` (in that folder) — the **user-facing** doc (setup, security, platform).

### Hard rules (violating these causes crashes or hangs)

- **Anything touching `adsk.*` must run on Fusion's main thread.** Tools do this by setting
  `run_on_main_thread=True` on their `Item` (the default); the server marshals via
  TaskManager. Calling the Fusion API from a request/worker thread can crash Fusion.
- **Never block the main thread.** No `time.sleep`/polling loops and no synchronous HTTP in
  `entry.start()` or in a tool handler — they run on the UI thread. (The port self-check runs
  on a background thread for exactly this reason; `doc_open` deliberately does not poll.)

### Behavior that isn't obvious from the code

- The server binds **`127.0.0.1:27182`**, path **`/mcp`**. Startup checks port ownership;
  another listener on that address prevents startup. Do not assume Fusion's built-in MCP server
  always uses that port. Confirm the Essentials server identity through `/health`.
- What each tool does, and the exact `adsk.*` it calls, is not restated here: a tool's own
  `description` is its contract, and `sys_get_api_doc` searches the installed API's real signatures.
  This section keeps only the behaviors NOT visible from a description or the code.
- `doc_open` is **async** — `documents.open()` returns before the document is active.
- `sys_execute_script` uses Fusion's `Python.Run` text command. It is **Windows-tested only**;
  the temp-path handling normalizes `\`→`/` for cross-platform use but is **unverified on
  macOS**.
- **Never compare Fusion API objects with `is`.** The API returns fresh wrapper objects for the
  same underlying entity, so `occurrence.component is someComponent` silently fails. Match by
  `.name` (or compare entity tokens); an `is`-based resolver matches nothing (live-verified).
- **A Joint Origin inside a referenced/child occurrence must be joined via its assembly-context
  proxy**, not the native JO: `jo.createForAssemblyContext(occurrence)`. Passing the native JO
  yields "Provided input paths for joint are not valid". The `joint_create` tool resolves this
  automatically (root JOs are used as-is; sub-component JOs are proxied through the occurrence
  that instances them, matched by component name).

### The development loop (iterating without manual Fusion steps)

The add-in can restart *itself*, and that is what makes agent-driven tool development possible: an
agent can write a new tool, reload the server it is currently connected through, and exercise that
tool against the live Fusion session — without a human ever opening the Add-Ins dialog. The loop
closes. If you are building your own tools on top of this project, this is the loop to work in, and
it is the reason a session can go from "this tool is missing" to "this tool works" unattended.

The `sys_reload_addin` tool restarts the add-in so a connected agent can pick up code edits.
Workflow: edit a `tools/*.py` file → call `sys_reload_addin` (deferred: it responds, then the
server restarts in ~0.5s) → poll `GET http://127.0.0.1:27182/health` until it is back →
`tools/list` to confirm. `sys_reload_addin` picks up **brand-new** tool modules too (verified) —
the `pkgutil` sweep discovers any `tools/*.py` that exposes `register_tool()` on reload, so just
having dropped the file in is enough; no manual Stop/Run is needed. A manual Stop/Run in
Fusion's Add-Ins dialog is only required if the add-in failed to start (so no server is
running to call `sys_reload_addin` against). Note: an MCP **client** may cache the tool list, so
a newly registered tool can be invisible to the client until it reconnects — reconnect the
server in your client (`/mcp` in Claude Code) to refresh, or drive it over raw HTTP meanwhile.
The stale cache bites harder on an **edited** tool: the client serializes arguments against its
old schema snapshot, so a property it does not know about (a newly added array input, say) can
cross the wire silently mangled — a JSON array arriving as its string repr — while every other
property works, which looks like a handler bug. After any schema change, reconnect the client
before exercising the tool.

### Driving the server from outside Fusion (for testing)

POST JSON-RPC to `http://127.0.0.1:27182/mcp` with header
`Accept: application/json, text/event-stream`. Example tool call:
`{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"data_get","arguments":{}}}`.
Diagnostics: `GET /health`, `GET /tools`.

### Adding a new tool (the pattern)

1. Create `tools/<name>.py` with `handler(...)`, `TOOL_DESCRIPTION`, `tool`, `item`, and
   `register_tool()`. **That's all the wiring there is** — registration is auto-discovered (a
   `pkgutil` sweep of `tools/` calls each module's `register_tool()`), so you do **not** edit
   `tools/__init__.py` or `entry.py`. `_`-prefixed modules are treated as shared helpers and
   skipped. (`test_tool_autodiscovery.py` enforces this contract.)
2. Resolve any input that refers to existing geometry/occurrences/bodies through a typed kind in
   `_inputs.py` (`GeometryHandle`/`BodyRef`/`OccurrenceRef`/`PlaneRef`/`AxisRef`/`Choice`/…)
   rather than a hand-rolled `name: str` — the kinds refuse ambiguity and self-heal stale handles.
3. Ground every `adsk.*` call in the live API before writing it — the Fusion API is niche and
   easy to get wrong. `sys_get_api_doc` searches the installed version's real signatures and
   docstrings, so the reference can never go stale.
4. Write its test (see **Testing a tool** below), then `sys_reload_addin` and smoke-test live.

### Auditing for dead code

Unreferenced symbols and unused imports are enforced continuously by `test_dead_code.py`
(name-based, so a dead symbol masked by a live same-named one elsewhere is not flagged - unique
names are what make deadness statically provable). UNREACHABLE BRANCHES need runtime evidence
instead: generate candidates with branch coverage over the suite -

```powershell
.\.cache\contributor-python\Scripts\python.exe -m pip install pytest-cov
.\.cache\contributor-python\Scripts\python.exe -m pytest -q --cov=commands.mcpServer --cov-branch --cov-report=html
```

then review never-executed branches in `htmlcov/`. An uncovered branch is either dead code or a
missing test; both deserve action. This is a periodic audit, not a gate - coverage of live-API
wrapper paths is legitimately partial. The agent-facing surface has its own generated deadness
audit: the "Blindspots" section of [tests/generated/TOOL_POINTER_MAP.md](tests/generated/TOOL_POINTER_MAP.md)
(orphan tools, guidance pointing at tools that do not exist).

To check that a regression test can detect its intended defect, temporarily change the condition it
exercises, run that test, and restore the source. Use the bounded procedure in
[tests/CLAUDE.md](tests/CLAUDE.md#prove-a-test-actually-bites); `test_assert_strength.py` also rejects
assertions that rely only on a bare error flag.

### Testing a tool

Every tool module is unit-tested or carries a recorded excuse — `test_unit_coverage_complete.py`
reconciles the module list against the suite, so the decision is never silent. The tools run in a
live Fusion session, so the suite **mocks `adsk`** to exercise pure handler logic outside Fusion — and
because mocks can't catch a wrong `adsk.*` signature, geometry-touching tools are also **live-validated**
(`sys_reload_addin`, then call the handler on a real document).

- **Harness.** `tests/conftest.py` injects a lightweight mock `adsk` into `sys.modules` before any tool
  is imported, then `load_tool("<tool>")` spec-loads that one module in isolation and returns it, so a
  test can call `module.handler(...)` and its private helpers directly. The mocks implement only what a
  tool touches — extend them per tool as you go.
- **The fake pattern.** A test builds small `Fake*` objects that model just the read/write surface the
  handler uses (e.g. a `FakeDesign` with the `rootComponent`/`timeline` the handler reads), installs them
  on the module's `app`, and points `adsk.fusion.Design.cast` at them. Assert on the JSON payload the
  handler returns (decode `result["content"][0]["text"]`) and on the exact `adsk.*` calls the fake
  captured (so a regression to a wrong method name / argument fails here).
- **Cover the guards too** — unknown-units / no-active-design / out-of-range inputs, not just the happy
  path. The error contract (`isError`, `message`) is part of the tool's behavior.
- **Run it:** `.\.cache\contributor-python\Scripts\python.exe -m pytest tests/unit/test_<tool>.py -q` while iterating; before calling any
  change done, run THE button - `.\.cache\contributor-python\Scripts\python.exe tests/check_all.py` - which chains the generator checks, the
  whole suite, the live-run receipt check, and the live gate, each failure naming its own repair.
  Without a live session, `--offline` skips the final live gate but still needs installed bindings
  and a current receipt. For the contributor route without Fusion, run `tests/unit` as above and
  report the remaining maintainer checks.
- **Changed tool source?** The receipt check goes red until the live suite has seen your code:
  run `.\.cache\contributor-python\Scripts\python.exe tests/live/tool_verify.py` with Fusion up (a green run rewrites
  `tests/live/VERIFIED_TOOLS.md` - commit it with your change). Reload the add-in first so the live
  session runs the code you just edited. Read the receipt's buckets honestly: a `covered` tool had
  a step whose predicate read a VALUE off the payload; a `called` tool only passed bare `ok` steps
  (the call did not fail - nothing about its effect was read). A new step for an Edit tool should
  read the effect back, not just `ok`.
- **Regenerate the docs:** `.\.cache\contributor-python\Scripts\python.exe tests/gen_all.py` rebuilds everything under `tests/generated/`
  (TOOL_MANIFEST + the CLAUDE.md maps from the registry, TOOL_POINTER_MAP from source,
  PERMISSION_POSTURE from the write annotations); run it whenever check_all says an artifact is stale.
- **New adsk API?** If your tool references an enum family the generated `live_api_facts.py` has
  not measured, the suite goes red with the one command that fixes it: run
  `.\.cache\contributor-python\Scripts\python.exe tests/live/measure_api.py` with Fusion up, then commit the regenerated facts.

See [commands/mcpServer/README.md](commands/mcpServer/README.md) for the user-facing setup,
the full tool list, and the security model.
