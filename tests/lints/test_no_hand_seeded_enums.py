# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: a MEASURED adsk enum member is never hand-assigned in a unit test - it comes seeded.

A line under tests/unit that installs a live_api_facts.ENUMS member by attribute, dict key, kwarg,
a setattr loop over the member names, or a setattr/monkeypatch.setattr of one member - named
either as the second argument or as the tail of a dotted target string - fails;
conftest seeds those onto the mock adsk modules.
_ALLOWLIST files are exempt."""

import ast
import os
import re

import _corpus
import live_api_facts

UNIT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "unit")

_MEMBERS = sorted({m for members in live_api_facts.ENUMS.values() for m in members})
_MEMBER_SET = frozenset(_MEMBERS)
_NAMES = "|".join(map(re.escape, _MEMBERS))
# Three re-seeding shapes - attribute install, dict literal, kwarg - each compiled SEPARATELY: a
# single alternation this large can silently fail to match its tail. The kwarg form's lookbehind
# keeps an attribute install from double-counting and a longer identifier from matching a suffix.
_PATTERNS = (
    re.compile(r"\.(?:" + _NAMES + r")\s*=(?!=)"),
    re.compile(r"[\"'](?:" + _NAMES + r")[\"']\s*:"),
    re.compile(r"(?<![.\w\"'])(?:" + _NAMES + r")\s*=(?!=)"),
)

# file -> why its sentinel installer is tolerated. Shrink-only.
_ALLOWLIST = {}


def _is_setattr(func):
    """True for a `setattr(...)` or `<anything>.setattr(...)` callee - monkeypatch's included."""
    return ((isinstance(func, ast.Name) and func.id == "setattr")
            or (isinstance(func, ast.Attribute) and func.attr == "setattr"))


def _setattr_seeded_linenos(path):
    """Lines where a setattr installs a measured member: a for-loop over a literal tuple/list of
    member NAMES re-seeding a whole family at once, the single site handing one quoted member
    name straight to setattr/monkeypatch.setattr, and monkeypatch's dotted-target form, where the
    member is the tail of the first argument's string. The name sits beside neither an `=` nor a
    `:` in any of those shapes, so the three patterns above cannot see it."""
    hits = set()
    for node in ast.walk(_corpus.tree(path)):
        if isinstance(node, ast.Call) and _is_setattr(node.func) and len(node.args) >= 2:
            named = node.args[1]
            if isinstance(named, ast.Constant) and named.value in _MEMBER_SET:
                hits.add(named.lineno)
            target = node.args[0]
            if isinstance(target, ast.Constant) and isinstance(target.value, str) \
                    and target.value.rsplit(".", 1)[-1] in _MEMBER_SET:
                hits.add(target.lineno)
        if not (isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List))):
            continue
        if any(isinstance(n, ast.Call) and _is_setattr(n.func)
               for stmt in node.body for n in ast.walk(stmt)):
            hits |= {e.lineno for e in ast.walk(node.iter)
                     if isinstance(e, ast.Constant) and e.value in _MEMBER_SET}
    return hits


def _offending_lines(path):
    src = _corpus.text(path)
    lines = src.split("\n")
    out = {}
    # Whole-file screen first: no pattern is line-anchored and the one lookbehind admits the "\n"
    # at a line start, so a line match is always a full-text match too.
    if any(p.search(src) for p in _PATTERNS):
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue
            if any(p.search(line) for p in _PATTERNS):
                out[i] = line.strip()
    for i in _setattr_seeded_linenos(path):
        out[i] = lines[i - 1].strip()
    return sorted(out.items())


class TestNoHandSeededEnums:
    def test_measured_enum_members_are_never_hand_assigned(self):
        offenders = []
        for fn in sorted(os.listdir(UNIT_DIR)):
            if not (fn.startswith("test_") and fn.endswith(".py")) or fn in _ALLOWLIST:
                continue
            for i, text in _offending_lines(os.path.join(UNIT_DIR, fn)):
                offenders.append(f"{fn}:{i}: {text}")
        assert not offenders, (
            "A measured enum member is hand-assigned - these values come SEEDED from the generated "
            "live_api_facts.py (conftest wires them onto the mock adsk modules). Delete the "
            "assignment; if the value you need is not measured yet, add a measurement row to "
            "tests/live/measure_api.py and regenerate:\n  " + "\n  ".join(offenders))

    def test_allowlist_files_still_trip(self):
        stale = []
        for fn, reason in _ALLOWLIST.items():
            assert reason.strip(), f"{fn} allowlist entry needs a plain-English reason"
            path = os.path.join(UNIT_DIR, fn)
            if not os.path.exists(path):
                stale.append(f"{fn}: no such file")
            elif not _offending_lines(path):
                stale.append(f"{fn}: no hand-assignment remains - remove the allowlist entry")
        assert not stale, "stale allowlist entries:\n  " + "\n  ".join(stale)

