# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: a cap/limit a wire description STATES is interpolated from the constant that ENFORCES it.

A digit run inside a wire description equal to a same-file module int constant whose NAME carries
CAP/CEILING/MAX/DEFAULT/LIMIT/DEPTH fails, as does a parameter DEFAULT spelling that same number."""

import ast
import os
import re

import _corpus
from conftest import TOOLS_DIR

# The naming convention every wire description constant uses: TOOL_DESCRIPTION, plus the per-verb
# _GET_DESC / _REQUEST_DESC variants an action-dispatched or multi-tool file carries.
_DESCRIPTION_NAME = re.compile(r".*_DESC(RIPTION)?$")

# Any run of digits, so a bound written flush against other characters ("1-4096", "800x600") is
# still read. A word-boundary match would miss those.
_DIGITS = re.compile(r"\d+")

# The cap vocabulary a constant's name must carry for its value to be treated as an enforced bound.
# It is what keeps the rule quiet: measured, without it the rule also fires where a description's
# digits collide with an unrelated constant (an alpha of 255 against a "0-255" colour RANGE, a
# zone count of 2 against the "2" in "a 2D drawing") - neither a cap, neither able to drift.
_CAP_NAME = re.compile(r"CAP|CEILING|MAX|DEFAULT|LIMIT|DEPTH")

# The keyword arguments whose value reaches the wire as a description. 'description' is the tool's
# own; 'input_param_description' is create_with_string_input's, which tool.py writes into the single
# input's JSON-schema "description" - the same surface an agent reads, under another argument name.
_DESCRIPTION_KWARGS = ("description", "input_param_description")


def _module_int_constants(tree):
    """{NAME: value} for every module-level ``NAME = <int literal>`` whose name is upper-case. A
    bool is excluded: True is an int to Python, so SOME_MAX = True would read as the value 1 and
    flag every '1' in a description."""
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not isinstance(value, ast.Constant):
            continue
        if not isinstance(value.value, int) or isinstance(value.value, bool):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.upper() == target.id:
                out[target.id] = value.value
    return out


def _description_literals(tree):
    """(label, lineno, text) for every plain string literal in a wire description position - a
    ``*_DESC``/``*_DESCRIPTION`` constant, a ``"description"`` value in a JSON-schema dict, and the
    ``description=`` / ``input_param_description=`` keyword arguments.

    Only ``ast.Constant`` strings are collected. The static pieces of an f-string are collected
    too - an f-string that still hardcodes a digit is a finding - while the interpolated value
    itself is not a literal, which is what exempts a correctly interpolated cap.

    A literal the positions reach twice is reported once, keyed by its exact source span: a
    ``*_DESC`` constant built from a call or a dict is read by two positions and is one sentence.
    """
    out = []
    seen = set()

    def collect(label, node):
        for n in ast.walk(node):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
                continue
            span = (n.lineno, n.col_offset, n.end_lineno, n.end_col_offset)
            if span in seen:
                continue
            seen.add(span)
            out.append((label, n.lineno, n.value))

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and _DESCRIPTION_NAME.match(target.id):
                collect(target.id, node.value)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "description":
                    collect("description", value)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in _DESCRIPTION_KWARGS:
                    collect(f"{kw.arg}=", kw.value)
    return out


def hardcoded_caps(src, filename="<src>"):
    """(label, lineno, [constant names], value, text) per digit run matching an in-file cap
    constant."""
    tree = ast.parse(src, filename=filename)
    by_value = _cap_constants(tree)
    hits = []
    for label, lineno, text in _description_literals(tree):
        for match in _DIGITS.finditer(text):
            value = int(match.group())
            if value in by_value:
                hits.append((label, lineno, sorted(by_value[value]), value, text))
    return hits


def _cap_constants(tree):
    """{value: [names]} for the module constants whose name carries the cap vocabulary."""
    by_value = {}
    for name, value in _module_int_constants(tree).items():
        if _CAP_NAME.search(name.upper()):
            by_value.setdefault(value, []).append(name)
    return by_value


def _parameter_defaults(tree):
    """(function, parameter, node) per int literal written as a PARAMETER DEFAULT. A constant's own
    module-level assignment is not one of these, so a declaration is never read as a copy of itself.

    The parameter-default position is where this rule sits because it was measured: any int literal
    anywhere flags 17 across the tool modules (a rounding place, an enum int), a parameter default
    flags 2, and narrowing further to cap-shaped parameter names buys nothing while dropping the
    ``width``/``height`` case the rule exists for."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        positional = args.posonlyargs + args.args
        pairs = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
        pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
        for arg, default in pairs:
            for n in ast.walk(default):
                if isinstance(n, ast.Constant) and isinstance(n.value, int) \
                        and not isinstance(n.value, bool):
                    out.append((node.name, arg.arg, n))
    return out


