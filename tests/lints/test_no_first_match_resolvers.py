"""Lint: the substring-first-match smell is banned across EVERY tool module, and the tools that
resolve a single occurrence keep routing through the shared resolver. A single-target resolver
fails when it picks a candidate by a substring/`.find()` name match, or returns the first EXACT
name hit instead of collecting every match and asking `len()` how many carry the name."""

import ast
import operator
import os
import re

import _corpus
from conftest import TOOLS_DIR

# Only shapes that are a smell REGARDLESS of name uniqueness: substring containment and
# indexed-first-of-many. A bare `x.lower() == want` is the CORRECT shape for a scope-unique name
# (find_setup/find_operation), so it is deliberately not flagged.

# `want.lower() in nm.lower()` / `x.lower() in cand.name`, either direction.
_SUBSTRING_NAME = re.compile(r"\.lower\(\)\s*in\s+.*(?:\.name|\.lower\(\)|\bnm\b|\bname\b)", re.I)

# The reversed shape, anchored to if/elif with an IDENTIFIER operand so it skips a literal
# membership test (`if "http" in x.lower()`) and a list-comprehension collector.
_SUBSTRING_NAME_REV = re.compile(r"^\s*(?:el)?if\s+\w+\s+in\s+.*\.lower\(\)")

# `.find(...)` used as a containment test - the substring smell wearing str.find.
_FIND_NAME = re.compile(r"\.find\([^)]*\)\s*(?:>=\s*0|!=\s*-?1|>\s*-1)", re.I)

# `[c for c in ... if term in c.name][0]` - every loose match collected, then one silently taken.
_FIRST_OF_COMPREHENSION = re.compile(
    r"\[[^\]]*\bfor\b[^\]]*\b(?:name|nm)\b[^\]]*\]\s*\[\s*0\s*\]", re.I)


def _smells(line):
    return bool(_SUBSTRING_NAME.search(line)
                or _SUBSTRING_NAME_REV.search(line)
                or _FIND_NAME.search(line)
                or _FIRST_OF_COMPREHENSION.search(line))


# The exact-name first-match PICK reads as the correct scope-unique shape line by line, so it is
# matched over the AST in four spellings: the returning loop (`for`, `for i, c in enumerate(...)`,
# `while (c := ...)`), the same loop written inside-out with `continue`, `next((c for c in ... if
# c.name == want), None)`, and a `[0]` off a collection of EVERY name match. The way out is a
# `len()` question that separates one from many. Blind spots needing a reviewer: a name read through
# a call (`if comp_label(c) == want`), a len() question whose answer is ignored, and a collector
# named outside the by_name/_named vocabulary.


def _reads_a_name(node):
    return any(isinstance(n, ast.Attribute) and n.attr in ("name", "fullPathName")
               for n in ast.walk(node))


def _name_locals(loop):
    """Loop-body variables assigned FROM a `.name` read: `nm = p.name` moves the read, not the
    shape, so a comparison against `nm` is the same comparison."""
    out = set()
    for n in ast.walk(loop):
        if isinstance(n, ast.Assign) and n.value is not None and _reads_a_name(n.value):
            out.update(t.id for t in n.targets if isinstance(t, ast.Name))
    return out


def _exact_name_test(test, name_locals=(), op=ast.Eq):
    """True when `test` compares something that came from a candidate's name with `op` (`==`, or
    `!=` for the skip-the-others spelling of the same loop)."""
    for n in ast.walk(test):
        if not (isinstance(n, ast.Compare) and any(isinstance(o, op) for o in n.ops)):
            continue
        if _reads_a_name(n):
            return True
        if any(isinstance(x, ast.Name) and x.id in name_locals for x in ast.walk(n)):
            return True
    return False


def _candidate_vars(loop):
    """The variables `loop` binds to ONE candidate per pass - what a first-match pick returns. A
    `for` target is a Name or a TUPLE holding one; a `while` binds its candidate with a walrus."""
    if isinstance(loop, ast.For):
        target = loop.target
        elts = target.elts if isinstance(target, ast.Tuple) else [target]
        return {e.id for e in elts if isinstance(e, ast.Name)}
    return {n.target.id for n in ast.walk(loop.test)
            if isinstance(n, ast.NamedExpr) and isinstance(n.target, ast.Name)}


