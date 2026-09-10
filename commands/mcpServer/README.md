# Fusion-Essentials MCP Server

A local [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that lets an AI agent
(Claude, or any MCP client) work in your live Fusion session. It covers the design side (sketching,
modelling, surfaces and meshes, joints and assembly), the manufacturing side (setups, toolpaths,
posting), drawings and annotations, and the cloud data model, plus the reads and screenshots needed
to check any of it.

Every tool does one job and assumes nothing about your process. A procedure is something you build
on top of these calls in your client, not something the server decides for you. Inputs are typed, so
a vague reference is refused rather than guessed at, and a tool that writes checks its own effect
before it reports success (see *What makes the tools trustworthy*).

It is **off by default** and runs only on your own machine (loopback).

## Enabling it

1. [Install and run Fusion-Essentials](../../README.md#installation) in Fusion.
2. Open **Fusion Essentials Settings**, and on the feature-enablement tab tick
   **Enable MCP Server**.
3. Reload Fusion-Essentials (Add-Ins dialog → Stop, then Run). The setting takes
   effect on reload.
4. The endpoint is `http://127.0.0.1:27182/mcp`. Open `http://127.0.0.1:27182/health` in a browser
   and check both `"status": "healthy"` and `"server": "Fusion-Essentials MCP Server"`.

Optional tool **families** (`appearance`, `cam`, `data`, `drawing`, `mesh`, `save`, `surface`) can each
be disabled with a checkbox under **Settings → MCP Server**, to shrink the tool surface an agent has
to load when you don't need that domain. All families are enabled by default. Like the other MCP
settings, a disabled family takes effect on reload — its tools are not registered, and
`workspace_orient` / `sys_capability_map` / `sys_find_tool` reflect the available tools and gates.
Reconnect your MCP client after reload so it refreshes its tool list. To disable MCP, untick
**Enable MCP Server** and reload; stopping the add-in also stops its server.

### Port note

Fusion-Essentials binds `127.0.0.1:27182` and checks the responding server's identity. Another
server conflicts only if it occupies that endpoint; Fusion's built-in MCP server does not always
use this port. On a collision, stop the process holding it and reload Fusion-Essentials. If that
process is Fusion's built-in server, turn off **Preferences → Fusion MCP Server**. Its configuration
is separate from this add-in's settings.

## Connecting a client

The server speaks the **Streamable HTTP** MCP transport, so clients that support an
HTTP transport can connect directly from the same machine — no `mcp-remote`/Node bridge needed.

**Claude Code** (project-scoped `.mcp.json` at the repo root):

```json
{
  "mcpServers": {
    "fusion-essentials": {
      "type": "http",
      "url": "http://127.0.0.1:27182/mcp"
    }
  }
}
```

This repository includes that [`.mcp.json`](../../.mcp.json). For another client, add a Streamable
HTTP server with the same URL. Approve or enable the server in the client as required, then check
that its tool list contains `workspace_orient`. A healthy endpoint alone does not establish a client
connection. Review the client permission presets under *Security* before adopting them.

## Why specific tools instead of "just let it write scripts"

Fusion has a Python API, and a capable model can write against it. Handing an assistant a single
"run this script" endpoint is a fair design, and it covers a lot of ground fast. This server went
the other way for one reason: writing the code was never the part that went wrong. Knowing whether
it had done the right thing was.

Working without checks, an assistant gets caught out in a few recurring ways.

**It gets a convention wrong.** Sketch planes, hole directions and pattern axes do not always line
up with the axes you would expect. A script built on the wrong assumption still runs, still reports
success, and quietly builds the wrong shape. Nothing in the result says so.

**It loses track of what it is pointing at.** References to faces and edges shift when a model
rebuilds, and Fusion does not require names to be unique anywhere. A reference that finds
*something* is not the same as a reference that finds the right thing.

**It cannot check its own work.** "Success" on its own proves very little, and the obvious sanity
checks (a count, a total volume) often cannot tell two quite different designs apart. An assistant
can verify what it did and still be wrong about it.

The tools are shaped around those three. Reads hand back the plane's orientation, the units and a
durable reference before you act on anything. Inputs refuse a vague reference rather than guess at
it. Anything that writes reads the model back afterwards and reports the change it measured. The
mechanisms are in *What makes the tools trustworthy*, below.

There is still a general script tool, `sys_execute_script`, for cases the others do not cover. It is
off by default and gated separately from everything else (see *Security*). Most of the work here
exists so that you rarely need to reach for it.

## What the agent is told

You do not have to coach the assistant on how to use this. When a client connects, the server sends
an instruction block that routes it to two cheap orientation reads before it touches anything else,
and each tool then carries its own contract, its next-step pointers, and whichever Fusion traps
apply to it.

Fusion is several environments glued together (CAD, assemblies, the parametric timeline, CAM, the
cloud data model), and most wasted effort comes from acting before knowing which one you are in.
The places a reading looks authoritative but isn't are stated by the tool that owns them, so they
arrive when they are relevant rather than in a list nobody re-reads: CAM validity is untrustworthy
until Manufacture has been opened, saves and exports complete asynchronously, Fusion enforces no
name uniqueness anywhere.

If you are writing tools rather than using them, that doctrine and the reasoning behind it live in
the `CLAUDE.md` files beside the code.

## Tools

**The authoritative tool inventory is [`tests/generated/TOOL_MANIFEST.md`](../../tests/generated/TOOL_MANIFEST.md)** —
generated from the live registry (every tool, each with its inputs and write level) — or ask a
connected client for its tool list (`tools/list`). Each tool's own `TOOL_DESCRIPTION` is the
contract the agent sees; this README does not restate it (a second copy only drifts). Tool names
are predictable: `<family>_<verb>`, so the family prefix tells you the area —

| Prefix | Area | Examples |
|--------|------|----------|
| `sys_` | session / introspection / the script escape hatch | `sys_find_tool`, `sys_get_api_doc`, `sys_execute_script` |
| `data_` | the cloud data model (projects, folders, files) | `data_get`, `data_upload_file`, `data_delete_file` |
| `doc_` | document lifecycle (open / save / copy / insert) | `doc_open`, `doc_save_as`, `doc_insert_occurrence` |
| `design_` | the active design as a whole (tree, timeline, mode) | `design_get`, `design_configure`, `design_recompute` |
| `sketch_` | 2D sketching | `sketch_create`, `sketch_add_geometry`, `sketch_constrain` |
| `model_` | solid features | `model_extrude`, `model_revolve`, `model_fillet`, `model_pattern_circular` |
| `joint_` | joints & joint origins | `joint_create`, `joint_at_geometry`, `joint_create_origin` |
| `assembly_` | positioning & kinematic state | `assembly_get`, `assembly_ground`, `assembly_move` |
| `param_` | parameters | `param_get`, `param_set`, `param_add` |
| `find_` | geometry queries returning handles | `find_geometry` |
| `view_` | workspace & viewport (screenshots, isolate, section) | `view_screenshot`, `view_set`, `view_section` |
| `cam_` | manufacturing (setups, operations, toolpaths) | `cam_get`, `cam_generate`, `cam_get_status` |
| `pmi_` | model-based annotations (notes, leaders, GD&T) | `pmi_get`, `pmi_create`, `pmi_edit` |
| `appearance_` / `mesh_` / `surface_` | colour, mesh bodies, surface modelling | `appearance_set`, `mesh_export`, `surface_thicken` |
| `drawing_` / `workspace_` / `save_` | 2D drawings, orientation, save-as-mesh | `drawing_create`, `workspace_orient`, `save_as_mesh` |

Tool definitions expose `readOnlyHint` and, for destructive tools, `destructiveHint` annotations.
These describe behavior; the client's permission policy determines whether it asks before a call.

### A few core tools (the ones a session leans on)

These are the load-bearing reads that the design philosophy (above) is built around — start
here when learning the surface:

- **`workspace_orient`** — the cold-boot read. One call reports what's open, its health,
  whether CAM data exists, the major pieces, and *pointers* to the right narrow tool next.
  Call it first.
- **`assembly_get`** — kinematic state as JSON: every occurrence's world position, ground
  flags, and joint wiring. The numbers you reason about instead of a cluttered screenshot.
- **`find_geometry`** → **`joint_at_geometry`** — the geometry-as-values pair. `find_geometry`
  returns stable *handles* to faces/edges (filterable by radius/proximity); you pass a handle
  to a consumer like `joint_at_geometry`, which lands the joint AT that exact geometry.
- **`view_set`** + **`view_screenshot`** — the agent's "eyes": isolate/orient a single
  component, then capture it (a screenshot of a whole assembly is the least reliable input).
- **`sys_execute_script`** — the gated escape hatch: arbitrary Fusion Python, off by default
  (see Security). The typed tool surface exists so this is rarely needed.

### Things that aren't obvious from a tool's name

- The CAM tools read CAM data **without requiring you to switch to the Manufacture
  workspace**.
- `doc_open` / `doc_save_as` are **asynchronous**: the call returns before the document is
  fully active/saved — confirm with `workspace_orient` / `doc_get` afterward.
- `cam_get(include=['time'])` needs generated toolpaths to be meaningful.
- `cam_generate` is fire-and-poll: it returns immediately with a handle; poll `cam_get_status`
  until done (it never blocks for the multi-minute compute).

## What it gets used for

Three fairly different jobs, all built from the same calls, which is the test of whether the tools
really are workflow-agnostic:

- **A repeatable procedure.** A skill in your client runs a fixed sequence where the order matters
  and you want the same result every time. The steps and the checks live with you, outside the
  server, and the tools underneath stay general.
- **Open-ended modelling.** The assistant gets its bearings, reads the state, does something, checks
  what actually happened, and decides what to do next — closer to how a person works than one long
  script that either succeeds or leaves you reading a stack trace.
- **Reading, auditing and data work.** Checking a design's health, its parameters, its external
  references or its CAM setup; walking the cloud project structure; answering questions across a
  pile of files you would otherwise open one at a time. Much of this never writes anything, and it
  is where an assistant tends to earn its keep first.

Two runnable demonstrations show the surface driving real work end-to-end:

- **A shipped procedure** (a Claude Code skill in `.claude/skills/`): `insert-into-template` stands
  up a CAM job for a part — saves the CAD, defines a part-space origin, places the shop template,
  inserts and positions the part, and sizes stock from measurements. It is built entirely from
  these tools; fork it as the pattern for your own shop procedures.
- **The eval pipeline** (`tests/live/evals/scenarios/`): goal-shaped scenarios (parametric
  multi-part foundations, joints and motion, detail features, external references, CAM templating)
  that a context-isolated agent runs against a live session holding only this server's wire — the
  standing proof that the tool descriptions alone can carry an agent from a goal to a verified
  result.

## What makes the tools trustworthy (the contracts)

Three typed kind systems make false success structurally hard. They are what to study if you are
forking this as a pattern for your own MCP server:

- **Inputs** ([`tools/_inputs.py`](tools/_inputs.py)) — a tool never takes a bare `name: str` for
  existing geometry. Typed kinds resolve names and handles, REFUSE ambiguity instead of guessing
  an instance, and self-heal a stale geometry handle from its world-position locator.
- **Outputs** ([`tools/_outputs.py`](tools/_outputs.py)) — a tool declares `RETURNS = [...]`
  (handles, URNs, names, verdicts); tests assert the declared keys are actually minted, and an
  assertion read's verdict is a real boolean beside its measured evidence, never prose.
- **Postconditions** ([`tools/_assert.py`](tools/_assert.py)) — a write tool declares
  verify-the-effect kinds; the kernel re-reads ground truth after the mutation and converts a
  "success" that changed nothing into an error. Fusion really does report success while changing
  nothing, which is the whole reason this layer exists. Where the effect is material, the evidence is
  geometric: a cut that removed no volume, a mesh trim that moved neither count nor area, a delete
  whose target still resolves — each is an error carrying its measurements, never a false ok.

Two further habits run through every payload. **Honest reads**: a value that cannot be read is
published as null (or the key is absent), never a fabricated 0/False/echo of the request — so an
agent can always tell "measured as nothing" from "could not be measured" — and a partial success
(a joint created but a limit refused, a rename the platform declined) is disclosed with exactly
what landed. **Identity by handle**: Fusion enforces no name uniqueness at any level (even two
siblings can share a full path), so structural reads emit each entity's `handle` (its
entityToken), every reference-taking input accepts it as the exact identity, and a name several
entities answer to is refused with the candidates' handles rather than silently matched.

The naming schema (`<family>_<verb>`, with the verb's read/write kind linted against the declared
write level), pure-ASCII wire strings, helper deduplication, and doc freshness are all enforced by
lints in `tests/`. Run `tests/check_all.py` with the
[contributor environment](../../CONTRIBUTING.md#contributor-setup-and-verification) for generator
checks, the suite and maintainer live acceptance. Generated inventories live in `tests/generated/`:
[`TOOL_MANIFEST.md`](../../tests/generated/TOOL_MANIFEST.md) (per-tool) and
[`TOOL_POINTER_MAP.md`](../../tests/generated/TOOL_POINTER_MAP.md) (how tools point to each other, plus a
self-audit of the guidance strings). Authoring conventions live in
[`CONTRIBUTING.md`](../../CONTRIBUTING.md) and the `CLAUDE.md` files beside the code.

## Security

- **Local transport.** The server binds `127.0.0.1` and rejects non-loopback web origins. It has no
  authentication token and permits requests without an Origin header, so other local processes can
  connect. A connected AI client may send tool results to its model provider.
- **Registration gates.** The server is off by default and runs only while the add-in is running.
  Family checkboxes remove those tools on reload; they are independent of client permissions.
- **`sys_execute_script` is separately gated.** It lets a connected agent run
  arbitrary Python in your active Fusion session — including modifying or deleting
  your design. It is **disabled by default**; enable it only if you trust the agent
  and the client connecting to the server, via **Settings → MCP Server → "Allow AI to
  execute arbitrary Fusion API scripts (advanced; security risk)"** (then reload). Scripts can also
  access local files and network resources. The default execution path groups document changes into
  an undo transaction, but an error does **not** guarantee rollback; verify state afterward.
- **Read-only script option.** `sys_execute_script(read_only=true)` uses Fusion's `MCP.Execute`
  read-only context to reject design changes. It still permits file writes and still requires the
  script registration and client permission gates. Builds without that text command return an error.
  This is not a server-wide read-only mode or a Python sandbox.
- **Document guard.** When supplied, `expect_document` refuses a write when the active
  document differs from the target. This checks identity, not permission to write.
- **Client policies differ.** [`.claude/settings.json`](../../.claude/settings.json) allows the listed
  read tools and leaves writes out of its allow list.
  [`.codex/config.toml`](../../.codex/config.toml) sets `approve` for listed typed tools, including
  writes; cloud file/folder deletion and `sys_execute_script` use `prompt`, as do unlisted tools.
  These project files apply only when the client loads them, and other client settings can change the
  effective policy. Inspect the active policy before use; a tool annotation is not an approval gate.
  Keep personal Claude overrides in the ignored `.claude/settings.local.json` file.

## Platform support

Developed and tested on **Windows**, using Fusion's embedded Python. No separate server runtime
or package installation is needed. The manifest targets Windows and macOS; that declaration is not
verification of macOS support. macOS remains unverified, including script execution and settings UI.
