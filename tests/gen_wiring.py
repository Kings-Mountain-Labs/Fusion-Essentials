"""Generate tests/generated/TOOL_POINTER_MAP.md - the tool-to-tool pointer map, from the live registry + tool source.

Every tool speaks to the agent through three wire surfaces: its DESCRIPTION (always present, the
manual), and its runtime NOTE / ERROR strings (situational, the results). When one of those strings
names another tool, that is a BREADCRUMB - a tip steering the agent to a next step. This script reads
those references out of the code into a text diagnostic (no graphs - this is for an agent working in
the repo, not a human), so an agent developing tools can see: where breadcrumbs are missing (orphans),
where a guard is duplicated across the surface (a shared-helper candidate), and where a tip points at a
name that no longer exists (a dead reference).

It is the pointer-map counterpart to TOOL_MANIFEST (what tools exist):

    py -3 tests/gen_wiring.py          # writes tests/generated/TOOL_POINTER_MAP.md
    py -3 tests/gen_wiring.py --check  # exit 1 if TOOL_POINTER_MAP.md is stale

The reference edges are read from CODE (AST), attributed by registered handler, and split by
SURFACE (description = manual, note/error = situational tip). Static literals from handlers and one
level of local helper calls. Imported or deeper helpers and assembled messages are not exhaustively
covered. sys_capability_map is excluded from the "workflow" view - it is a catalog, not a breadcrumb.
"""

import argparse
import ast
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest  # noqa: E402
conftest.install_mock_adsk()
from conftest import load_tool, TOOLS_DIR, COMMANDS_DIR  # noqa: E402
if COMMANDS_DIR not in sys.path:
    sys.path.insert(0, COMMANDS_DIR)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
WIRING_PATH = os.path.join(TESTS_DIR, "generated", "TOOL_POINTER_MAP.md")

from gen_manifest import _FAMILY_PREFIXES, claim_name  # noqa: E402 - single source for the family map

_DOMAINS = sorted({lab for _, lab in _FAMILY_PREFIXES})
# sys_capability_map names every family's entry tool by design - a catalog, not a workflow tip.
_CATALOG = {"sys_capability_map", "sys_find_tool"}


def _family(name):
    for pre, lab in _FAMILY_PREFIXES:
        if name.startswith(pre):
            return lab
    return "other"


def _tool_modules():
    return [fn[:-3] for fn in sorted(os.listdir(TOOLS_DIR))
            if fn.endswith(".py") and not fn.startswith("_") and fn != "__init__.py"]


# ── string extraction (AST): non-docstring literals, and note/error literals specifically ───────────

def _non_doc_strings(node):
    doc_ids = set()
    for n in ast.walk(node):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(n, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_ids.add(id(body[0].value))
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids]


