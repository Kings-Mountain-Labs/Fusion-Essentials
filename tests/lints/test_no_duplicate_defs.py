# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: no module under commands/mcpServer/ or tests/ defines the same function/class name twice in
one scope - neither at module scope nor inside one class body. Python keeps only the later binding,
so the earlier def becomes unreachable dead code with no import-time warning; in a class body that
means a `test_*` method that no longer runs while the suite still reads green.
"""

import ast
from pathlib import Path

import _corpus
from conftest import TOOLS_DIR

MCP_ROOT = Path(TOOLS_DIR).parent          # commands/mcpServer
REPO = MCP_ROOT.parent.parent              # repo root
TESTS_ROOT = REPO / "tests"                # the harness: conftest fakes, unit tests, lints
SCANNED_ROOTS = (MCP_ROOT, TESTS_ROOT)

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_ACCESSOR_ATTRS = frozenset({"setter", "getter", "deleter"})


def _py_files():
    return [p for root in SCANNED_ROOTS for p in sorted(_corpus.py_files(root))]


def _duplicate_top_level_defs(path):
    """[(name, [linenos])] for every top-level function/class name this module defines more than
    once. Scans `tree.body` directly: a def inside a function body is a closure, not a shadow."""
    by_name = {}
    for node in _corpus.tree(path).body:
        if isinstance(node, _DEFS):
            by_name.setdefault(node.name, []).append(node.lineno)
    return [(name, linenos) for name, linenos in by_name.items() if len(linenos) > 1]


def _accessor_kind(node):
    """Which accessor a definition declares itself to be - 'setter'/'getter'/'deleter' when it is
    decorated `@<its OWN name>.<kind>`, else None. The decorator's base must be the definition's own
    name: a pasted `@other.setter` above a repeated name rebinds nothing, so it is still a shadow."""
    for dec in node.decorator_list:
        if (isinstance(dec, ast.Attribute) and dec.attr in _ACCESSOR_ATTRS
                and isinstance(dec.value, ast.Name) and dec.value.id == node.name):
            return dec.attr
    return None


def _is_property_pair(nodes):
    """True for a group of same-named defs that is one property and its accessors - a repeated name
    by design. Both conditions are load-bearing: EXACTLY ONE member is not an accessor (that one is
    the getter, so two plain defs of a name is the shadow itself, and a getterless run of accessors
    is too), and no accessor KIND repeats (a second `@value.setter` replaces the first)."""
    kinds = [_accessor_kind(n) for n in nodes]
    return kinds.count(None) == 1 and len(set(kinds)) == len(kinds)


def _duplicate_class_body_defs(path):
    """[(class_name, name, [linenos])] for every name a single class body defines more than once.
    Walks EVERY class in the module (nested ones included) but compares names only within one
    class's own body, so the same method name in two classes is not a hit."""
    hits = []
    for cls in ast.walk(_corpus.tree(path)):
        if not isinstance(cls, ast.ClassDef):
            continue
        by_name = {}
        for node in cls.body:
            if isinstance(node, _DEFS):
                by_name.setdefault(node.name, []).append(node)
        for name, nodes in by_name.items():
            if len(nodes) > 1 and not _is_property_pair(nodes):
                hits.append((cls.name, name, [n.lineno for n in nodes]))
    return hits


def _offenders(paths, root):
    """One rendered failure line per shadowed name, naming the file, the scope and every lineno."""
    out = []
    for path in paths:
        rel = path.relative_to(root)
        for name, linenos in _duplicate_top_level_defs(path):
            lines = ", ".join(str(n) for n in linenos)
            out.append(f"{rel}: '{name}' defined {len(linenos)}x at module scope (lines {lines})")
        for cls_name, name, linenos in _duplicate_class_body_defs(path):
            lines = ", ".join(str(n) for n in linenos)
            out.append(f"{rel}: '{name}' defined {len(linenos)}x in class {cls_name} "
                       f"(lines {lines})")
    return out


class TestNoDuplicateDefs:
    def test_no_module_shadows_a_def(self):
        offenders = _offenders(_py_files(), REPO)
        assert not offenders, (
            "A def/class name is defined more than once in one scope - Python keeps only the LAST "
            "binding, so every earlier definition silently becomes unreachable dead code (no "
            "import-time warning). In a class body that means a `test_*` method that no longer "
            "runs while the suite reads green. Rename one of them, or delete the stale "
            "duplicate:\n  " + "\n  ".join(offenders))

    def test_the_scan_reaches_both_trees(self):
        # The detectors are only as good as the glob feeding them: with tests/ out of scope, a
        # shadowed helper in a test file passes this lint green. Pin both roots by naming a file
        # that must be in the scan - this file itself for tests/, _common.py for the tools tree.
        scanned = {p.resolve() for p in _py_files()}
        for probe in (Path(__file__), Path(TOOLS_DIR) / "_common.py"):
            assert probe.resolve() in scanned, (
                f"{probe.name} is not in the scanned set - a root dropped out of the glob, so "
                "duplicate defs under it are no longer detected")
