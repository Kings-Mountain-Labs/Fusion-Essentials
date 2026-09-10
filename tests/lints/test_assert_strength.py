# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: every test must be able to fail for the RIGHT reason.

A test whose only assertion is a bare isError flag check must also pin the error MESSAGE or another
payload fact; a test with no assertion at all (and no pytest.raises) cannot fail, so it fails here."""

import ast
from pathlib import Path

import _corpus

TESTS = Path(__file__).parents[1]

# test-id -> one-line audited reason a bare flag check is genuinely sufficient. Shrink-only.
_EXEMPT = {}


def _is_bare_flag_assert(node):
    """True when an Assert's whole substance is res[...]['isError'] compared to/used as a bool."""
    expr = node.test
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        expr = expr.operand
    if isinstance(expr, ast.Compare):
        involved = [expr.left] + list(expr.comparators)
        subscripts = [n for n in involved if isinstance(n, ast.Subscript)]
        others = [n for n in involved
                  if not isinstance(n, (ast.Subscript, ast.Constant))]
        if others or not subscripts:
            return False
        return all(isinstance(s.slice, ast.Constant) and s.slice.value == "isError"
                   for s in subscripts)
    if isinstance(expr, ast.Subscript):
        return isinstance(expr.slice, ast.Constant) and expr.slice.value == "isError"
    return False


def _asserts_by_call(fn):
    """True when the test asserts through a call: pytest.raises/fail, or a shared assert_*
    helper (the conftest helpers assert on message content internally)."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in ("raises", "fail") or name.startswith("assert_"):
                return True
    return False


def _test_functions(tree):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            yield "", node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name.startswith("test_"):
                    yield node.name + ".", item


class TestAssertStrength:
    def test_no_test_relies_on_a_bare_iserror_flag_alone(self):
        offenders = []
        for path in sorted(TESTS.rglob("test_*.py")):
            for prefix, fn in _test_functions(_corpus.tree(path)):
                test_id = f"{path.name}::{prefix}{fn.name}"
                if test_id in _EXEMPT:
                    continue
                asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
                if _asserts_by_call(fn):
                    continue
                if not asserts:
                    offenders.append(f"{test_id}: no assertion at all")
                    continue
                if all(_is_bare_flag_assert(a) for a in asserts):
                    offenders.append(f"{test_id}: only asserts the isError flag")
        assert not offenders, (
            "Weak tests (also pin the error message / a payload fact, so the test fails when the "
            "tool errors for the WRONG reason):\n" + "\n".join(offenders))
