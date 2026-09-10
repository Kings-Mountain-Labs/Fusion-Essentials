# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: a de-dup/comparison key over Fusion entities is ``_common.native_identity``, never a bare
``entityToken`` - keyed, or compared with ``==``. The ``token or id(x)`` fallback counts as a key. A
token is DOCUMENT-LOCAL, so entities reached through two x-refs MERGE under one key, and a wrapper's
own token differs from its native's, so one body SPLITS into two entries.
"""

import ast
import os

import _corpus
import live_api_facts
from conftest import TOOLS_DIR


# (module basename without .py, enclosing function) -> why this site is exempt. A reason must state
# a fact about THIS site. An entry prefixed 'gap:' is NOT an exemption-as-correct: it is a named
# open defect nobody has fixed yet, and it only stays legal while the reason names what was
# measured. Kept honest by test_allowlist_entries_still_trip; the table only shrinks.
_ALLOWLIST = {}


def _mentions_entity_token(node):
    """True when a bare document-local token is read in this subtree, in any of the three spellings
    the repo writes it in: the attribute, the getattr name string, and `_common.native_token` - the
    sanctioned reader, which resolves a proxy to its native (closing the SPLIT) but still returns a
    document-local value (leaving the MERGE)."""
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == "entityToken":
            return True
        if isinstance(n, ast.Constant) and n.value == "entityToken":
            return True
        if isinstance(n, ast.Call) and _func_name(n) == "native_token":
            return True
    return False


def _func_name(call):
    """The called name bare or through a module: `_common.native_token(x)` answers 'native_token'."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _calls_id(node):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "id"
               for n in ast.walk(node))


def _is_token_or_id_key(node):
    """True for `<... entityToken ...> or id(...)` - either operand order, any wrapping."""
    if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
        return False
    return (any(_mentions_entity_token(v) for v in node.values)
            and any(_calls_id(v) for v in node.values))


# The mapping methods whose FIRST argument is a key (or a set member).
_KEYED_CALLS = ("get", "add", "setdefault", "pop")


def _key_positions(node):
    """Every expression this node uses as a MAPPING KEY. The VALUE side is deliberately not read:
    publishing a token as a dict value (a handle a tool hands back) is the wrapper token's job."""
    if isinstance(node, ast.Subscript):
        return [node.slice]
    if isinstance(node, ast.Compare):
        return [node.left for op in node.ops if isinstance(op, (ast.In, ast.NotIn))]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in _KEYED_CALLS and node.args):
        return [node.args[0]]
    return []


def _walks_bodies_or_components(expr):
    """True when an iteration draws from a collection of BODIES or COMPONENTS, by the naming
    convention the Fusion API and this repo share. The WHOLE expression is searched, since the house
    idiom wraps the collection: iter_collection(safe(lambda: host.bRepBodies))."""
    for n in ast.walk(expr):
        name = n.attr if isinstance(n, ast.Attribute) else (n.id if isinstance(n, ast.Name) else None)
        if name and (name.lower().endswith("bodies") or name.lower().endswith("components")):
            return True
    return False


def _reads_any(expr, names):
    return any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(expr))


