"""Generate a tool + input-kind MANIFEST from the LIVE registry.

The registry IS the inventory: every ``tools/<name>.py`` self-registers a Tool with a name, a
write-status, a description, and an input schema. This script imports each tool module (against the
test harness's mocked ``adsk``), pulls those facts off the registered primitives, also collects the
typed ``InputKind`` subclasses from ``_inputs.py``, and renders ``tests/generated/TOOL_MANIFEST.md`` — one grouped
"what tools + kinds exist" reference.

Why generate instead of hand-write: a static index goes stale the instant a tool is added (a prior
``TOOL_INDEX`` was removed for exactly that). This regenerates from the registry, and ``--check`` fails
the suite when the committed ``TOOL_MANIFEST.md`` drifts — so the inventory cannot lie. It is the GENERATIVE
counterpart to the live ``sys_find_tool`` lookup (same data, batch form): a reviewer or a cold-booting
agent gets the whole map in one file, and the build guarantees it is current.

Run from the repo root:

    py -3 tests/gen_manifest.py          # writes tests/generated/TOOL_MANIFEST.md
    py -3 tests/gen_manifest.py --check  # exit 1 if TOOL_MANIFEST.md is stale
"""

import argparse
import inspect
import os
import sys

# This script imports the tool modules, which do ``import adsk`` at module top. When run standalone
# (not under pytest) the mocked adsk isn't installed yet — install it the way conftest does, BEFORE any
# tool import. Under pytest, conftest has already installed it; install_mock_adsk is idempotent.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest  # noqa: E402
conftest.install_mock_adsk()

from conftest import load_tool, TOOLS_DIR, COMMANDS_DIR  # noqa: E402

# load_tool puts COMMANDS_DIR on sys.path lazily (first call); collect() imports the registry up front,
# so ensure the path now — same seam load_tool uses, so the registry object is the one tools register into.
if COMMANDS_DIR not in sys.path:
    sys.path.insert(0, COMMANDS_DIR)

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated", "TOOL_MANIFEST.md")

# Tool name prefix -> family label. The order here is the manifest's section order; a tool falls into
# the FIRST prefix it matches (so 'design_get' -> design, 'sys_find_tool' -> sys). A tool matching
# none lands in "other" — which the family test asserts stays empty-or-accounted-for by total count.
_FAMILY_PREFIXES = [
    ("model_", "model"), ("surface_", "surface"), ("mesh_", "mesh"), ("sketch_", "sketch"),
    ("cam_", "cam"), ("assembly_", "assembly"), ("joint_", "joint"), ("design_", "design"),
    ("doc_", "doc"), ("data_", "data"), ("drawing_", "drawing"), ("param_", "param"), ("pmi_", "pmi"),
    ("view_", "view"), ("find_", "find"), ("workspace_", "workspace"), ("appearance_", "appearance"),
    ("save_", "save"), ("sys_", "sys"),
]


def _first_sentence(text: str, limit: int = 160) -> str:
    """First sentence of a description (mirrors sys_find_tool's summary), trimmed."""
    if not text:
        return ""
    s = text.split(". ")[0].strip()
    return s[:limit] + ("..." if len(s) > limit else "")


def _write_status(item) -> str:
    """'read' / 'write' / 'destructive' from the tool's annotation hints (the same source the server
    reports). Defaults to 'write' if a tool somehow declared nothing (test_write_status enforces one)."""
    d = item.to_dict()
    ann = d.get("annotations") or {}
    if ann.get("readOnlyHint"):
        return "read"
    if ann.get("destructiveHint"):
        return "destructive"
    return "write"


def _tool_modules():
    """Every importable tools/*.py module name (skips the _-prefixed shared helpers and __init__)."""
    for fn in sorted(os.listdir(TOOLS_DIR)):
        if fn.endswith(".py") and not fn.startswith("_") and fn != "__init__.py":
            yield fn[:-3]


