"""Lint: the codebase is EVERGREEN (tools/CLAUDE.md, "Module docstrings").
No comment/docstring/string narrates its own history ("used to", "previously"), admits the code is
not final ("for now", a TODO marker), or points at a planning document (a work-item label, a plan
phase, a dated observation). A legitimate domain use takes an _ALLOWLIST entry with a reason."""

import os
import re

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tests/lints/ -> tests/
REPO_ROOT = os.path.dirname(TESTS_DIR)
_SWEPT_DIRS = (
    os.path.join(REPO_ROOT, "commands", "mcpServer"),
    os.path.join(REPO_ROOT, "tests"),
)

# This file quotes every denylisted phrase as data, and gen_wiring.py carries an identical list as
# ITS OWN smell-detection pattern - both would trip on their own pattern list. TOOL_MANIFEST.md /
# TOOL_POINTER_MAP.md are GENERATED digests of the source already swept here.
_EXCLUDED_FILES = {"test_evergreen_no_baggage.py", "gen_wiring.py", "TOOL_MANIFEST.md",
                   "TOOL_POINTER_MAP.md"}

# Word-boundary wrapped, matched case-insensitively: "the fix" does not match inside "the fixture",
# and "previously" does not match inside "was_previously_saved" (underscore is a word character).
_PHRASE_NAMES = (
    # history narrative - describes a past state instead of the current one
    "used to", "previously", "the old", "until now", "renamed from",
    # deferral - code admitting it is not its final form points at a plan
    "for now", "revisit this", "tech debt",
    # plan artifacts by name - code never points into the planning tree
    "work order", "backlog",
)
_PHRASES = [(p, re.compile(r"\b" + re.escape(p) + r"\b")) for p in _PHRASE_NAMES]
_WO_DIGIT = re.compile(r"\bWO-\d")
# A bare work-item label like "C7:"/"P0.1:" opening a comment - the letter + item-number shorthand
# a planning doc uses, meaningless once that doc is gone. Anchored to the comment OPENING with a
# colon so prose like "# P40 is the fleet percentile" never collides.
_ITEM_LABEL = re.compile(r"#\s*[A-Z][0-9]{1,2}(?:\.[0-9]{1,2})?\s*:")
# Classic deferral markers. Case-SENSITIVE: the uppercase marker is the convention; a lowercase
# "todo" can be ordinary prose (tool_verify's "the honest 'todo' ledger").
_TODO_MARKER = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
_PHASE_LABEL = re.compile(r"\bPhase [0-9]\b")
# A calendar date is an observation diary's timestamp. Files whose dates are DATA - generated
# verification stamps, or fixtures mimicking a dated wire format - are exempted by name, and the
# MCP protocol version ids (dates by construction, pinned by the spec) are stripped before the scan.
_DATE = re.compile(r"\b20\d{2}-[01]\d(?:-[0-3]\d)?\b")
_DATE_OK_TOKENS = ("2025-03-26", "2025-06-18")   # MCP protocol version ids (spec-pinned literals)
_DATE_EXEMPT_FILES = {
    "live_api_facts.py",           # generated: VERIFIED_ON stamp the check_all gate compares
    "VERIFIED_API_FACTS.md",       # generated verification receipt - the stamp is its function
    "VERIFIED_TOOLS.md",           # generated verification receipt - the stamp is its function
    "test_tool_verify_receipt.py", # fixtures exercising the receipt writer's dated format
    "test__cam_common.py",         # fixtures mimicking Fusion's dated messageLog format
}
# A numbered run/item reference ("run 01", "item-5", "# Item 6:") points at a session log or a
# ticket nobody outside that process ever saw - describe the defect, not its ticket.
_RUN_NUMBER = re.compile(r"\brun[ -]\d")
_ITEM_NUMBER = re.compile(r"\bitem[ -]\d+\b|#\s*item\s*\d*\s*:")

# _LOWER_CHECKS are searched against the LOWERCASED line, _RAW_CHECKS against the raw one (the
# TODO / label shapes are case-sensitive by design).
_LOWER_CHECKS = (("run-number reference", _RUN_NUMBER), ("item-number reference", _ITEM_NUMBER))
_RAW_CHECKS = (("WO-<digit>", _WO_DIGIT), ("item-number label", _ITEM_LABEL),
               ("TODO marker", _TODO_MARKER), ("Phase <n> label", _PHASE_LABEL))

# "<path>" excuses a whole file; "<path>:<token>" excuses one detection class in it. Keyed by TOKEN
# rather than by line so an edit anywhere above the line does not re-pin the entry.
_ALLOWLIST = {
    "tests/lints/test_postconditions_declared.py:backlog":
        "the defect ledger's filename is the path that lint OPENS to resolve a gap declaration's "
        "id - a functional constant, not a pointer into a planning narrative",
    "tests/unit/test_joint_create_origin.py:Phase <n> label":
        "'Phase 2' names a step of the shipped insert-into-template skill, not a transient plan",
}


def _iter_files():
    for base in _SWEPT_DIRS:
        for root, dirs, files in os.walk(base):
            # evals/results/ holds per-run RECORDS: dated, additive documents (gitignored besides).
            # History narrative is their content, not baggage.
            if root.replace("\\", "/").endswith("tests/live/evals"):
                dirs[:] = [d for d in dirs if d != "results"]
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in files:
                if not (fn.endswith(".py") or fn.endswith(".md")):
                    continue
                if fn in _EXCLUDED_FILES:
                    continue
                yield os.path.join(root, fn)


def _tripped(line, check_date):
    """The tokens one line trips, in the order they are reported."""
    low = line.lower()
    tokens = [phrase for phrase, phrase_re in _PHRASES if phrase_re.search(low)]
    if check_date:
        undated = line
        for tok in _DATE_OK_TOKENS:
            undated = undated.replace(tok, "")
        if _DATE.search(undated):
            tokens.append("calendar date")
    tokens += [token for token, check in _LOWER_CHECKS if check.search(low)]
    tokens += [token for token, check in _RAW_CHECKS if check.search(line)]
    return tokens


def _line_offenders(path):
    """Every (lineno, token, line) offender in one file."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    check_date = os.path.basename(path) not in _DATE_EXEMPT_FILES
    # split on "\n" alone - the boundary iterating the file object uses. str.splitlines() also
    # breaks on a form feed, which would shift every line number after one.
    return [(i, token, line.strip())
            for i, line in enumerate(text.split("\n"), 1)
            for token in _tripped(line, check_date)]


class TestNoHistoricalOrPlanBaggage:
    def test_no_file_narrates_history_or_points_at_a_plan(self):
        offenders = []
        for path in _iter_files():
            rel = os.path.relpath(path, REPO_ROOT).replace("\\", "/")
            for lineno, token, text in _line_offenders(path):
                if rel in _ALLOWLIST or f"{rel}:{token}" in _ALLOWLIST:
                    continue
                offenders.append(f"{rel}:{lineno}: [{token}] {text}")
        assert not offenders, (
            "code is evergreen - describe the behavior, not the change/plan:\n  "
            + "\n  ".join(offenders)
        )

