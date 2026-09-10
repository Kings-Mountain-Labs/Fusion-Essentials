# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Resolve, per tool module, which local variables hold a FeatureInput and of what class.

input_scopes() is the entry point for test_input_property_names and test_bool_returns_checked.
Scopes are per-function and carry enclosing bindings: sibling handlers reuse the name 'inp'."""

import ast
import os

import _corpus
import api_surface
from conftest import TOOLS_DIR

# A collection attribute reads `<lowerCamel>Features`/`<lowerCamel>s`; its class is the same name
# with an initial capital. Only fusion/cam collections are resolved - a core factory is reached the
# same way and falls through to the same lookup.
_MODULES = ("fusion", "cam", "core", "drawing")


# 'module.Collection' for every collection that declares a factory returning a FeatureInput.
_COLLECTIONS = frozenset(k.rsplit(".", 1)[0] for k in api_surface.FACTORIES)


def _factories_by_method():
    out = {}
    for key, cls in api_surface.FACTORIES.items():
        out.setdefault(key.rsplit(".", 1)[1], set()).add(cls)
    return out


# The factory methods whose declarations ALL return the SAME input class - what resolves a
# collection reached through a PARAMETER, since nothing in its own file binds one. The filter reads
# RETURNED CLASSES, so `createInput`, whose declarations disagree, names no class.
_UNIQUE_FACTORIES = {m: next(iter(v)) for m, v in _factories_by_method().items() if len(v) == 1}


def _collection_class(attr_name):
    """'meshCombineFeatures' -> the 'module.MeshCombineFeatures' key api_surface knows, or None."""
    cls = attr_name[:1].upper() + attr_name[1:]
    for mod in _MODULES:
        key = f"{mod}.{cls}"
        if key in _COLLECTIONS:
            return key
    return None


def _unwrap(node):
    """The expression inside `safe(lambda: <expr>)` / `safe(lambda: <expr>, default)`, else node.
    Feature collections are routinely fetched through safe(), and the class is still knowable."""
    while isinstance(node, ast.Call):
        fname = node.func.attr if isinstance(node.func, ast.Attribute) else \
            getattr(node.func, "id", None)
        if fname != "safe" or not node.args:
            break
        first = node.args[0]
        node = first.body if isinstance(first, ast.Lambda) else first
    return node


def _collection_vars(nodes):
    """variable -> 'module.Collection' for every `x = <...>.<someFeatures>` in ONE scope. Scoped,
    not module-wide: two handlers in one file both call their collection 'feats'."""
    out = {}
    for node in nodes:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        value = _unwrap(node.value)
        if isinstance(value, ast.Attribute):
            coll = _collection_class(value.attr)
            if coll:
                out[node.targets[0].id] = coll
    return out


def _factory_target(call, coll_vars=None):
    """The 'module.Class' a `<collection>.createInput(...)` call returns, or None. The collection is
    reached either inline (`comp.features.meshRepairFeatures.createInput`) or through a local
    variable bound earlier in the module (`feats = comp.features.meshRepairFeatures`) - and where
    neither reads, through _UNIQUE_FACTORIES on the method name alone."""
    call = _unwrap(call)
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        return None
    method = call.func.attr
    owner = call.func.value
    coll = None
    if isinstance(owner, ast.Attribute):
        coll = _collection_class(owner.attr)
    elif isinstance(owner, ast.Name) and coll_vars:
        coll = coll_vars.get(owner.id)
    if coll is None:
        return _UNIQUE_FACTORIES.get(method)
    return api_surface.FACTORIES.get(f"{coll}.{method}")


def _iter_tool_files():
    for name in sorted(os.listdir(TOOLS_DIR)):
        if name.endswith(".py") and name != "__init__.py":
            yield name, os.path.join(TOOLS_DIR, name)


# The name-by-string forms: (callable, index of the object arg, index of the property-name arg).
# set_verified passes the property name as a STRING, so an attribute-only walk would be blind to it.
_STRING_FORMS = {"set_verified": (0, 1), "setattr": (0, 1)}


def _named_assignments_in(nodes):
    """[(lineno, var, prop)] over BOTH forms: `var.prop = ...` and set_verified/setattr(var, 'prop')."""
    out = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name):
                    out.append((tgt.lineno, tgt.value.id, tgt.attr))
        elif isinstance(node, ast.Call):
            fname = node.func.attr if isinstance(node.func, ast.Attribute) else \
                getattr(node.func, "id", None)
            spec = _STRING_FORMS.get(fname)
            if spec is None:
                continue
            obj_i, prop_i = spec
            if len(node.args) <= max(obj_i, prop_i):
                continue
            obj, prop = node.args[obj_i], node.args[prop_i]
            if isinstance(obj, ast.Name) and isinstance(prop, ast.Constant) \
                    and isinstance(prop.value, str):
                out.append((node.lineno, obj.id, prop.value))
    return out


def _scopes(tree):
    """[(nodes, enclosing_nodes)] - one entry per function body plus the module's, each body
    EXCLUDING functions nested inside it (they become entries of their own) but carrying everything
    lexically enclosing it: sibling handlers reuse a local name ('inp', 'feats') for a DIFFERENT
    object, and a handler routinely binds the collection then works in a nested closure."""
    scopes = []

    def collect(body, enclosing):
        nodes, nested = [], []
        for stmt in body:
            stack = [stmt]
            while stack:
                node = stack.pop()
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nested.append(node)
                    continue          # its body belongs to its own scope
                nodes.append(node)
                stack.extend(ast.iter_child_nodes(node))
        scopes.append((nodes, enclosing))
        for fn in nested:
            collect(fn.body, enclosing + nodes)

    collect(tree.body, [])
    return scopes


def _builder_returns(tree):
    """function name -> {position: 'module.Class'} for every function in the module that BUILDS a
    FeatureInput and hands it back; `position` is the index in a returned TUPLE, None for a bare
    return. `return holes.createSimpleInput(d), None` is this repo's builder idiom, so the caller
    binds the input through a TUPLE target."""
    out = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        found = {}
        stack = list(fn.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue          # its returns belong to that function's own entry
            if isinstance(node, ast.Return) and node.value is not None:
                value = _unwrap(node.value)
                if isinstance(value, ast.Tuple):
                    for i, elt in enumerate(value.elts):
                        cls = _factory_target(elt)
                        if cls:
                            found[i] = cls
                else:
                    cls = _factory_target(value)
                    if cls:
                        found[None] = cls
            stack.extend(ast.iter_child_nodes(node))
        if found:
            out[fn.name] = found
    return out


def _returned_positions(value, builders):
    """{position: 'module.Class'} for whatever `value` evaluates to - a call to a local builder, or
    a tuple written out at the assignment itself. Empty when nothing there builds an input."""
    value = _unwrap(value)
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return builders.get(value.func.id) or {}
    out = {}
    if isinstance(value, ast.Tuple):
        for i, elt in enumerate(value.elts):
            cls = _factory_target(elt)
            if cls:
                out[i] = cls
    return out


def _bindings(nodes, coll_vars, builders=None):
    """variable -> 'module.Class' for every FeatureInput bound in ONE scope, through any of the
    three shapes a tool uses: `inp = <collection>.createXInput(...)`, `inp = _build(...)` where
    _build returns one, and `inp, err = _build(...)` where it returns the (input, error) pair."""
    builders = builders or {}
    bound = {}
    for node in nodes:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name):
            cls = _factory_target(node.value, coll_vars) \
                or _returned_positions(node.value, builders).get(None)
            if cls:
                bound[target.id] = cls
        elif isinstance(target, (ast.Tuple, ast.List)):
            positions = _returned_positions(node.value, builders)
            for i, elt in enumerate(target.elts):
                cls = positions.get(i)
                if cls and isinstance(elt, ast.Name):
                    bound[elt.id] = cls
    return bound


def input_scopes(tree):
    """(nodes, {var: 'module.Class'}) for every scope in ONE module that binds a FeatureInput - the
    one place both lints over this corpus decide what holds an input."""
    builders = _builder_returns(tree)
    for nodes, enclosing in _scopes(tree):
        visible = enclosing + nodes
        bound = _bindings(visible, _collection_vars(visible), builders)
        if bound:
            yield nodes, bound


def _offenders_in(path):
    """[(line, var, prop, class)] for every property assigned onto a resolved input whose name is
    not a real member of that input's class."""
    offenders = []
    for nodes, bound in input_scopes(_corpus.tree(path)):
        for lineno, var, prop in _named_assignments_in(nodes):
            cls = bound.get(var)
            if cls is None:
                continue
            members = api_surface.PROPERTIES.get(cls)
            if not members:
                # An input class the table does not carry would make every property on it
                # unverifiable. Report it rather than skip: silence here is the whole failure mode.
                offenders.append((lineno, var, prop, cls))
                continue
            if prop not in members:
                offenders.append((lineno, var, prop, cls))
    return offenders


