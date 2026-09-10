# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Prose budget: comments, docstrings and wire sentences under tools/ stay bounded.

Six rules - note/error text, descriptions, docstrings, comment runs, per-file prose share,
catalog blurbs. Each failure names file:line and the measured size, so a cutting pass works
from the failure text. _EXEMPT lists the files failing each rule the day the lint shipped."""

import ast
import io
import os
import re
import tokenize

import pytest

import _corpus
from conftest import TOOLS_DIR, register_all_tools

NOTE_BUDGET_CHARS = 400
TOOL_DESCRIPTION_BUDGET_CHARS = 900
INPUT_DESCRIPTION_BUDGET_CHARS = 220
MODULE_DOCSTRING_MAX_LINES = 6
DEF_DOCSTRING_MAX_LINES = 4
COMMENT_RUN_MAX_LINES = 3
PROSE_SHARE_MAX = 0.30
BLURB_BUDGET_CHARS = 400

_DESCRIPTION_NAME = re.compile(r".*DESCRIPTION$")

# Files failing each rule TODAY, generated from a run of the rules themselves - a cutting wave
# deletes names from here, and nothing else may enter. Both directions are guarded below.
_EXEMPT = {
    "prose_share": frozenset({
        # __init__.py holds only the licence header and its module docstring, so its prose share is
        # 100% whatever the docstring says - the one entry no cut can retire.
        "__init__.py",
    }),
}


def _tool_files():
    """(filename, path) for every module under tools/ - the corpus all six rules read."""
    return [(fn, os.path.join(TOOLS_DIR, fn))
            for fn in sorted(os.listdir(TOOLS_DIR)) if fn.endswith(".py")]


def _static_len(node):
    """The characters of STATIC literal text one expression contributes: an f-string's constant
    pieces summed with its interpolations ignored, a `+` chain's two sides summed (so a
    produces_block() call appended to a description contributes nothing), 0 for anything else."""
    if isinstance(node, ast.Constant):
        return len(node.value) if isinstance(node.value, str) else 0
    if isinstance(node, ast.JoinedStr):
        return sum(_static_len(v) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_len(node.left) + _static_len(node.right)
    return 0


def _literals(node):
    """(lineno, size) per LITERAL in an expression: a dict/tuple/list of sentences is measured one
    entry at a time, everything else as one summed measurement."""
    if isinstance(node, ast.Dict):
        return [m for v in node.values for m in _literals(v)]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [m for e in node.elts for m in _literals(e)]
    size = _static_len(node)
    return [(node.lineno, size)] if size else []


def _callee(node):
    fn = node.func
    return fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None


def _module_assignments(tree):
    """(target, value) for every module-level assignment, one row per assigned name."""
    out = []
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign) else
                   [node.target] if isinstance(node, ast.AnnAssign) else [])
        if not targets or node.value is None:
            continue
        for target in targets:
            out += [(name, node.value) for name in ast.walk(target) if isinstance(name, ast.Name)]
    return out


def _keyed_values(node, key):
    """The values a dict literal holds under `key`."""
    return [v for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant) and k.value == key]


def _note_sites(tree):
    """(lineno, size) for every note/error sentence: error()'s first argument, a 'note' dict value,
    a module-level NOTE constant, and an assignment to a ['note'] subscript."""
    out = []
    for name, value in _module_assignments(tree):
        if "NOTE" in name.id:
            out += _literals(value)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node) == "error" and node.args:
            out += _literals(node.args[0])
        elif isinstance(node, ast.Dict):
            for value in _keyed_values(node, "note"):
                out += _literals(value)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                and t.slice.value == "note" for t in node.targets):
            out += _literals(node.value)
    return sorted(set(out))


def _description_sites(tree):
    """(lineno, size, cap) for every wire description: a module-level *DESCRIPTION constant, and
    each description inside an add_input_property(...) schema (nested schemas included)."""
    out = []
    for name, value in _module_assignments(tree):
        if _DESCRIPTION_NAME.match(name.id):
            out += [(ln, n, TOOL_DESCRIPTION_BUDGET_CHARS) for ln, n in _literals(value)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node) == "add_input_property":
            for arg in node.args:
                for d in [x for x in ast.walk(arg) if isinstance(x, ast.Dict)]:
                    for value in _keyed_values(d, "description"):
                        out += [(ln, n, INPUT_DESCRIPTION_BUDGET_CHARS) for ln, n in _literals(value)]
    return sorted(set(out))


def _docstring_sites(tree):
    """(lineno, lines, cap) for every docstring - the module's, and each def/class's."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        doc = node.body[0] if node.body else None
        if (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
                and isinstance(doc.value.value, str)):
            cap = (MODULE_DOCSTRING_MAX_LINES if isinstance(node, ast.Module)
                   else DEF_DOCSTRING_MAX_LINES)
            out.append((doc.lineno, doc.end_lineno - doc.lineno + 1, cap))
    return sorted(out)