def collect():
    """Walk the registry for tool records + the _inputs.py kinds. Returns {'tools': [...], 'kinds': [...]}.

    Each tool module is imported (registering its Item) then read off a FRESH registry so we attribute
    each registered tool to nothing but itself. (A module may register more than one tool — we take all
    of them.)

    ISOLATION: collect() imports EVERY tool module (some for the first time) and churns the shared
    registry singleton. Under pytest that would leak into the next test (a populated registry, or an
    adsk-mock attribute a module reassigns at import time). So we snapshot the registry singleton and
    the shared adsk mock dicts up front and restore BOTH in a finally — collect() leaves the process
    exactly as it found it. (Standalone, restore is a harmless no-op.)"""
    from mcpServer.mcp_primitives import registry

    saved_registry = registry._registry_instance
    saved_adsk = conftest._snapshot_adsk_dicts()
    try:
        return _collect_unguarded(registry)
    finally:
        registry._registry_instance = saved_registry
        conftest._restore_adsk_dicts(saved_adsk)


def claim_name(owner, name, mod_name):
    """Record which MODULE registered `name`, raising SystemExit if a second module claims it.

    The registry is one flat namespace, and the live server registers every module into a SINGLE
    registry - so two modules registering the same tool name means one silently wins there. This
    walk resets the registry per module, which would otherwise hide that collision entirely (each
    module looks clean in isolation). Both files are named so the duplicate is fixable at a glance.
    Shared with gen_wiring so the manifest and the pointer map refuse the same collision.
    """
    prev = owner.get(name)
    if prev is not None and prev != mod_name:
        raise SystemExit(
            f"tool name collision: {name!r} is registered by BOTH tools/{prev}.py and "
            f"tools/{mod_name}.py. The registry is one flat namespace, so on the live server one "
            "of them silently wins and the other tool is unreachable - rename one.")
    owner[name] = mod_name


def _collect_unguarded(registry):
    tools = []
    owner = {}
    for mod_name in _tool_modules():
        mod = load_tool(mod_name)
        rt = getattr(mod, "register_tool", None)
        if not callable(rt):
            continue
        registry.reset_registry()
        rt()
        for item in registry.get_tools():
            name = item.get_name()
            claim_name(owner, name, mod_name)
            d = item.to_dict()
            props = list((d.get("inputSchema") or {}).get("properties", {}).keys())
            tools.append({
                "name": name,
                "module": mod_name,
                "write": _write_status(item),
                "summary": _first_sentence(d.get("description", "")),
                "inputs": props,
            })
    tools.sort(key=lambda t: t["name"])

    return {"tools": tools, "kinds": _collect_kinds(), "helpers": _collect_helpers()}


def _collect_kinds():
    """The typed InputKind subclasses in _inputs.py (name + first docstring line) — the 'what already
    exists to REFERENCE geometry/profile/body/etc.' half, so the manifest doubles as the anti-drift
    catalog sys_find_tool searches."""
    _inputs = load_tool("_inputs")
    base = getattr(_inputs, "InputKind", None)
    out = []
    if base is None:
        return out
    for cname, cls in inspect.getmembers(_inputs, inspect.isclass):
        if cls is base or not issubclass(cls, base):
            continue
        doc = (inspect.getdoc(cls) or "").strip().split("\n")[0]
        # MAP_HINT (a curated one-liner ON the kind) drives the terse CLAUDE.md map; the docstring's
        # first line is the fuller summary for TOOL_MANIFEST.md. A blank MAP_HINT shows up blank in the map
        # — the signal to a kind-author to fill it in.
        out.append({"kind": cname, "summary": doc[:160], "hint": getattr(cls, "MAP_HINT", "")})
    out.sort(key=lambda k: k["kind"])
    return out


# The shared `_`-prefixed helper modules to surface in the "reuse before you write" list. Listed
# explicitly (not by globbing tools/_*.py) so a NEW helper is a deliberate one-line add here AND a
# MAP_BLURB on the module — the same self-disclosing pattern as a kind's MAP_HINT. (test conftest's
# load_tool is the importer; _data_common etc. import cleanly under mocked adsk.)
def _helper_modules():
    """Every `_`-prefixed module under tools/ - the catalog enrolls them ALL, so a shared helper
    cannot be silently invisible to the reuse map (a hardcoded list dropped 8 of 20 once, five of
    them carrying a MAP_BLURB that reached nothing)."""
    tools_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "commands", "mcpServer", "tools")
    return tuple(sorted(
        fn[:-3] for fn in os.listdir(tools_dir)
        if fn.startswith("_") and fn.endswith(".py") and fn != "__init__.py"))


