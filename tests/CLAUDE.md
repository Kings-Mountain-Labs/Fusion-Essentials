# Writing a test here

Before calling any change done: `py -3 tests/check_all.py` - the one command (generator checks +
suite + live-run receipt + live gate, each failure naming its repair).

This is the test-authoring recipe for `tests/`. Read the root [CLAUDE.md](../CLAUDE.md) for the
constitution and [tests/README.md](README.md) for the full harness explanation (how `conftest.py`
mocks `adsk`, the fake pattern, the triage for which tools need tests at all); this file is the
short, mandatory-for-new-tests version of "which pattern to copy."

## The canonical pattern (mandatory for new tests)

Every test imports its tool with `conftest.load_tool("<tool>")`, then sets up ALL state with a
`@pytest.fixture` and `monkeypatch.setattr` — never an imperative `mod.app = …` poke at module or
test-body level. A fixture's patches undo themselves after the test runs, so nothing leaks into the
next one; an imperative assignment does not undo itself.

Pick the shape that matches what you're testing:

- **A rich read** (`<domain>_get`, or a router that dispatches to `_slice_*`/measurement-core
  helpers) → copy **`test_design_get.py`** or **`test_model_inspect.py`**. A fixture stubs the
  router's internal slice functions with `monkeypatch.setattr`; tests assert the ROUTER's
  composition (default = the orientation slice only, each `include=` adds exactly its slice, the
  note advertises the rest) — not the underlying Fusion calls, which are covered by live validation,
  not re-mocked here. `test_cam_get.py` is the same shape.
- **A tool that needs a fuller fake object model** (bodies, occurrences, components) → copy
  **`test_model_mirror.py`**. It builds a design with the shared `make_design(...)` /
  `MakeComp` / `MakeDesign` fakes and wires it into the tool module with `install(mod, design)`,
  inside a fixture. `install` patches BOTH seams a tool can read `design()` through — its own
  `_common` and, when the tool also imports `_inputs`, `_inputs._common` too (see "the dual-seam
  trap" below) — plus `adsk.fusion.Design.cast` and `adsk.core.ObjectCollection.create`. If
  `MakeComp`/`MakeDesign` lack a surface your tool needs, extend them in the design family module
  under `tests/fakes/` — don't fork a bespoke `Fake*` hierarchy into your test file.
- **A pure function** (parse/encode/convert, no Fusion at all) → copy **`test_quoting.py`**. No
  `adsk` surface to fake; call the function and round-trip the result.

Then: read the tool, list its `_helper` functions and the `handler`, and write one test per
specific, plausible bug (not one per function) — the name should read like a spec line
(`test_picks_largest_body_by_volume`). Assert on concrete values (`adsk.*`
mocks return a truthy child `Mock` for anything unmodeled, so `assert result is not None` proves
nothing). Cover sizes 0, 1, 2, N for anything taking a collection, and the guards (bad units, no
active design, missing/ambiguous target) alongside the happy path. If a tool reads an `adsk`
attribute the mocks don't model yet, add it to `install_mock_adsk()` (or a `.cast` pass-through)
once, in the harness, rather than re-mocking it per test.

**Deleting an attribute off the shared `adsk` mock does not reliably undo itself.** `monkeypatch`
cannot restore a `Mock` child it marked `_deleted`, so the deletion can outlive the test: one
`monkeypatch.delattr(adsk.fusion, "DistanceUnits")` passed on its own and took 9 unrelated tests down
under randomized order. Many tests here do delete an `adsk` member to cover a "this build lacks it"
branch and are fine - the boundary between those and the one that leaked is not established, and
whole ENUM FAMILIES the shared fakes read are the known-bad case. So: prefer another route to that
branch (an unknown key, a `getattr` default, a `safe()` that returns the default). If you delete
anyway, run `-p randomly` over the full suite before believing it.

For each test ask: **what specific, plausible bug would this catch?** If the only answer is "the
function was deleted," it is decoration — assert the value that would change if the logic were wrong.
A test may only pin behavior that is CORRECT: pinning a wrong result (e.g. a first-match resolver's
substring hit) locks the defect in place, so when a handler's behavior is corrected the test that
asserted the wrong behavior SHOULD go red — that red is the signal to update the assertion to the
correct value, not evidence the change was wrong.

Every unit test stands on the shared fakes, which live one module per Fusion family under
`tests/fakes/`, over one scaffold module the families share. `conftest.py` re-exports every one of
them, so a test imports its fakes from conftest and a new fake joins its family's module. A type
whose shape live Fusion was measured for (a key of `live_api_facts.SHAPES`) uses its shared fake -
import it, or subclass it and add only the extra the test needs; `test_fake_shapes_exist.py` refuses
a free-standing local double of such a type, because the shape sweep never reaches a local copy. A
type with no shape dump has no shared fake to stand on and keeps a local double, whose one-line
docstring says that.

## The dual-seam trap

A tool that resolves geometry/occurrences through a typed kind in `_inputs.py` reads the active
design through TWO import paths: its own `from . import _common` AND `_inputs`'s own `from . import
_common` (imported again inside `_inputs.py`). Patching only `mod._common.design` leaves
`mod._inputs._common.design` pointing at the real (absent, in tests) Fusion app — so an `_inputs`
kind's resolution silently fails even though the handler's own reads work. Patch BOTH seams to the
SAME design object, inside a fixture so it's torn down: `conftest.install(mod, design)` does this for
you; if you patch by hand, patch `mod._common.design` and `mod._inputs._common.design` together.

## Comments and API facts in tests

A test comment is evergreen: the present-tense fact, a few lines at most - no process notes,
observation diaries, or plan references (enforced by `test_evergreen_no_baggage.py`). An adsk API
fact (an enum int, a behavior flag) is never hand-typed: it comes from the generated
`live_api_facts.py` - the mock adsk enums arrive pre-seeded with measured values, the shared
fakes read their behavior flags from it, and `test_no_hand_seeded_enums.py` bans hand-assigning a
measured member. If a fact you need is missing, add a measurement row to `measure_api.py` and
regenerate against live Fusion.

## Prove a test actually bites

A test that can't fail is decoration. After writing one, sanity-check it by temporarily breaking the
code it covers (flip a comparison, change a constant) and confirming the right test goes red — then
restore the code. Do this especially for a new guard or cap: a `truncated` flag or an ambiguity
refusal is easy to write in a way that always passes.

The harness sets `sys.dont_write_bytecode = True` for exactly this workflow: the tool loader
spec-loads source files, and mtime-keyed `.pyc` caches can otherwise go stale when you edit-then-
restore a tool quickly during that same break/confirm-red/restore cycle, masking the restored source.

## Updating tests as behavior changes

**The test changes in the same commit as the behavior it describes.** A red test after a code change
is the suite telling you a promise changed - confirm you meant it, then update the test. Never edit a
test purely to make it pass without understanding why it broke (refactor that changed behavior ->
fix the code; test asserting an implementation detail -> fix the test).

## Regenerating docs

`py -3 tests/gen_all.py` rebuilds everything under `tests/generated/` (TOOL_MANIFEST + the
CLAUDE.md maps from the registry, TOOL_POINTER_MAP from source, PERMISSION_POSTURE from the write
annotations); `--check` fails if anything is stale (also enforced by `test_generated_docs_current.py`).
