# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: an export-options knob is written through ``_export.applied_pair``, never a bare
set-then-read-back. Measured, an unset knob already reads the value most requests ask for (unitType
reads 0 and MillimeterDistanceUnits IS 0, meshRefinement reads 1 and Medium IS 1, isBinaryFormat
reads True), so the read-back answers the same whether the assignment took or was dropped.
"""

import ast
import os

import _corpus
from conftest import TOOLS_DIR


# (module basename without .py, enclosing function) -> why this site is exempt. A reason must state
# a fact about THIS site. Kept honest by test_allowlist_entries_still_trip; the table only shrinks.
_ALLOWLIST = {}

# A name ending in one of these is an export-options object, by the convention the Fusion API and
# this repo share: opts, stl_opts, options, export_options, exportOptions.
_OPTIONS_SUFFIXES = ("opts", "options")


def _call_name(node):
    """The called name bare or through a module: `builtins.setattr(...)` answers 'setattr'."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _pos(node):
    """(line, column) - the source order two nodes in one scope are compared in."""
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _scope_nodes(scope):
    """Every node under `scope` EXCEPT the bodies of nested def/class statements, which are their own
    scopes. A Lambda is deliberately transparent: the house idiom wraps every options write and read
    in one (`safe(lambda: setattr(opts, prop, val))`)."""
    out = []

    def rec(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            out.append(child)
            rec(child)
    rec(scope)
    return out


def _scopes(tree):
    """(name, node) for the module and every function in it, each judged on its OWN body."""
    yield "<module>", tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node


def _prop_key(node):
    """The comparable identity of a property NAME as written - `opts.unitType`,
    `setattr(opts, "unitType", v)` and `getattr(opts, "unitType")` all answer the same key."""
    return ast.dump(node)


def _writes(nodes):
    """[(object name, property key, statement node)] for every property write in the scope, in both
    spellings. The VALUE written is deliberately not read: requiring the read-back to be compared
    against the same expression would answer a differently-spelled comparison with silence."""
    out = []
    for node in nodes:
        if (_call_name(node) == "setattr" and len(node.args) == 3
                and isinstance(node.args[0], ast.Name)):
            out.append((node.args[0].id, _prop_key(node.args[1]), node))
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                out.append((target.value.id, _prop_key(ast.Constant(value=target.attr)), node))
    return out


def _reads_of(node, obj, prop):
    """Every READ of obj.prop in this subtree, in both spellings. A Store target (`o.p = v`) is not
    a read, so the write itself never counts as its own pre-read."""
    hits = []
    for child in ast.walk(node):
        if (_call_name(child) == "getattr" and len(child.args) >= 2
                and isinstance(child.args[0], ast.Name) and child.args[0].id == obj
                and _prop_key(child.args[1]) == prop):
            hits.append(child)
        if (isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load)
                and isinstance(child.value, ast.Name) and child.value.id == obj
                and _prop_key(ast.Constant(value=child.attr)) == prop):
            hits.append(child)
    return hits


def _options_objects(nodes):
    """The names in this scope assigned from a create*Options(...) factory - the export-options
    object at a site named something the suffix convention does not reach."""
    made = set()
    for node in nodes:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            name = _call_name(node.value)
            if (isinstance(target, ast.Name) and name
                    and name.startswith("create") and name.endswith("Options")):
                made.add(target.id)
    return made


def _is_export_options(obj, made):
    return obj in made or any(obj.lower().endswith(s) for s in _OPTIONS_SUFFIXES)


def _consulting_contexts(nodes):
    """The expressions in this scope where a value is CONSULTED as evidence: every comparison, and
    the test of every if/while/conditional expression. A read-back that only reaches a payload key or
    a return value is not here - publishing what the object holds is honest whoever put it there."""
    out = []
    for node in nodes:
        if isinstance(node, ast.Compare):
            out.append(node)
        elif isinstance(node, (ast.If, ast.IfExp, ast.While)):
            out.append(node.test)
    return out


def _offenders(tree):
    """[(enclosing function name, lineno)] for every export-options knob written and read back with
    no pre-read. The function NAME is what an allowlist entry addresses - a line number moves with
    any edit above it."""
    found = []
    for fname, scope in _scopes(tree):
        nodes = _scope_nodes(scope)
        made = _options_objects(nodes)
        contexts = _consulting_contexts(nodes)
        for obj, prop, stmt in _writes(nodes):
            if not _is_export_options(obj, made):
                continue
            where = _pos(stmt)
            if any(_pos(read) < where for node in nodes for read in _reads_of(node, obj, prop)):
                continue                     # the pre-read is there: equality can bite
            # locals that HOLD the read-back, so `landed = getattr(opts, p)` then `if landed != v`
            # reads as one consulted read-back rather than two unrelated statements
            aliases = set()
            for node in nodes:
                if (isinstance(node, ast.Assign) and _pos(node) > where
                        and _reads_of(node.value, obj, prop)):
                    aliases |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            for ctx in contexts:
                if _pos(ctx) < where:
                    continue
                consulted = bool(_reads_of(ctx, obj, prop)) or any(
                    isinstance(n, ast.Name) and n.id in aliases for n in ast.walk(ctx))
                if consulted:
                    found.append((fname, stmt.lineno))
                    break
    return found


def _tool_modules():
    return [fn for fn in sorted(os.listdir(TOOLS_DIR)) if fn.endswith(".py")]


class TestExportKnobsPreRead:
    def test_no_tool_module_verifies_an_export_knob_without_a_pre_read(self):
        offenders = []
        for fn in _tool_modules():
            for fname, lineno in _offenders(_corpus.tree(os.path.join(TOOLS_DIR, fn))):
                if (fn[:-3], fname) in _ALLOWLIST:
                    continue
                offenders.append(f"{fn}:{lineno} (in {fname})")
        assert not offenders, (
            "an export-options property can ALREADY read the value being requested - measured, "
            "unitType reads 0 unset and MillimeterDistanceUnits IS 0, meshRefinement reads 1 unset "
            "and MeshRefinementMedium IS 1, isBinaryFormat reads True unset - so reading it back "
            "after the set answers the same whether the assignment took or was dropped, and the "
            "collision lands on the commonest request every time. Write the knob through "
            "_export.applied_pair, which reads the property BEFORE the set as well and answers "
            "(landed value, changed); what that 'changed' is worth is the knob's own measured "
            "question, and applied_pair's note carries the three answers on file:\n  "
            + "\n  ".join(offenders))

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
                stale.append(f"({mod_name}, {fname}): no longer writes an export knob without a "
                             "pre-read - remove the entry")
        assert not stale, "stale allowlist entries:\n  " + "\n  ".join(stale)