def _collect_helpers():
    """(module, blurb) for each shared helper — blurb from the module's MAP_BLURB string (a curated
    terse 'what to reuse from here'), falling back to the first docstring line. A helper with neither
    shows up blank — the signal to add a MAP_BLURB."""
    out = []
    for name in _helper_modules():
        mod = load_tool(name)
        blurb = getattr(mod, "MAP_BLURB", "")
        if not blurb:
            doc = (mod.__doc__ or "").strip().split("\n")[0]
            blurb = doc[:120]
        out.append({"module": name, "blurb": blurb})
    return out


def families(tools):
    """Group tool records by name-prefix family (first match wins); leftovers go to 'other'."""
    groups = {}
    for t in tools:
        label = "other"
        for prefix, name in _FAMILY_PREFIXES:
            if t["name"].startswith(prefix):
                label = name
                break
        groups.setdefault(label, []).append(t)
    return groups


_MARK = {"read": "·", "write": "✎", "destructive": "⚠"}


def render(data) -> str:
    tools = data["tools"]
    kinds = data["kinds"]
    fam = families(tools)

    lines = [
        "# Tool & Input-Kind Manifest (generated)",
        "",
        "_Auto-generated from the live registry by `tests/gen_manifest.py`. Do not edit by hand —"
        " re-run the generator after adding/renaming a tool or kind. `--check` fails the suite if this"
        " is stale. This is the batch form of the `sys_find_tool` live lookup: the one place to see what"
        " already exists before building it._",
        "",
        f"**Tools:** {len(tools)}  |  **Input-kinds:** {len(kinds)}  |  "
        "write-status: `·` read · `✎` write · `⚠` destructive",
        "",
        "## Input kinds — reference EXISTING geometry/structure with these (don't hand-roll a name/index)",
        "",
        "Before adding a tool input that points at a face/edge/body/plane/axis/profile/occurrence, use"
        " one of these (extend the kind if it's close). See `CLAUDE.md` 'Input kinds'.",
        "",
        "| Kind | What it references |",
        "|---|---|",
    ]
    for k in kinds:
        lines.append(f"| `{k['kind']}` | {k['summary']} |")
    lines.append("")
    lines.append("## Tools by family")
    lines.append("")

    # Section order = _FAMILY_PREFIXES order, then any leftover families, then 'other'.
    order = [name for _, name in _FAMILY_PREFIXES]
    for extra in sorted(fam):
        if extra not in order:
            order.append(extra)
    for label in order:
        items = fam.get(label)
        if not items:
            continue
        lines.append(f"### {label}")
        lines.append("")
        lines.append("| | Tool | Summary |")
        lines.append("|---|---|---|")
        for t in sorted(items, key=lambda x: x["name"]):
            lines.append(f"| {_MARK[t['write']]} | `{t['name']}` | {t['summary']} |")
        lines.append("")
    return "\n".join(lines)


# ── the token-efficient MAP spliced into CLAUDE.md (for a tool-AUTHOR agent at session start) ──────
#
# TOOL_MANIFEST.md (above) is the full browse-everything file. The CLAUDE.md map is the OPPOSITE: the
# shape of the space, tiny enough to sit in a code-author agent's context the moment they open the repo —
# the kinds catalog (the invisible abstraction they must not re-invent) + family names+counts (so they
# know roughly where to point sys_find_tool / which TOOL_MANIFEST.md section to read). Spliced between
# markers so the surrounding hand-written prose is untouched and the block can't rot.

CLAUDE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "CLAUDE.md")
TOOLS_CLAUDE_PATH = os.path.join(os.path.dirname(CLAUDE_PATH), "commands", "mcpServer", "tools", "CLAUDE.md")
# Root CLAUDE.md carries only the tiny families census (orientation for ANY session). The full
# kinds + helpers catalog is an authoring concern, so it lives in tools/CLAUDE.md - loaded only when
# you work in tools/. Both are generated from the same source, so neither can drift.
_FAM_BEGIN = "<!-- BEGIN GENERATED FAMILIES (py -3 tests/gen_manifest.py) -->"
_FAM_END = "<!-- END GENERATED FAMILIES -->"
_CAT_BEGIN = "<!-- BEGIN GENERATED CATALOG (py -3 tests/gen_manifest.py) -->"
_CAT_END = "<!-- END GENERATED CATALOG -->"