def _returns_the_candidate(body, var):
    """True when `body` returns the loop variable ITSELF (bare, or as a tuple element) - returning
    `c.name` or a count is a read ABOUT the candidates, not a pick of one."""
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Return) and n.value is not None:
                vals = n.value.elts if isinstance(n.value, ast.Tuple) else [n.value]
                if any(isinstance(v, ast.Name) and v.id == var for v in vals):
                    return True
    return False


def _skips_the_others(loop, name_locals, cvars):
    """True for `if c.name != want: continue` ... `return c` - the returning loop written
    inside-out."""
    for st in ast.walk(loop):
        if not (isinstance(st, ast.If) and _exact_name_test(st.test, name_locals, op=ast.NotEq)):
            continue
        if not any(isinstance(n, ast.Continue) for s in st.body for n in ast.walk(s)):
            continue
        if any(_returns_the_candidate(loop.body, v) for v in cvars):
            return True
    return False


def _is_name_filtered(node):
    """True for `[c for c in ... if c.name == want]` - a collection filtered on a candidate's name."""
    return isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)) and any(
        _exact_name_test(cond) for gen in node.generators for cond in gen.ifs)


# The shared collect-by-name helpers' spelling (`find_joints_by_name`, `_planes_named_in`): each
# returns a LIST because the name space is not unique, so its `[0]` is the comprehension's pick with
# the filtering moved behind a call. `_named` is anchored at the END (or before `_in`) because
# `_named_scope` returns an (entity, error) PAIR, whose `[0]` is a tuple element.
_NAME_LOOKUP_CALL = re.compile(r"by_name|_named(?:_in)?$", re.I)


def _is_name_lookup_call(node):
    """True for a call to a collect-by-name helper, read off the CALLEE's identifier - the only
    thing a call site decides on its own."""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    ident = (fn.id if isinstance(fn, ast.Name)
             else fn.attr if isinstance(fn, ast.Attribute) else "")
    return bool(_NAME_LOOKUP_CALL.search(ident))


def _collects_every_name_match(node):
    """True for a value holding EVERY candidate that carries a name - a name-filtered comprehension
    OR a collect-by-name call."""
    return _is_name_filtered(node) or _is_name_lookup_call(node)


def _own_nodes(scope):
    """Every node under `scope`'s own body, NOT descending into a nested function or class: those
    are scopes of their own, and a `len(hits)` inside one is a different question about a different
    `hits`. Walking through them is how an unrelated count comes to excuse this scope's subscript."""
    out, stack = [], list(getattr(scope, "body", []))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        out.append(node)
        stack.extend(c for c in ast.iter_child_nodes(node)
                     if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    return out


def _assignments(scope):
    """{variable: [(line, did it collect every name match)]} for the plain `v = ...` bindings in
    `scope` - enough to ask WHICH binding reaches a later subscript."""
    out = {}
    for n in _own_nodes(scope):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)):
            out.setdefault(n.targets[0].id, []).append(
                (n.lineno, _collects_every_name_match(n.value)))
    return out


_COMPARISONS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
                ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def _separates_one_from_many(op, const, len_on_left):
    """True when `len(v) <op> const` ANSWERS DIFFERENTLY for one item and for two - evaluated rather
    than listed, so `== 1`, `!= 1`, `> 1`, `< 2` earn the pass while `> 0` and `>= 1` (which are
    `if v:` spelled with len()) do not."""
    fn = _COMPARISONS.get(type(op))
    if fn is None or not isinstance(const, int) or isinstance(const, bool):
        return False
    pair = ((fn(1, const), fn(2, const)) if len_on_left else (fn(const, 1), fn(const, 2)))
    return pair[0] != pair[1]


def _counting_lines(scope, var):
    """The lines in `scope` that ask how many `var` holds in a way that separates one from many."""
    lines = []
    for n in _own_nodes(scope):
        if not isinstance(n, ast.Compare) or len(n.ops) != 1:
            continue
        left, right, op = n.left, n.comparators[0], n.ops[0]
        for side, other, on_left in ((left, right, True), (right, left, False)):
            if (isinstance(side, ast.Call) and isinstance(side.func, ast.Name)
                    and side.func.id == "len" and side.args
                    and isinstance(side.args[0], ast.Name) and side.args[0].id == var
                    and isinstance(other, ast.Constant)
                    and _separates_one_from_many(op, other.value, on_left)):
                lines.append(n.lineno)
    return lines