def _note_error_strings(node):
    """Strings the SERVER REPORTS at runtime: error(...) args, {'note'/'warning'/'readiness': X} values,
    and note=/result['note']= assignments."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "error" and n.args:
            out += _str_parts(n.args[0])
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value in ("note", "warning", "readiness", "hint"):
                    out += _str_parts(v)
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                        and t.slice.value in ("note", "warning"):
                    out += _str_parts(n.value)
                if isinstance(t, ast.Name) and t.id in ("note", "_note"):
                    out += _str_parts(n.value)
    return out


def _str_parts(node):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


# One parse per tool FILE, not per tool: _attribute runs once per registered tool (and needed the
# module twice itself), and the guard census walks every module again - about 370 redundant parses
# of 184 files, ~3s of gen_all --check. The key carries the file's stat, so a module rewritten
# under a monkeypatched TOOLS_DIR (or edited between runs in one process) is parsed again rather
# than served stale.
_parsed = {}


def _parse_module(mod_name):
    """(functions, top-level non-docstring strings, tool -> handler map) for one tool module."""
    path = os.path.join(TOOLS_DIR, mod_name + ".py")
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _parsed.get(key)
    if hit is None:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fns = {node.name: node for node in tree.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        top_desc = [s for node in tree.body
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    for s in _non_doc_strings(node)]
        hit = _parsed[key] = (fns, top_desc, _tool_handler_map(tree))
    return hit


def _module_functions(mod_name):
    fns, top_desc, _handlers = _parse_module(mod_name)
    return fns, top_desc


def _tool_handler_map(tree):
    """tool name -> handler function name, resolved through the registration call-sites
    (V = Tool.create_*(name="X")... then Item.create_tool_item(tool=V, handler=H)) - the
    unambiguous seam, so sibling tools sharing a name stem never swap attributed notes."""
    var_to_tool = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        for call in ast.walk(node.value):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr in ("create_simple", "create_with_string_input")):
                for kw in call.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        var_to_tool[node.targets[0].id] = kw.value.value
    mapping = {}
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == "create_tool_item"):
            continue
        tool_var = handler_name = None
        for kw in call.keywords:
            if kw.arg == "tool" and isinstance(kw.value, ast.Name):
                tool_var = kw.value.id
            if kw.arg == "handler" and isinstance(kw.value, ast.Name):
                handler_name = kw.value.id
        if tool_var in var_to_tool and handler_name:
            mapping[var_to_tool[tool_var]] = handler_name
    return mapping


def _called_local_names(node, fns):
    """The same-module function names `node`'s body CALLS, in first-call order (deduplicated).

    Matches a bare `helper(...)` and an attribute call whose final name is a module function
    (`_slice_geometry(...)`, `mod._slice_geometry(...)`); a call to the enclosing function itself is
    excluded so a recursive handler is not walked into.
    """
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
        if name in fns and name != node.name:
            out.append(name)
    return list(dict.fromkeys(out))


def _attribute(mod_name, tool_name):
    """(note_string_LIST, extra_desc_strings) belonging to a tool - its REGISTERED handler's
    notes (resolved by call-site, see _tool_handler_map) plus module-level DESC constants.
    Falls back to whole-stem name matching for an unusual registration shape."""
    fns, top_desc, handlers = _parse_module(mod_name)
    handler = handlers.get(tool_name)
    note = []
    if handler and handler in fns:
        node = fns[handler]
        note += _note_error_strings(node)
        # ONE call hop into same-module helpers: a rich read's handler is a router, and the notes it
        # can actually return are built in the _slice_* helpers it dispatches to. Harvesting only the
        # handler body reports those tools as having no guidance surface at all. The hop stops at one
        # level (a helper's own callees are NOT followed) so a shared low-level utility called deep in
        # the chain does not get its strings attributed to every tool above it.
        for callee in _called_local_names(node, fns):
            note += _note_error_strings(fns[callee])
    else:
        stem = tool_name.split("_", 1)[1] if "_" in tool_name else tool_name
        for fn_name, node in fns.items():
            if stem in fn_name.lower():
                note += _note_error_strings(node)
    return note, "\n".join(top_desc)


# Smell heuristics for the guidance audit. Each maps a regex to a one-word tag surfaced next to the
# string so a reviewer can scan for rot instead of reading 1000 lines.
_SMELLS = [
    ("war-story", re.compile(r"\b(used to|previously|historically|once advised|copy-pasted|epidemic|"
                             r"war stor|legacy behaviou?r|we (?:used|had)|the old|before fix|"
                             r"after fix)\b", re.I)),
    ("cause-guess", re.compile(r"\b(almost always|probably|might be|likely (?:owned|because)|"
                               r"is likely|presumably|i think|we think|seems)\b", re.I)),
    ("hedge", re.compile(r"\b(should (?:probably|maybe)|may or may not|not sure|possibly)\b", re.I)),
    ("process-note", re.compile(r"\b(revisit this|for now|todo|fixme|in one session|going red|"
                                r"work order)\b", re.I)),
]


def _smells(text):
    return sorted({tag for tag, rx in _SMELLS if rx.search(text)})


# ── collect: per-tool {desc, note_text, family, readonly} + reference edges by surface ──────────────

def collect(registry=None):
    """Collect wiring data without replacing the process registry singleton."""
    if registry is not None:
        return _collect_unguarded(registry)
    from mcpServer.mcp_primitives import registry as shared_registry
    saved_registry = shared_registry._registry_instance
    try:
        return _collect_unguarded(shared_registry)
    finally:
        shared_registry._registry_instance = saved_registry


def _collect_unguarded(registry):
    """Collect wiring data through the supplied registry module or test seam."""
    records = {}
    owner = {}
    input_names = set()
    for mod_name in _tool_modules():
        mod = load_tool(mod_name)
        rt = getattr(mod, "register_tool", None)
        if not callable(rt):
            continue
        registry.reset_registry()
        rt()
        for item in registry.get_tools():
            name = item.get_name()
            # A second module registering this name would overwrite records[name] here, so the map
            # would describe one tool while the server serves the other. Refuse, naming both files.
            claim_name(owner, name, mod_name)
            d = item.to_dict()
            ann = d.get("annotations") or {}
            props = list((d.get("inputSchema") or {}).get("properties", {}).keys())
            input_names.update(props)
            note_list, top_desc = _attribute(mod_name, name)
            records[name] = {
                "module": mod_name, "family": _family(name),
                "readonly": bool(ann.get("readOnlyHint", False)),
                "description": (d.get("description") or ""),
                "desc": (d.get("description") or "") + "\n" + top_desc,
                "note": "\n".join(note_list),
                "note_list": [s.strip() for s in note_list if len(s.strip()) > 20],
                "inputs": props,
            }
    names = set(records)

    def refs(text, self_name):
        hits = set()
        for b in names:
            if b == self_name or b in _CATALOG:
                continue
            if re.search(r"(?<![\w])" + re.escape(b) + r"(?![\w])", text):
                hits.add(b)
        return hits

    # edges by surface
    desc_edges, note_edges = {}, {}
    for n, r in records.items():
        desc_edges[n] = refs(r["desc"], n)
        note_edges[n] = refs(r["note"], n)

    # dead references (a domain_verb call token, not a real tool, not an input param)
    ghost_re = re.compile(r"\b((?:" + "|".join(_DOMAINS) + r")_[a-z][a-z_]*)\(")
    ghosts = defaultdict(set)
    for n, r in records.items():
        for tok in ghost_re.findall(r["desc"] + "\n" + r["note"]):
            if tok not in names and tok not in input_names:
                ghosts[tok].add(n)

    # duplicated guard strings (a note/error literal appearing verbatim in many tools = shared-helper
    # candidate). Count exact note/error strings across the surface.
    guard_counts = Counter()
    guard_where = defaultdict(set)
    for mod_name in {r["module"] for r in records.values()}:
        fns, _ = _module_functions(mod_name)
        for node in fns.values():
            for s in _note_error_strings(node):
                s2 = s.strip()
                if len(s2) > 25:                      # ignore trivial fragments
                    guard_counts[s2] += 1
                    guard_where[s2].add(mod_name)

    return {"records": records, "desc_edges": desc_edges, "note_edges": note_edges,
            "ghosts": dict(ghosts), "guards": guard_counts, "guard_where": guard_where}


# ── render ──────────────────────────────────────────────────────────────────────────────────────────

def _indeg(edges):
    d = Counter()
    for outs in edges.values():
        for dst in outs:
            d[dst] += 1
    return d


def render(data):
    records, desc_e, note_e = data["records"], data["desc_edges"], data["note_edges"]
    ghosts, guards, gwhere = data["ghosts"], data["guards"], data["guard_where"]
    desc_in, note_in = _indeg(desc_e), _indeg(note_e)
    combined_in = Counter()
    for n in records:
        combined_in[n] = desc_in[n] + note_in[n]

    L = [
        "# Tool pointer map (generated)",
        "",
        "_Auto-generated from the tool source by `tests/gen_wiring.py`. Do not edit by hand._ For an",
        "agent DEVELOPING tools in this repo, to diagnose the surface agents CONSUMING these tools",
        "navigate by: where each tool's text (its **description** = the manual, its runtime **note/error**",
        "= the situational tip) names ANOTHER tool. Act on the Blindspots below - fix dead references,",
        "close orphans, factor duplicated guards into shared helpers.",
        "",
        f"**Tools:** {len(records)}  |  **description breadcrumbs:** {sum(len(v) for v in desc_e.values())}"
        f"  |  **note/error breadcrumbs:** {sum(len(v) for v in note_e.values())}",
        "",
        "## Blindspots to engineer",
        "",
        "### Dead references (a detected literal tip names something that is not a tool - FIX THESE)",
    ]
    if ghosts:
        for tok in sorted(ghosts):
            L.append(f"- `{tok}(` named by: {', '.join(sorted(ghosts[tok]))}")
    else:
        L.append("- none detected in the scanned literals.")

    # orphans: no breadcrumb (desc OR note) leads here
    any_in = {n for n in records if combined_in[n] > 0}
    orphans = sorted(n for n in records if n not in any_in and n not in _CATALOG)
    read_orph = [n for n in orphans if records[n]["readonly"]]
    edit_orph = [n for n in orphans if not records[n]["readonly"]]
    L += ["",
          "### Orphans (no incoming breadcrumb detected in this map)",
          f"**Read/Acquire ({len(read_orph)})** - higher concern, a check-your-work tool nothing points to:",
          "  " + (", ".join(f"`{n}`" for n in read_orph) or "(none)"),
          f"\n**Edit ({len(edit_orph)})** - usually leaf actions, scan for genuine gaps:",
          "  " + (", ".join(f"`{n}`" for n in edit_orph) or "(none)")]

    # duplicated guards - shared-helper candidates. Sort by (-count, string) for a STABLE order (ties
    # in count must not reorder run-to-run, or --check reports false staleness).
    dupes = sorted(((s, c) for s, c in guards.items() if c >= 4), key=lambda sc: (-sc[1], sc[0]))
    L += ["", "### Duplicated guard strings (>=4 copies = factor into a shared _common helper)"]
    if dupes:
        for s, c in dupes:
            short = (s[:88] + "...") if len(s) > 88 else s
            L.append(f"- **{c}x** across {len(gwhere[s])} module(s): \"{short}\"")
    else:
        L.append("- none over the threshold.")

    # hubs - stable order: by (-count, name) so equal-count hubs don't reorder run-to-run.
    L += ["", "### Hubs (most breadcrumbs lead here - the connective tissue)"]
    for n in sorted(records, key=lambda x: (-combined_in[x], x))[:12]:
        L.append(f"- `{n}`  <- {combined_in[n]}  (desc {desc_in[n]}, note {note_in[n]})")

    # ── the full guidance surface, legible in one place ────────────────────────────────────────────
    smell_total = 0
    audit = ["", "## The detected guidance surface", "",
             "Static runtime **note/warning** literals per tool, attributed from each registered handler and one",
             "level of local helper calls. Imported or deeper helpers and assembled messages are not",
             "exhaustively covered. Use this surface to judge consistency and best-practice guidance.",
             "Smells are auto-tagged: `war-story` (narrates history), `cause-guess` (asserts an",
             "unverified cause), `hedge` (waffles). (Pure error-validation strings - 'must be a number' -",
             "are omitted; this is the GUIDANCE layer, not input validation.)", ""]
    for n in sorted(records):
        notes = records[n]["note_list"]
        if not notes:
            continue
        # de-dup identical strings within a tool, keep order
        seen, uniq = set(), []
        for s in notes:
            if s not in seen:
                seen.add(s)
                uniq.append(s)
        tagged = [(s, _smells(s)) for s in uniq]
        if not any(t for _, t in tagged) and all(len(s) < 25 for s, _ in tagged):
            continue
        audit.append(f"### `{n}`")
        for s, tags in tagged:
            flag = ("  " + " ".join(f"**[{t}]**" for t in tags)) if tags else ""
            smell_total += len(tags)
            short = s if len(s) <= 200 else s[:197] + "..."
            audit.append(f"- {short}{flag}")
        audit.append("")
    # headline the smell count up top
    L[9] = L[9] + f"  |  **guidance smells flagged:** {smell_total}"
    L += audit

    return "\n".join(L)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 if tests/generated/TOOL_POINTER_MAP.md is out of date (does not write).")
    args = parser.parse_args()
    rendered = render(collect())
    if args.check:
        existing = open(WIRING_PATH, encoding="utf-8").read() if os.path.exists(WIRING_PATH) else ""
        if existing.strip() != rendered.strip():
            print("TOOL_POINTER_MAP.md is stale - run `py -3 tests/gen_wiring.py` and commit.", file=sys.stderr)
            sys.exit(1)
        print("TOOL_POINTER_MAP.md is up to date.")
        return
    with open(WIRING_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(rendered + "\n")
    print(f"Wrote {WIRING_PATH}")


if __name__ == "__main__":
    main()