def render_families(data) -> str:
    """The one-line families census for root CLAUDE.md: name(count), in section order."""
    fam = families(data["tools"])
    order = [name for _, name in _FAMILY_PREFIXES]
    for extra in sorted(fam):
        if extra not in order:
            order.append(extra)
    fam_bits = [f"`{name}`({len(fam[name])})" for name in order if fam.get(name)]
    total = sum(len(v) for v in fam.values())
    return (f"{_FAM_BEGIN}\n"
            f"**Tool families** ({total} tools — `sys_find_tool <kw>` to search, "
            "`TOOL_MANIFEST.md` for the full list): " + " ".join(fam_bits) + f"\n{_FAM_END}")


def render_catalog(data) -> str:
    """The kinds catalog + shared-helper table for tools/CLAUDE.md - the abstraction surface an author
    must reuse rather than re-roll. Both tables are generated, so they can't drift from the code."""
    lines = [_CAT_BEGIN,
             "| Kind | References (use this — don't hand-roll a name/index) |",
             "|---|---|"]
    for k in data["kinds"]:
        # escape any '|' in the hint so it can't break the markdown table column.
        hint = (k["hint"] or k["summary"]).replace("|", "\\|")
        lines.append(f"| `{k['kind']}` | {hint} |")
    lines.append("")
    lines.append("| Helper | Provides (import from here - never re-implement) |")
    lines.append("|---|---|")
    for h in data.get("helpers", []):
        blurb = (h["blurb"] or "").replace("|", "\\|")
        lines.append(f"| `{h['module']}` | {blurb} |")
    lines.append(_CAT_END)
    return "\n".join(lines)


def _splice(path, begin, end, block, *, check=False):
    """Replace the region between begin/end markers in `path` with `block`. Returns True if already
    current. With check=True, does not write - just reports whether it would change."""
    # The already-current test compares against the file's RAW bytes: read through universal
    # newlines a CRLF file whose block is current compares equal, returns here, and keeps its CRs
    # through every regeneration. The splice itself runs on the LF-normalized text.
    with open(path, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if begin not in text or end not in text:
        raise SystemExit(f"{os.path.basename(path)} is missing the markers {begin!r}/{end!r} — add "
                         "them where the generated block should live.")
    pre, rest = text.split(begin, 1)
    _, post = rest.split(end, 1)
    new = pre + block + post
    if new == raw:
        return True
    if not check:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 if TOOL_MANIFEST.md or the CLAUDE.md map block is out of date (no write).")
    args = parser.parse_args()

    data = collect()
    rendered = render(data)
    fam_block = render_families(data)
    cat_block = render_catalog(data)

    if args.check:
        stale = []
        existing = ""
        if os.path.exists(MANIFEST_PATH):
            with open(MANIFEST_PATH, encoding="utf-8") as fh:
                existing = fh.read()
        if existing.strip() != rendered.strip():
            stale.append("tests/generated/TOOL_MANIFEST.md")
        if not _splice(CLAUDE_PATH, _FAM_BEGIN, _FAM_END, fam_block, check=True):
            stale.append("CLAUDE.md (families census)")
        if not _splice(TOOLS_CLAUDE_PATH, _CAT_BEGIN, _CAT_END, cat_block, check=True):
            stale.append("tools/CLAUDE.md (kinds+helpers catalog)")
        if stale:
            print("Stale — run `py -3 tests/gen_manifest.py` and commit: " + ", ".join(stale),
                  file=sys.stderr)
            sys.exit(1)
        print("TOOL_MANIFEST.md, the families census, and the catalog are up to date.")
        return

    with open(MANIFEST_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(rendered + "\n")
    _splice(CLAUDE_PATH, _FAM_BEGIN, _FAM_END, fam_block)
    _splice(TOOLS_CLAUDE_PATH, _CAT_BEGIN, _CAT_END, cat_block)
    print(f"Wrote {MANIFEST_PATH}, the CLAUDE.md families census, and the tools/CLAUDE.md catalog "
          f"({len(data['tools'])} tools, {len(data['kinds'])} kinds).")


if __name__ == "__main__":
    main()
