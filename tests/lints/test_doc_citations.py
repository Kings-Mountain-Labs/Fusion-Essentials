# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: every file a doc or comment cites exists, every tool name a doc cites is registered, and
every symbol a dotted citation points at is defined by the module it names.
A file resolves by BASENAME anywhere under the repo; a tool resolves against the LIVE registry; a
`<module>.<symbol>` resolves against that module's AST, and an unknown module half is skipped."""

import ast
import io
import re
import tokenize
from collections import Counter
from functools import lru_cache
from pathlib import Path

import _corpus
from conftest import register_all_tools

REPO = Path(__file__).resolve().parent.parent.parent
MCP = REPO / "commands" / "mcpServer"

# The prose surface every rule here polices - one list, so the scanned set cannot drift between them.
CONSTITUTION_DOCS = [
    REPO / "CLAUDE.md",
    REPO / "CONTRIBUTING.md",
    MCP / "README.md",
    MCP / "tools" / "CLAUDE.md",
    REPO / "tests" / "CLAUDE.md",
    REPO / "tests" / "README.md",
]
# a file citation: an optional path prefix then a basename ending in .py, .md, or .log (a probe
# log is EVIDENCE - a comment citing one that was never committed is an unbacked measurement
# claim, the exact rot this lint exists for).
_FILE = re.compile(r"[\w./\\-]*[\w-]+\.(?:py|md|log)\b")
_INLINE_CODE = re.compile(r"`([^`]+)`")


# Log files the RUNNING add-in writes (they exist at runtime, never in the repo) - citing one is
# a pointer to live output, not to committed evidence.
_RUNTIME_LOGS = {"futil.log", "app.log"}


@lru_cache(maxsize=None)
def _present():
    # basename -> how many files carry it, over the whole repo. .claude/worktrees/ holds agent
    # checkouts of this same repo, so counting them would answer "that file exists" for a file
    # deleted here and still present in an abandoned worktree.
    counts = Counter()
    worktrees = REPO / ".claude" / "worktrees"
    for ext in ("*.py", "*.md", "*.log"):
        counts.update(p.name for p in REPO.rglob(ext) if worktrees not in p.parents)
    counts.update(_RUNTIME_LOGS)
    return counts


MEASURE_ROWS = REPO / "tests" / "live" / "measure_api.py"


def _scanned_files():
    # the constitution docs PLUS the server source PLUS the measurement registry - a docstring,
    # comment or measurement row that cites a file rots the same way a doc does when the file is
    # renamed/moved.
    files = [d for d in CONSTITUTION_DOCS if d.exists()]
    files += sorted(MCP.rglob("*.py"))
    files.append(MEASURE_ROWS)
    return files


_ROW_PROSE_KEYS = {"claim", "encoded_in"}


def _row_prose(src):
    """The claim and encoded_in text of every measurement row: where a row says which fake and
    which tool lean on it. The row bodies are Fusion-side scripts and are not prose."""
    chunks = []
    for node in ast.walk(_corpus.tree(src)):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value in _ROW_PROSE_KEYS
                    and isinstance(value, ast.Constant) and isinstance(value.value, str)):
                chunks.append(value.value)
    return "\n".join(chunks)


def _basename(cite):
    return cite.replace("\\", "/").split("/")[-1]


def _inline_code(doc):
    """Every inline-`code` span in one doc, joined - what a tool citation is written in."""
    return " ".join(m.group(1) for m in _INLINE_CODE.finditer(_corpus.text(doc)))


def _citable_text(src):
    """What a scanned file says in its own voice: a doc or source module entire, the measurement
    registry only through its rows' claim and encoded_in text."""
    return _row_prose(src) if src == MEASURE_ROWS else _corpus.text(src)


class TestDocCitations:
    def test_cited_files_exist(self):
        present = _present()
        offenders = []
        for src in _scanned_files():
            text = _citable_text(src)
            for cite in sorted(set(_FILE.findall(text))):
                if _basename(cite) not in present:
                    offenders.append(f"{src.relative_to(REPO)} cites '{cite}' - no file named "
                                     f"'{_basename(cite)}' exists")
        assert not offenders, (
            "A doc or source comment cites a file that doesn't exist (renamed/removed? cite the "
            "current name):\n  " + "\n  ".join(offenders))


# A token counts as a tool citation when it is snake_case AND either its first segment is a known
# tool family or it is a converter shaped X_to_Y.

_SNAKE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")

