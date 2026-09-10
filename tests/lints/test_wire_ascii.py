"""Lint: every agent-facing wire string is pure ASCII (CLAUDE.md "Tool descriptions").
The strings cross the wire JSON-encoded with ensure_ascii, so a non-ASCII character becomes a
6-character \\uXXXX escape - use the plain-ASCII spelling (' - ', '...', '->', 'deg'). Three
surfaces: the live registry's descriptions, module-level constants, and ok()/error() literals."""

import ast
import os

import _corpus
from conftest import TOOLS_DIR, register_all_tools


def _tool_files():
    """(filename, path) for every module under tools/ - the source half of the sweep."""
    for fn in sorted(os.listdir(TOOLS_DIR)):
        if fn.endswith(".py"):
            yield fn, os.path.join(TOOLS_DIR, fn)


def _walk_property_descriptions(props, path, out):
    """Recurse into a JSON-schema properties dict, collecting (path, description) pairs (nested
    arrays/objects too)."""
    for name, schema in props.items():
        if not isinstance(schema, dict):
            continue
        desc = schema.get("description")
        if isinstance(desc, str):
            out.append((f"{path}.{name}", desc))
        nested = schema.get("properties")
        if isinstance(nested, dict):
            _walk_property_descriptions(nested, f"{path}.{name}", out)
        items = schema.get("items")
        if isinstance(items, dict) and isinstance(items.get("properties"), dict):
            _walk_property_descriptions(items["properties"], f"{path}.{name}[]", out)


def _input_descriptions(d):
    """(path, description) for every input property of one tool's to_dict()."""
    found = []
    _walk_property_descriptions((d.get("inputSchema") or {}).get("properties", {}) or {},
                                d.get("name"), found)
    return found


def _module_constant_strings(tree):
    """(constant_name, [string literals in its assigned value]) for every module-level assignment -
    one row per assigned NAME, so ``A = B = ...`` and a tuple unpack each answer for themselves.
    The value is WALKED, not literal_eval'd, so an f-string piece, a dict of notes keyed by mode,
    and a description handed straight to an inline ``Tool(...)`` are each checked."""
    out = []
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign) else
                   [node.target] if isinstance(node, ast.AnnAssign) else [])
        if not targets or node.value is None:
            continue
        strings = [n.value for n in ast.walk(node.value)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        if not strings:
            continue
        for target in targets:
            out += [(n.id, strings) for n in ast.walk(target) if isinstance(n, ast.Name)]
    return out


def _ok_error_call_strings(tree):
    """(call_name, lineno, [string literals]) for every ``ok(...)`` / ``error(...)`` call
    (``_common.ok``/``_common.error`` attribute calls too). Every literal in the call subtree is
    collected - f-string pieces, nested dict keys/values - since each crosses the wire."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (
            fn.attr if isinstance(fn, ast.Attribute) else None)
        if name not in ("ok", "error"):
            continue
        strings = [n.value for n in ast.walk(node)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        if strings:
            out.append((name, node.lineno, strings))
    return out


def _non_ascii(text):
    return [(c, hex(ord(c))) for c in text if ord(c) > 127]


# A constant that must hold a non-ASCII character: {(file, CONSTANT): reason}. Keyed by BOTH, so an
# entry covers the one constant that earned it. EMPTY today, and _stale_exemptions keeps it
# shrink-only.
_ASCII_EXEMPT = {}


def _ascii_report(fn, const_name, strings):
    """An offender line for each string carrying a non-ASCII character, unless (fn, const_name)
    is exempt."""
    if (fn, const_name) in _ASCII_EXEMPT:
        return []
    return [f"{fn}: {const_name} has {bad} - "
            f"replace with a plain-ASCII spelling (' - ', '...', '->')"
            for bad in (_non_ascii(s) for s in strings) if bad]


def _stale_exemptions():
    """Entries in _ASCII_EXEMPT that no longer earn their place: no reason, no such file, no such
    constant, or a constant whose text is ASCII again."""
    stale = []
    for (fn, const_name), reason in _ASCII_EXEMPT.items():
        if not (reason or "").strip():
            stale.append(f"{fn}:{const_name}: needs a plain-English reason")
            continue
        path = os.path.join(TOOLS_DIR, fn)
        if not os.path.exists(path):
            stale.append(f"{fn}:{const_name}: no such file - drop the entry")
            continue
        strings = [s for name, ss in _module_constant_strings(_corpus.tree(path))
                   if name == const_name for s in ss]
        if not strings:
            stale.append(f"{fn}:{const_name}: no such module-level constant - drop the entry")
        elif not any(_non_ascii(s) for s in strings):
            stale.append(f"{fn}:{const_name}: the constant is ASCII again - drop the entry")
    return stale


class TestToolDescriptionsAreAscii:
    def test_every_tool_description_is_ascii(self):
        offenders = []
        for it in register_all_tools():
            d = it.to_dict()
            bad = _non_ascii(d.get("description") or "")
            if bad:
                offenders.append(f"{d.get('name')}: description has {bad} - "
                                  f"replace with a plain-ASCII spelling (' - ', '...', '->')")
        assert not offenders, "non-ASCII tool description(s):\n  " + "\n  ".join(offenders)

    def test_every_input_description_is_ascii(self):
        offenders = []
        for it in register_all_tools():
            for path, desc in _input_descriptions(it.to_dict()):
                bad = _non_ascii(desc)
                if bad:
                    offenders.append(f"{path}: input description has {bad} - "
                                      f"replace with a plain-ASCII spelling (' - ', '...', '->')")
        assert not offenders, "non-ASCII input description(s):\n  " + "\n  ".join(offenders)


class TestModuleConstantsAreAscii:
    def test_every_module_constant_is_ascii(self):
        offenders = []
        for fn, path in _tool_files():
            for const_name, strings in _module_constant_strings(_corpus.tree(path)):
                offenders += _ascii_report(fn, const_name, strings)
        assert not offenders, "non-ASCII module constant(s):\n  " + "\n  ".join(offenders)

    def test_the_exemption_table_only_shrinks(self):
        assert _stale_exemptions() == []


class TestRuntimePayloadStringsAreAscii:
    def test_every_ok_error_literal_is_ascii(self):
        offenders = []
        for fn, path in _tool_files():
            for call_name, lineno, strings in _ok_error_call_strings(_corpus.tree(path)):
                for s in strings:
                    bad = _non_ascii(s)
                    if bad:
                        offenders.append(f"{fn}:{lineno}: {call_name}(...) literal has {bad} - "
                                          f"replace with a plain-ASCII spelling (' - ', '...', '->')")
        assert not offenders, "non-ASCII ok()/error() payload literal(s):\n  " + "\n  ".join(offenders)