def _comment_lines(src):
    """The line numbers carrying a comment, and the subset whose line is comment-ONLY."""
    lines = src.splitlines()
    every, alone = set(), set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            every.add(tok.start[0])
            if not lines[tok.start[0] - 1][:tok.start[1]].strip():
                alone.add(tok.start[0])
    return every, alone


def _comment_runs(src):
    """(start line, length) for every run of consecutive comment-only lines - a blank line or a
    line of code ends a run."""
    _every, alone = _comment_lines(src)
    runs, start = [], None
    for lineno in range(1, len(src.splitlines()) + 2):
        if lineno in alone:
            start = lineno if start is None else start
        elif start is not None:
            runs.append((start, lineno - start))
            start = None
    return runs


def _prose_share(src, tree):
    """(prose lines, non-blank lines) - comment lines plus docstring lines over the file's body."""
    every, _alone = _comment_lines(src)
    doc = {ln for start, lines, _cap in _docstring_sites(tree) for ln in range(start, start + lines)}
    nonblank = sum(1 for line in src.splitlines() if line.strip())
    return len(every | doc), nonblank


def _rule_wire_notes(fn, path):
    return [f"{fn}:{ln} note/error literal {n} chars (cap {NOTE_BUDGET_CHARS})"
            for ln, n in _note_sites(_corpus.tree(path)) if n > NOTE_BUDGET_CHARS]


def _rule_descriptions(fn, path):
    return [f"{fn}:{ln} description literal {n} chars (cap {cap})"
            for ln, n, cap in _description_sites(_corpus.tree(path)) if n > cap]


def _rule_docstrings(fn, path):
    return [f"{fn}:{ln} docstring {n} lines (cap {cap})"
            for ln, n, cap in _docstring_sites(_corpus.tree(path)) if n > cap]


def _rule_comment_runs(fn, path):
    return [f"{fn}:{ln} run of {n} comment-only lines (cap {COMMENT_RUN_MAX_LINES})"
            for ln, n in _comment_runs(_corpus.text(path)) if n > COMMENT_RUN_MAX_LINES]


def _rule_prose_share(fn, path):
    prose, nonblank = _prose_share(_corpus.text(path), _corpus.tree(path))
    share = prose / nonblank if nonblank else 0.0
    return ([f"{fn}:1 prose share {share:.1%} ({prose}/{nonblank} lines, cap "
             f"{PROSE_SHARE_MAX:.0%})"] if share > PROSE_SHARE_MAX else [])


def _rule_blurbs(fn, path):
    if not fn.startswith("_"):
        return []
    return [f"{fn}:{ln} MAP_BLURB {n} chars (cap {BLURB_BUDGET_CHARS})"
            for name, value in _module_assignments(_corpus.tree(path)) if name.id == "MAP_BLURB"
            for ln, n in _literals(value) if n > BLURB_BUDGET_CHARS]


_RULES = {
    "wire_notes": _rule_wire_notes,
    "descriptions": _rule_descriptions,
    "docstrings": _rule_docstrings,
    "comment_runs": _rule_comment_runs,
    "prose_share": _rule_prose_share,
    "blurbs": _rule_blurbs,
}