# tool-shaped tokens a doc names on purpose that are NOT tools (a helper function, or an action= value
# of an action-dispatched tool). Shrink-only; each needs a reason.
_NOT_A_TOOL = {
    "find_setup": "a _cam_common helper that resolves a CAM setup by exact name, not a tool",
    "find_operation": "a _cam_common helper that resolves a CAM operation by exact name, not a tool",
}


def _registered_names():
    return {it.to_dict().get("name") for it in register_all_tools()}


def _module_basenames():
    # a helper module (_cam_common, _data_read) is family-shaped but is a FILE a doc legitimately
    # names, not a dangling tool - never flag one.
    return {p.stem for p in MCP.rglob("*.py")}


def _is_tool_citation(tok, families):
    return tok.split("_", 1)[0] in families or "_to_" in tok


class TestToolCitations:
    def test_cited_tool_names_are_registered(self):
        registered = _registered_names()
        families = {n.split("_", 1)[0] for n in registered}
        stems = _module_basenames()
        # helper modules are cited WITH their leading underscore (`_cam_common`) but tokenize without
        # it, so admit the stripped form too; a truly stale `data_read.py` is owned by the file lint.
        known = registered | stems | {s.lstrip("_") for s in stems}
        offenders = []
        for doc in CONSTITUTION_DOCS:
            if not doc.exists():
                continue
            for tok in sorted(set(_SNAKE.findall(_inline_code(doc)))):
                if tok in known or tok in _NOT_A_TOOL:
                    continue
                if _is_tool_citation(tok, families):
                    offenders.append(f"{doc.relative_to(REPO)} cites `{tok}` - not a registered tool")
        assert not offenders, (
            "A doc cites a tool name no registered tool answers to (rename to the current tool, or add "
            "a genuine helper/action to _NOT_A_TOOL with a reason):\n  " + "\n  ".join(offenders))

    def test_not_a_tool_entries_still_cited_and_still_not_tools(self):
        # both staleness directions for the exemption table: an entry that became a real tool now
        # SHADOWS the check; an entry no doc cites anymore is dead weight. Either way, drop it.
        registered = _registered_names()
        cited = set()
        for doc in CONSTITUTION_DOCS:
            if doc.exists():
                cited |= set(_SNAKE.findall(_inline_code(doc)))
        stale = []
        for tok, reason in _NOT_A_TOOL.items():
            assert reason.strip(), f"_NOT_A_TOOL: {tok} needs a plain-English reason"
            if tok in registered:
                stale.append(f"{tok}: now a registered tool - drop the entry")
            elif tok not in cited:
                stale.append(f"{tok}: no doc cites it anymore - drop the entry")
        assert not stale, "stale _NOT_A_TOOL entries:\n  " + "\n  ".join(stale)


# A citation is `<module>.<symbol>` judged on its first two segments. The head must OPEN the dotted
# path (a tail like the `version` of `Milestone.version.versionNumber` is a claim about a class) and
# must be spelled as the module is - `_cam_common`, not `cam_common` - so an API call on a `joints`
# collection is not read as a citation of _joints.py.

_DOTTED = re.compile(r"(?<![\w.])(_?[a-z][a-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")
# tails rule 1 owns: `_inputs.py` is a FILE citation, checked there and skipped here.
_FILE_TAILS = frozenset({"py", "md", "log"})

# a dotted token in prose that is NOT a module reference: an instance whose local name matches a
# module basename, or a tool's INPUT. Shrink-only; each needs a reason.
_NOT_A_MODULE_SYMBOL = {
    "tool.add_input_property": "a Tool INSTANCE's wiring method - tool.py holds the class the "
                               "method is on, and every tool builds through an instance of it",
    "item.primitive": "a field of the registry Item a dispatch is handling, not of item.py",
}