def _takes_the_first(scope):
    """Each `[0]` taken off a collection of EVERY name match in ONE function - directly, or through
    a variable whose reaching binding is such a collection and which nothing counted in between.
    Scoped to the function and to the binding because `hits`/`matches`/`found` are everywhere: a
    module-wide lookup lets an unrelated `len(hits)` excuse a subscript nothing guards."""
    assigns = _assignments(scope)
    out = []
    for n in _own_nodes(scope):
        if not (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                and n.slice.value == 0):
            continue
        val = n.value
        if _collects_every_name_match(val):
            out.append(n)                       # the one-line form: nothing was named to count
            continue
        if not isinstance(val, ast.Name):
            continue
        earlier = [b for b in assigns.get(val.id, []) if b[0] <= n.lineno]
        if not earlier:
            continue
        bound_at, from_a_name_match = max(earlier)
        if not from_a_name_match:
            continue                            # a later rebinding reaches here, not the collection
        if not any(bound_at < ln <= n.lineno for ln in _counting_lines(scope, val.id)):
            out.append(n)
    return out


def _first_match_loops(src):
    """[(enclosing function, line number, line)] for every exact-name first-match PICK in `src`."""
    tree = ast.parse(src)
    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                owner.setdefault(id(n), fn.name)
    lines = src.splitlines()
    out = []

    def record(node, lineno):
        out.append((owner.get(id(node), "<module>"), lineno, lines[lineno - 1].strip()))

    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.While)):
            cvars = _candidate_vars(node)
            if not cvars:
                continue
            name_locals = _name_locals(node)
            for st in ast.walk(node):
                if (isinstance(st, ast.If) and _exact_name_test(st.test, name_locals)
                        and any(_returns_the_candidate(st.body, v) for v in cvars)):
                    record(node, st.lineno)
            if _skips_the_others(node, name_locals, cvars):
                record(node, node.lineno)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "next" and node.args
                and isinstance(node.args[0], ast.GeneratorExp)):
            gen = node.args[0]
            if any(_exact_name_test(cond) for g in gen.generators for cond in g.ifs):
                record(node, node.lineno)
    # Per FUNCTION, never per module: which binding reaches a subscript, and whether anything
    # counted it, are questions about one function's body.
    scopes = [fn for fn in ast.walk(tree)
              if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in scopes + [tree]:      # every function on its own, then the module's own body
        for node in _takes_the_first(scope):
            record(node, node.lineno)
    return sorted(set(out), key=lambda r: r[1])


# module.function -> why THIS resolver's name space makes an exact first match correct. Each reason
# must describe the code's own behavior, not point at a review or work item.
_FIRST_MATCH_ALLOWLIST = {
    # The loop widens to allParameters after userParameters.itemByName misses; both name spaces are
    # the one an expression resolves against.
    "_param_common._find_parameter":
        "parameter names are the identifiers expressions reference - unique by construction",
    # These two search the CLOUD data model. The FOLDER name space is measured unique; the PROJECT
    # one is not established, and that resolver returns the first sibling carrying the name.
    "_data_common._find_project":
        "one hub's projects, matched case-insensitively; whether a hub can hold two projects of "
        "one name is not established here",
    "_data_common._child_folder_by_name":
        "one folder's immediate subfolders, a name space measured UNIQUE: adding a subfolder under "
        "a name a sibling already carries raises '3 : CB_NAE - Another object with the same name "
        "already exists in this container', so a folder cannot hold two of one name and the first "
        "match is the only match",
}


# module (no .py) -> why this one is allowed to keep the pattern. Each reason must describe the
# module's own behavior, not point at a review or work item.
_ALLOWLIST = {}


def _all_tool_files():
    return [fn for fn in sorted(os.listdir(TOOLS_DIR))
            if fn.endswith(".py") and fn != "__init__.py"]


class TestNoFirstMatchResolverAnywhere:
    def test_no_tool_hand_rolls_a_substring_name_match(self):
        offenders = []
        for fn in _all_tool_files():
            mod_name = fn[:-3]
            if mod_name in _ALLOWLIST:
                continue
            src = _corpus.text(os.path.join(TOOLS_DIR, fn))
            for i, line in enumerate(src.splitlines(), 1):
                if _smells(line):
                    offenders.append(f"{fn}:{i}: {line.strip()}")
        assert not offenders, (
            "these lines resolve a single target by a lower()-cased substring match against .name - "
            "the wrong-instance risk _inputs._resolve_occurrence (or the OccurrenceRef/"
            "OccurrenceRefList kind) exists to refuse instead of guessing. Route through the shared "
            "resolver, or add a plain-English allowlist entry naming why this one is different:\n"
            + "\n".join(offenders)
        )

    def test_allowlist_entries_still_exist_and_still_trip_the_smell(self):
        # An allowlist entry that no longer matches anything (the code moved on) is dead weight that
        # hides a regression check; keep the list honest by requiring every entry to still be real.
        stale = []
        for mod_name, reason in _ALLOWLIST.items():
            assert reason.strip(), f"{mod_name} allowlist entry needs a plain-English reason"
            path = os.path.join(TOOLS_DIR, mod_name + ".py")
            if not os.path.exists(path):
                stale.append(f"{mod_name}: no such module")
                continue
            src = _corpus.text(path)
            if not any(_smells(line) for line in src.splitlines()):
                stale.append(f"{mod_name}: no longer matches the smell - remove the allowlist entry")
        assert not stale, "stale allowlist entries:\n  " + "\n  ".join(stale)

    def test_no_tool_returns_the_first_exact_name_hit_from_a_loop(self):
        offenders = []
        for fn in _all_tool_files():
            mod_name = fn[:-3]
            src = _corpus.text(os.path.join(TOOLS_DIR, fn))
            for func, lineno, line in _first_match_loops(src):
                if f"{mod_name}.{func}" in _FIRST_MATCH_ALLOWLIST:
                    continue
                offenders.append(f"{fn}:{lineno} (in {func}): {line}")
        assert not offenders, (
            "these return the FIRST candidate whose name matches exactly - a pick of one of however "
            "many carry that name, with nothing said about the rest. Collect every match and ASK "
            "HOW MANY: len(matches) == 1 resolves, more REFUSES naming the candidates (_export."
            "find_component / _cam_common.find_operation / _joints.find_joint). Collecting alone is "
            "not the way out - a [0] off a name-filtered comprehension, or off a collect-by-name "
            "helper's list, is this same pick. Or add a '<module>.<function>' entry to "
            "_FIRST_MATCH_ALLOWLIST saying why that name space cannot hold two:\n"
            + "\n".join(offenders)
        )

    def test_first_match_allowlist_entries_still_exist_and_still_trip(self):
        stale = []
        for key, reason in _FIRST_MATCH_ALLOWLIST.items():
            assert reason.strip(), f"{key} allowlist entry needs a plain-English reason"
            mod_name, _, func = key.partition(".")
            path = os.path.join(TOOLS_DIR, mod_name + ".py")
            if not os.path.exists(path):
                stale.append(f"{key}: no such module")
                continue
            src = _corpus.text(path)
            if not any(f == func for f, _ln, _line in _first_match_loops(src)):
                stale.append(f"{key}: no longer a first-match loop - remove the allowlist entry")
        assert not stale, "stale first-match allowlist entries:\n  " + "\n  ".join(stale)


# Modules whose SINGLE-occurrence resolution was routed through _inputs._resolve_occurrence. The
# frozen list is what stops a routed tool from passing the smell ban by ceasing to resolve
# occurrences at all; the pattern tools declare theirs in the shared _pattern_common.
_ROUTED_TOOLS = (
    "assembly_ground",
    "assembly_move",
    "assembly_rigid_group",
    "assembly_constrain",
    "joint_create_as_built",
    "model_arrange",
    "_pattern_common",
    "_joint_inputs",
    "view_screenshot",
    "view_section",
)


class TestRoutedToolsStayOnSharedResolver:
    def test_routed_tools_reference_the_shared_resolver(self):
        missing = []
        for name in _ROUTED_TOOLS:
            src = _corpus.text(os.path.join(TOOLS_DIR, f"{name}.py"))
            if "_resolve_occurrence" not in src and "OccurrenceRef" not in src:
                missing.append(name)
        assert not missing, (
            "expected these to call _inputs._resolve_occurrence (directly or via "
            "OccurrenceRef/OccurrenceRefList): " + ", ".join(missing))