def _target_names(target):
    """The loop variables one `for` target binds: `for b, nm in zip(...)` binds a body exactly as
    `for b in bodies` does. A starred or attribute/subscript element binds no local name here."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return {n for element in target.elts for n in _target_names(element)}
    return set()


def _token_gathering_comprehension(node):
    """True for a SET/DICT-key/generator comprehension gathering bare tokens off a body or component
    collection - that set IS the key collection, so no `d[tok]` appears for _key_positions to see.
    LIST comprehensions are excluded: a list of tokens is how a tool publishes HANDLES."""
    if isinstance(node, (ast.SetComp, ast.GeneratorExp)):
        element = node.elt
    elif isinstance(node, ast.DictComp):
        element = node.key
    else:
        return False
    return (_mentions_entity_token(element)
            and any(_walks_bodies_or_components(g.iter) for g in node.generators))


def _token_bindings(assign):
    """The local names one assignment binds to a BARE token VALUE. An element-wise tuple assignment
    is paired up element by element (`tok, name = a.entityToken, a.name` binds ONE token), so a
    sibling bound to something else in the same statement is not marked a token."""
    out = set()
    for target in assign.targets:
        if (isinstance(target, ast.Tuple) and isinstance(assign.value, ast.Tuple)
                and len(target.elts) == len(assign.value.elts)):
            for element, value in zip(target.elts, assign.value.elts):
                if _mentions_entity_token(value):
                    out |= _target_names(element)
        elif _mentions_entity_token(assign.value):
            out |= _target_names(target)
    return out


def _is_token_identity_compare(node, token_vars):
    """True for an `==`/`!=` whose EVERY side is a bare token - read inline, or held in a local this
    frame bound to one. Both sides must be tokens: a token compared against a wire string is a
    handle round-trip, not a claim that two entities are one."""
    if not isinstance(node, ast.Compare) or not node.ops:
        return False
    if not all(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
        return False
    sides = [node.left] + list(node.comparators)
    return all(_mentions_entity_token(s) or _reads_any(s, token_vars) for s in sides)


def _offenders(tree):
    """[(enclosing function name, lineno)] for every offending shape in `tree`. The function NAME is
    what an allowlist entry addresses - a line number moves with any edit above it.

    `walked` holds the loop variables bound to a body/component collection in scope, `tokens` the
    locals assigned a token read off one of them, and `token_vars` every local this frame bound to a
    token whatever it was read off. A nested function's own PARAMETERS shadow all three: without that
    subtraction, one function's `for b in comp.bRepBodies` would make every other function's `b`
    parameter look like a walked body."""
    found = []

    def walk(node, fname, walked, tokens, token_vars):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params = {a.arg for a in child.args.args + child.args.kwonlyargs}
                walk(child, child.name, walked - params, tokens - params, token_vars - params)
                continue
            if isinstance(child, ast.For) and _walks_bodies_or_components(child.iter):
                walked = walked | _target_names(child.target)
            if isinstance(child, ast.Assign):
                token_vars = token_vars | _token_bindings(child)
                if _mentions_entity_token(child.value) and _reads_any(child.value, walked):
                    tokens = tokens | {t.id for t in child.targets if isinstance(t, ast.Name)}
            if (_is_token_or_id_key(child) or _token_gathering_comprehension(child)
                    or _is_token_identity_compare(child, token_vars)):
                found.append((fname, child.lineno))
            else:
                for key in _key_positions(child):
                    if ((_mentions_entity_token(key) and _reads_any(key, walked))
                            or _reads_any(key, tokens)):
                        found.append((fname, child.lineno))
                        break
            walk(child, fname, walked, tokens, token_vars)

    walk(tree, "<module>", frozenset(), frozenset(), frozenset())
    return found


def _tool_modules():
    return [fn for fn in sorted(os.listdir(TOOLS_DIR)) if fn.endswith(".py")]


class TestEntityKeysUseNativeIdentity:
    def test_no_tool_module_keys_on_a_bare_entity_token(self):
        offenders = []
        for fn in _tool_modules():
            for fname, lineno in _offenders(_corpus.tree(os.path.join(TOOLS_DIR, fn))):
                if (fn[:-3], fname) in _ALLOWLIST:
                    continue
                offenders.append(f"{fn}:{lineno} (in {fname})")
        assert not offenders, (
            "an entityToken is DOCUMENT-LOCAL and a wrapper's own token is not its native's, so a "
            "key built on one MERGES distinct entities across documents (two bodies reached through "
            "two x-refs; every document's root component) and SPLITS one body from its own proxy. "
            "Key on _common.native_identity(entity) instead - as the de-dup fallback "
            "'native_identity(b) or id(b)' (keep the id() last resort: it over-counts an "
            "identity-less entity rather than merging it), or as the mapping key itself. A "
            "same-entity COMPARISON runs on _common.same_component (two components) or on two "
            "native_identity values gated so that an unread one answers unknown instead of "
            "comparing equal to the other unread one. A site "
            "keying something native_identity does not describe - or a named open defect - gets an "
            "_ALLOWLIST entry with a reason:\n  " + "\n  ".join(offenders))

    def test_allowlist_entries_still_trip(self):
        # An entry pointing at a site that no longer carries the shape is dead weight hiding the
        # next copy; one pointing at a module or function that is gone is worse.
        stale = []
        for (mod_name, fname), reason in _ALLOWLIST.items():
            assert reason.strip(), f"({mod_name}, {fname}) allowlist entry needs a reason"
            path = os.path.join(TOOLS_DIR, mod_name + ".py")
            if not os.path.exists(path):
                stale.append(f"({mod_name}, {fname}): no such module - remove the entry")
                continue
            if fname not in {n for n, _ in _offenders(_corpus.tree(path))}:
                stale.append(f"({mod_name}, {fname}): no longer keys on a bare entityToken - "
                             "remove the entry")
        assert not stale, "stale allowlist entries:\n  " + "\n  ".join(stale)

    def test_the_scoping_argument_matches_what_was_measured(self):
        # The mapping-key arm is scoped to bodies/components because only a kind reaching a source
        # document has an identity carrying more than the token. That scope rests on the measured
        # SHAPES table, so the two facts it uses are pinned here rather than left as prose: an
        # Occurrence carries neither member (its token key IS the identity key), and a Sketch does
        # carry one (its absence is an unclosed spelling, not an equivalence). Timeline also
        # carries parentDesign, and no identity is ever taken for a timeline.
        shapes = live_api_facts.SHAPES
        assert sorted(k for k, v in shapes.items() if "parentDesign" in v) == ["Component", "Timeline"]
        assert "parentComponent" in shapes["Sketch"]
        assert "parentComponent" in shapes["BRepBody"] and "parentComponent" in shapes["MeshBody"]
        assert "parentComponent" not in shapes["Occurrence"]
        assert "parentDesign" not in shapes["Occurrence"]