@lru_cache(maxsize=None)
def _prose(src):
    """The hand-written prose of one scanned file: a doc entire, a measurement row's claim and
    encoded_in, a source module's comments and docstrings (see the module docstring for what the
    rest of a module would cost)."""
    if src.suffix != ".py":
        return _corpus.text(src)
    if src == MEASURE_ROWS:
        return _row_prose(src)
    chunks = [tok.string
              for tok in tokenize.generate_tokens(io.StringIO(_corpus.text(src)).readline)
              if tok.type == tokenize.COMMENT]
    for node in ast.walk(_corpus.tree(src)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                chunks.append(doc)
    return "\n".join(chunks)


@lru_cache(maxsize=None)
def _modules():
    """module basename -> the modules answering to it, over the two trees a citation points into.
    Two files can share a stem, so a symbol defined in ANY of them answers the citation. The shared
    fakes answer to `conftest`, which re-exports them: their own stems are the words prose spells an
    API instance with (`design.rootComponent`, `sketch.profiles`), so keying them by stem would read
    every such sentence as a citation of a fake module."""
    mods = {}
    fakes = REPO / "tests" / "fakes"
    for root in (MCP, REPO / "tests"):
        for path in _corpus.py_files(root):
            stem = "conftest" if fakes in path.parents else path.stem
            mods.setdefault(stem, []).append(path)
    return {stem: tuple(paths) for stem, paths in mods.items()}


@lru_cache(maxsize=None)
def _top_level(mod):
    """Every name a module binds at module level: a def/class name, an assignment target, or an
    import binding (`from . import _common` makes `_common` an attribute of the importer too)."""
    names = set()
    for node in _corpus.tree(mod).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            for target in targets:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    return frozenset(names)


@lru_cache(maxsize=None)
def _symbol_citations():
    """Every (file, module, symbol) the scanned prose cites, deduplicated per file."""
    found = []
    for src in _scanned_files():
        for mod, sym in sorted(set(_DOTTED.findall(_prose(src)))):
            if sym not in _FILE_TAILS:
                found.append((src, mod, sym))
    return tuple(found)


class TestSymbolCitations:
    def test_cited_symbols_are_defined(self):
        offenders = []
        for src, mod, sym in _symbol_citations():
            paths = _modules().get(mod)
            if not paths or f"{mod}.{sym}" in _NOT_A_MODULE_SYMBOL:
                continue
            if not any(sym in _top_level(path) for path in paths):
                offenders.append(f"{src.relative_to(REPO)} cites '{mod}.{sym}' - {mod} defines no "
                                 f"'{sym}' at module level")
        assert not offenders, (
            "A doc or source comment points into a module at a symbol it doesn't define "
            "(renamed/removed? cite the current name, or name a genuine non-module reference in "
            "_NOT_A_MODULE_SYMBOL with a reason):\n  " + "\n  ".join(offenders))

    def test_the_symbol_surface_still_collects_every_half(self):
        # A citation that is never COLLECTED, or whose module never RESOLVES, is skipped rather
        # than flagged - so each half can be narrowed away with nothing to report and the rule
        # above still green. Three pins over the real corpus, one per way that happens.
        mods = _modules()
        judged = [(src, mod, sym) for src, mod, sym in _symbol_citations() if mod in mods]
        in_source = {(mod, sym) for src, mod, sym in judged if src not in CONSTITUTION_DOCS}
        doc_only = {(mod, sym) for src, mod, sym in judged if src in CONSTITUTION_DOCS} - in_source
        assert doc_only, (
            "no module citation is collected from the constitution docs alone - the docs dropped "
            "out of the SYMBOL surface (_scanned_files/_symbol_citations) and their citations are "
            "now unchecked")
        underscored = {(mod, sym) for _, mod, sym in judged if mod.startswith("_")}
        assert underscored, (
            "no citation of an underscore-spelled module is collected - _DOTTED's head group no "
            "longer opens on a leading underscore (`_?[a-z]...`), so `_inputs.resolve_inputs` and "
            "every pointer like it left the SYMBOL surface")
        tests_tree = REPO / "tests"
        checked = {(mod, sym) for _, mod, sym in judged
                   if all(tests_tree in path.parents for path in mods[mod])
                   and any(sym in _top_level(path) for path in mods[mod])}
        assert checked, (
            "no cited symbol resolves through a module under tests/ - the tests tree dropped out "
            "of _modules(), so a citation into conftest.py or live_api_facts.py is now skipped as "
            "'no module of ours' instead of checked")

    def test_not_a_module_symbol_entries_still_cited_and_still_dangle(self):
        # both staleness directions, as for _NOT_A_TOOL: an entry whose module grew that symbol now
        # SHADOWS a real check; an entry no prose cites anymore is dead weight.
        cited = {f"{mod}.{sym}" for _, mod, sym in _symbol_citations()}
        stale = []
        for tok, reason in _NOT_A_MODULE_SYMBOL.items():
            assert reason.strip(), f"_NOT_A_MODULE_SYMBOL: {tok} needs a plain-English reason"
            mod, sym = tok.split(".", 1)
            if any(sym in _top_level(path) for path in _modules().get(mod, ())):
                stale.append(f"{tok}: {mod} defines '{sym}' at module level - drop the entry")
            elif tok not in cited:
                stale.append(f"{tok}: no prose cites it anymore - drop the entry")
        assert not stale, "stale _NOT_A_MODULE_SYMBOL entries:\n  " + "\n  ".join(stale)