def _scan(rule):
    """Every offender line one rule finds, the exempt files skipped."""
    exempt = _EXEMPT.get(rule, frozenset())
    return [line for fn, path in _tool_files() if fn not in exempt
            for line in _RULES[rule](fn, path)]


def _report(rule, offenders):
    return (f"{len(offenders)} {rule} offender(s) - cut the text, or move the fact to where an "
            f"agent meets it when it matters:\n  " + "\n  ".join(offenders))


def test_no_note_or_error_exceeds_its_budget():
    offenders = _scan("wire_notes")
    assert not offenders, _report("wire_notes", offenders)


def test_no_description_exceeds_its_budget():
    offenders = _scan("descriptions")
    assert not offenders, _report("descriptions", offenders)


def test_no_shipped_tool_description_exceeds_its_budget():
    # The REGISTERED description, not the source literal: a description assembled from a generated
    # tail (_outputs.produces_block) or held under a name the AST rule's *DESCRIPTION pattern does
    # not match ships bytes that rule never measures.
    offenders = [f"{name} ships {len(text)} chars (cap {TOOL_DESCRIPTION_BUDGET_CHARS})"
                 for name, text in ((i.primitive.name, i.primitive.description or "")
                                    for i in register_all_tools())
                 if len(text) > TOOL_DESCRIPTION_BUDGET_CHARS]
    assert not offenders, _report("shipped descriptions", sorted(offenders))


def test_no_docstring_exceeds_its_line_cap():
    offenders = _scan("docstrings")
    assert not offenders, _report("docstrings", offenders)


def test_no_comment_run_exceeds_its_line_cap():
    offenders = _scan("comment_runs")
    assert not offenders, _report("comment_runs", offenders)


def test_no_file_exceeds_its_prose_share():
    offenders = _scan("prose_share")
    assert not offenders, _report("prose_share", offenders)


def test_no_map_blurb_exceeds_its_budget():
    offenders = _scan("blurbs")
    assert not offenders, _report("blurbs", offenders)


def _stale_exemptions():
    """Entries that no longer earn their place: a file that is gone, or one that passes the rule
    now. Each is an entry to delete, which is what keeps the table shrink-only."""
    stale = []
    for rule, files in _EXEMPT.items():
        for fn in sorted(files):
            path = os.path.join(TOOLS_DIR, fn)
            if not os.path.exists(path):
                stale.append(f"{rule}: {fn} - no such file, drop the entry")
            elif not _RULES[rule](fn, path):
                stale.append(f"{rule}: {fn} - passes the rule now, drop the entry")
    return stale


def test_the_exemption_table_only_shrinks():
    stale = _stale_exemptions()
    assert not stale, ("_EXEMPT entries that no longer earn their place:\n  " + "\n  ".join(stale))


def test_no_passing_file_can_join_the_exemption_table(monkeypatch):
    clean = next(fn for fn, path in _tool_files() if not _RULES["comment_runs"](fn, path))
    assert set(_EXEMPT) <= set(_RULES), "an _EXEMPT key that names no rule"
    monkeypatch.setitem(_EXEMPT, "comment_runs",
                        frozenset(_EXEMPT.get("comment_runs", ())) | {clean, "no_such_file.py"})
    assert sorted(_stale_exemptions()) == sorted([
        f"comment_runs: {clean} - passes the rule now, drop the entry",
        "comment_runs: no_such_file.py - no such file, drop the entry",
    ])
    with pytest.raises(AssertionError, match=clean):
        test_the_exemption_table_only_shrinks()


def _probe(tmp_path, name, src, rule):
    (tmp_path / name).write_text(src, encoding="utf-8")
    return _RULES[rule](name, str(tmp_path / name))