def bare_cap_defaults(src, filename="<src>"):
    """(function, parameter, lineno, [constant names], value) per PARAMETER DEFAULT spelled as a
    digit while a cap constant declared in the same file already holds that number.

    Interpolating the description binds the sentence to the constant but leaves the handler free:
    ``width: int = 800`` beside ``_WIDTH_DEFAULT = 800`` puts the number in two places again, and
    both legs read 800 until one of them moves."""
    tree = ast.parse(src, filename=filename)
    by_value = _cap_constants(tree)
    hits = [(func, param, node.lineno, sorted(by_value[node.value]), node.value)
            for func, param, node in _parameter_defaults(tree) if node.value in by_value]
    return sorted(hits, key=lambda h: (h[2], h[1]))


# (file, parameter) -> the audited reason the default is still a digit. Entries only leave this
# table; a new one is a defect, not a style choice.
_BARE_DEFAULT_ALLOWED = {
    ("design_get.py", "max_depth"):
        "handler default 3 beside _TREE_DEFAULT_DEPTH - open, owned by design_get.py",
    ("find_geometry.py", "max_results"):
        "handler default 20 beside _MAX_RESULTS_DEFAULT - open, owned by find_geometry.py",
}


def _sweep_bare_defaults():
    offenders = []
    for fn in sorted(os.listdir(TOOLS_DIR)):
        if not fn.endswith(".py"):
            continue
        src = _corpus.text(os.path.join(TOOLS_DIR, fn))
        for func, param, lineno, names, value in bare_cap_defaults(src, fn):
            if (fn, param) in _BARE_DEFAULT_ALLOWED:
                continue
            # Several constants can hold one value; the message names all of them and picks none -
            # only the code knows which one this parameter's description interpolates.
            offenders.append(f"{fn}:{lineno}: {func}({param}=...) spells {value} as a digit while "
                             f"{'/'.join(names)} holds it - write that constant as the default, so "
                             "the handler reads the number the description interpolates")
    return offenders


def _sweep():
    offenders = []
    for fn in sorted(os.listdir(TOOLS_DIR)):
        if not fn.endswith(".py"):
            continue
        path = os.path.join(TOOLS_DIR, fn)
        for label, lineno, names, value, _text in hardcoded_caps(_corpus.text(path), path):
            offenders.append(f"{fn}:{lineno}: {label} spells {value} as a digit while "
                             f"{'/'.join(names)} holds it - interpolate it "
                             f"(f\"... {{{names[0]}}} ...\") so the sentence cannot outlive the value")
    return offenders


class TestWireCapsAreInterpolated:
    def test_no_description_hardcodes_a_cap_constant(self):
        offenders = _sweep()
        assert not offenders, (
            "wire description(s) hardcoding a cap a constant in the same file already holds:\n  "
            + "\n  ".join(offenders))


class TestHandlerDefaultsReadTheConstant:
    def test_no_parameter_default_hardcodes_a_cap_constant(self):
        offenders = _sweep_bare_defaults()
        assert not offenders, (
            "parameter default(s) hardcoding a cap a constant in the same file already holds:\n  "
            + "\n  ".join(offenders))

    def test_every_allowlist_entry_still_names_a_live_offender(self):
        # the table only shrinks: an entry whose defect was fixed must be deleted, not left to
        # silence a future one at the same address.
        live = set()
        for fn in sorted(os.listdir(TOOLS_DIR)):
            if not fn.endswith(".py"):
                continue
            for _func, param, *_rest in bare_cap_defaults(_corpus.text(os.path.join(TOOLS_DIR, fn)),
                                                          fn):
                live.add((fn, param))
        stale = sorted(set(_BARE_DEFAULT_ALLOWED) - live)
        assert not stale, f"allowlist entries whose offender is gone - delete them: {stale}"