def test_the_character_caps_bite_one_character_over(tmp_path):
    for rule, template, cap in (
            ("wire_notes", 'def h():\n    return error("%s")\n', NOTE_BUDGET_CHARS),
            ("descriptions", 'TOOL_DESCRIPTION = "%s"\n', TOOL_DESCRIPTION_BUDGET_CHARS),
            ("descriptions", 't.add_input_property("a", {"description": "%s"})\n',
             INPUT_DESCRIPTION_BUDGET_CHARS),
            ("blurbs", 'MAP_BLURB = "%s"\n', BLURB_BUDGET_CHARS)):
        assert not _probe(tmp_path, f"_{rule}{cap}at.py", template % ("x" * cap), rule)
        over = _probe(tmp_path, f"_{rule}{cap}over.py", template % ("x" * (cap + 1)), rule)
        assert len(over) == 1 and f"{cap + 1}" in over[0], over


def test_only_static_literal_text_counts(tmp_path):
    at = "y" * NOTE_BUDGET_CHARS
    assert not _probe(tmp_path, "f_at.py", 'def h():\n    return error(f"%s{n_more}")\n' % at,
                      "wire_notes")
    assert _probe(tmp_path, "f_over.py", 'def h():\n    return error(f"%sy{n}")\n' % at,
                  "wire_notes")
    desc = "d" * TOOL_DESCRIPTION_BUDGET_CHARS
    assert not _probe(tmp_path, "p_at.py",
                      'TOOL_DESCRIPTION = ("%s"\n    + _outputs.produces_block(RETURNS))\n' % desc,
                      "descriptions")
    assert _probe(tmp_path, "p_over.py",
                  'TOOL_DESCRIPTION = ("%s"\n    + "d" + _outputs.produces_block(RETURNS))\n' % desc,
                  "descriptions")


def test_the_line_caps_bite_one_line_over(tmp_path):
    mod = '"""m\n%s"""\n'
    assert not _probe(tmp_path, "m_at.py", mod % ("m\n" * (MODULE_DOCSTRING_MAX_LINES - 2)),
                      "docstrings")
    assert _probe(tmp_path, "m_over.py", mod % ("m\n" * (MODULE_DOCSTRING_MAX_LINES - 1)),
                  "docstrings")
    fn = 'def h():\n    """d\n%s    """\n'
    assert not _probe(tmp_path, "d_at.py", fn % ("    d\n" * (DEF_DOCSTRING_MAX_LINES - 2)),
                      "docstrings")
    assert _probe(tmp_path, "d_over.py", fn % ("    d\n" * (DEF_DOCSTRING_MAX_LINES - 1)),
                  "docstrings")
    assert not _probe(tmp_path, "c_at.py", "# c\n" * COMMENT_RUN_MAX_LINES + "x = 1\n",
                      "comment_runs")
    assert _probe(tmp_path, "c_over.py", "# c\n" * (COMMENT_RUN_MAX_LINES + 1) + "x = 1\n",
                  "comment_runs")


def test_a_run_ends_at_a_blank_line_or_a_line_of_code(tmp_path):
    run = "# c\n" * COMMENT_RUN_MAX_LINES
    assert not _probe(tmp_path, "r_blank.py", run + "\n" + run, "comment_runs")
    assert not _probe(tmp_path, "r_code.py", run + "x = 1\n" + run, "comment_runs")
    assert _comment_runs("# a\n# b\n\n# c\n") == [(1, 2), (4, 1)]
    assert _comment_runs("# a\nx = 1  # trailing\n# b\n") == [(1, 1), (3, 1)]
    assert _comment_runs("x = 1  # trailing\n") == []


def test_the_prose_share_cap_bites_at_thirty_percent(tmp_path):
    code = "x = 1\n"
    assert not _probe(tmp_path, "s_at.py", "# c\n" * 3 + code * 7, "prose_share")
    over = _probe(tmp_path, "s_over.py", "# c\n" * 3 + "# c\n" + code * 6, "prose_share")
    assert len(over) == 1 and "40.0% (4/10 lines" in over[0], over
    assert _prose_share('"""d\nd"""\n# c\nx = 1\n\ny = 2\n', ast.parse('"""d\nd"""\n# c\nx = 1\n\ny = 2\n')) == (3, 5)
