# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: every public attribute a SHARED fake exposes exists on its live adsk counterpart, and
every shared fake declares the live type it stands for plus the MEASURED rows behind it.
A fake-shaped class in the shared-fake modules maps to a SHAPES key (by name or by its own
@fusion_fake declaration) or fails; a mapped fake cites row ids that resolve against
measure_api.ROWS; and a tests/unit class named for a measured type stands on the shared fake
instead of doubling it."""

import ast
import importlib
import os
import sys

import _corpus
import conftest
import live_api_facts

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFTEST = os.path.join(TESTS_DIR, "conftest.py")
_FAKES_DIR = os.path.join(TESTS_DIR, "fakes")
_UNIT_DIR = os.path.join(TESTS_DIR, "unit")

sys.path.insert(0, os.path.join(TESTS_DIR, "live"))
import measure_api  # noqa: E402  the claim-id registry - ROWS, one dict per row, keyed 'id'


def _fake_files():
    """Every file the shared fakes live in: the fakes package's family modules plus conftest.py,
    which keeps the harness's own factories. A fake in any of them is in scope here."""
    return [_CONFTEST] + [str(p) for p in sorted(_corpus.py_files(_FAKES_DIR))
                          if os.path.basename(str(p)) != "__init__.py"]


def _fake_module_objects():
    """The imported module behind each file above - the object a declaration is read OFF. Read from
    the defining module, not through conftest's re-export, so a fake reachable only from its own
    family module is still swept."""
    mods = [conftest]
    for path in _fake_files():
        if path != _CONFTEST:
            mods.append(importlib.import_module("tests.fakes." + os.path.basename(path)[:-3]))
    return mods


def _collected(kinds, label):
    """{name: node} over every shared-fake file, REFUSING a name two of them both bind: conftest
    re-exports one object per name, so the other definition drops out of every check here with
    nothing to report."""
    found, owner = {}, {}
    for path in _fake_files():
        for node in _corpus.tree(path).body:
            if not isinstance(node, kinds):
                continue
            assert node.name not in found, (
                f"two shared-fake modules define the {label} '{node.name}' - "
                f"{os.path.basename(owner[node.name])} and {os.path.basename(path)}. Only one "
                "object answers that name through conftest, so the other is invisible to the shape "
                "sweep: rename one, or merge them into the family module that owns the type.")
            found[node.name], owner[node.name] = node, path
    return found


def _conftest_classes():
    """class name -> its ClassDef, for every class at a shared-fake module's scope. Every check here
    starts from this map, and _corpus parses each file once for the whole run."""
    return _collected(ast.ClassDef, "class")


def _conftest_functions():
    """function name -> its FunctionDef, for every def at a shared-fake module's scope - the defs
    that construct the fakes above."""
    return _collected((ast.FunctionDef, ast.AsyncFunctionDef), "def")


def _own_declaration(obj):
    """The @fusion_fake declaration an object carries ITSELF, or None.

    ``vars(obj)`` rather than a getattr is the load-bearing part: the attribute is INHERITED, so a
    subclass that declares nothing (FakeSetup extends FakeCAMFolder) would otherwise answer its
    base's declaration - swept against the base's live type, on provenance it never stated.
    """
    return vars(obj).get("__fusion_fake__")


def declarations():
    """{name: its @fusion_fake declaration} for every shared-fake class/def carrying one of its OWN.

    Read off the objects, not the source: the decorator records structured data, so there is
    nothing to parse.
    """
    found = {}
    names = list(_conftest_classes()) + list(_conftest_functions())
    for module in _fake_module_objects():
        for name in names:
            obj = getattr(module, name, None)
            declaration = _own_declaration(obj) if obj is not None else None
            if declaration is not None:
                found[name] = declaration
    return found


def _declared_map(decls=None):
    """{fake: the SHAPES key its declaration names} - the map's manual half, carried by the fakes
    themselves rather than by a table beside them."""
    decls = declarations() if decls is None else decls
    return {name: d["live_type"] for name, d in decls.items() if d.get("live_type")}

# fake.attr -> one-line reason a live-absent attribute is tolerated. Shrink-only.
_ALLOWLIST = {}

# Fake-shaped classes in the shared-fake modules with NO live SHAPES dump to sweep against yet.
# Shrink-only: the staleness check fails the moment a dump lands (auto-map then takes over) or the
# class goes.
_UNMAPPED_OK = {
    "_MeshBodies": "MeshBodies has no SHAPES dump; the one member the fake takes a position on is "
                   "itemByName, which it DROPS off the measured meshbodies-no-itembyname row "
                   "(BEHAVIOR['meshbodies_has_itembyname']) rather than asserting a surface",
}


def _stripped(name):
    """The class name with a leading underscore and a Fake/Make prefix removed - the name the
    fake impersonates (_FakeObjectCollection -> ObjectCollection, MakeDesign -> Design)."""
    base = name.lstrip("_")
    for prefix in ("Fake", "Make"):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    return base


def _auto_mapped(class_names, shapes):
    """{conftest class: SHAPES key} for every class whose stripped name IS a SHAPES key."""
    return {n: _stripped(n) for n in class_names if _stripped(n) in shapes}


def _is_fake_shaped(name, live_names):
    """Fake/_Fake-prefixed, the Make prefix conftest's builder fakes use, or the bare name of a
    live adsk type with or without a leading underscore (_MeshBodies stands for MeshBodies)."""
    return (name.startswith("Fake") or name.startswith("_Fake") or name.startswith("Make")
            or name.lstrip("_") in live_names)


def _live_type_names():
    """Every live adsk class name a bare conftest class could be standing in for: the MEASURED
    SHAPES keys plus every class in the generated api_surface dump. Without the api_surface half a
    fake named exactly like a live type that has no shape dump YET is invisible to the completeness
    gate. Only the DISCRIMINATOR widens - the auto-map still keys on SHAPES."""
    import api_surface
    names = set(live_api_facts.SHAPES)
    for table in (api_surface.PROPERTIES, api_surface.FACTORIES):
        for key in table:
            names.add(key.rsplit(".", 1)[-1])
    return names


def _unmapped_fakes(class_names, shapes, manual, allowlist, live_names=None):
    """Fake-shaped conftest classes the sweep would silently skip: neither manually mapped, nor
    auto-mapped, nor excused by the allowlist. `live_names` is the discriminator's name set
    (defaults to `shapes`); the auto-map always keys on `shapes`."""
    auto = _auto_mapped(class_names, shapes)
    live_names = shapes if live_names is None else live_names
    return [n for n in sorted(class_names)
            if _is_fake_shaped(n, live_names)
            and n not in manual and n not in auto and n not in allowlist]


def _conftest_bases(cls_node, classes):
    """The bases of a fake that are themselves conftest classes - their members are inherited
    and so are exposed too."""
    return [classes[b.id] for b in cls_node.bases
            if isinstance(b, ast.Name) and b.id in classes and classes[b.id] is not cls_node]


def _public_surface(cls_node, classes=None):
    """Public attribute names a fake class exposes: methods, class-level assigns, self.<name>
    assignments anywhere in its methods, and everything a conftest base hands down."""
    names = set()
    for node in cls_node.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and not t.id.startswith("_"):
                    names.add(t.id)
    for node in ast.walk(cls_node):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                        and t.value.id == "self" and not t.attr.startswith("_")):
                    names.add(t.attr)
        elif isinstance(node, ast.Tuple):
            for el in node.elts:
                if (isinstance(el, ast.Attribute) and isinstance(el.value, ast.Name)
                        and el.value.id == "self" and not el.attr.startswith("_")):
                    names.add(el.attr)
    if classes:
        for base in _conftest_bases(cls_node, classes):
            names |= _public_surface(base, classes)
    return names


def _effective_map(class_names):
    """The full sweep map: auto-derived entries plus every declared live type (a declaration wins
    on overlap)."""
    mapping = _auto_mapped(class_names, live_api_facts.SHAPES)
    mapping.update(_declared_map())
    return mapping


class TestSharedFakeShapesExist:
    def test_every_shared_fake_attribute_exists_live(self):
        classes = _conftest_classes()
        offenders = []
        for fake, live in sorted(_effective_map(classes).items()):
            assert fake in classes, f"mapped fake {fake} not found in the shared-fake modules"
            shape = live_api_facts.SHAPES.get(live)
            assert shape, (
                f"SHAPES has no '{live}' - add it to a shape-dump measurement row and regenerate "
                "(py -3 tests/live/measure_api.py with Fusion up)")
            for attr in sorted(_public_surface(classes[fake], classes)):
                if attr in shape or f"{fake}.{attr}" in _ALLOWLIST:
                    continue
                offenders.append(f"{fake}.{attr} does not exist on live {live}")
        assert not offenders, (
            "A shared fake exposes attributes its live type does not have - the fake teaches an "
            "API that will AttributeError in Fusion. Rename/remove the attribute, or if the live "
            "surface genuinely changed, re-run the probes and commit the regenerated facts:\n  "
            + "\n  ".join(offenders))

    def test_every_fake_shaped_class_is_mapped(self):
        # The completeness gate: a NEW shared fake that maps to nothing is a silently-unswept
        # mock - it must map (rename it so the stripped name hits a SHAPES key, declare its live
        # type, or measure the missing live type), never just be left out.
        classes = _conftest_classes()
        live = _live_type_names()
        # the discriminator's two name sources: narrow it to either half alone and a bare shadow of
        # a live type drops out of this gate with nothing to report.
        assert set(live_api_facts.SHAPES) <= live and "MeshRepairFeature" in live
        unmapped = _unmapped_fakes(classes, live_api_facts.SHAPES, _declared_map(), _UNMAPPED_OK,
                                   live_names=live)
        assert not unmapped, (
            "fake-shaped shared classes the shape sweep would silently skip - map each to a "
            "SHAPES key (auto: name it after the live type; or declare @fusion_fake(live_type=...) "
            "on it; or shape-dump the live type), or add a reasoned _UNMAPPED_OK entry:\n  "
            + "\n  ".join(unmapped))

    def test_the_sweep_map_carries_the_declared_fakes(self):
        # The map is two halves and the MERGE is load-bearing: a fake whose stripped name is no
        # SHAPES key reaches the sweep ONLY through its declaration, so a map that dropped the
        # declared half would quietly stop sweeping every one of them, and no other check would
        # report it.
        classes = _conftest_classes()
        mapping = _effective_map(classes)
        auto = _auto_mapped(classes, live_api_facts.SHAPES)
        for fake, live in (("FakePoint", "Point3D"), ("MakeComp", "Component"),
                           ("_NamedCollection", "BRepBodies")):
            assert fake not in auto, f"{fake} auto-maps now - this check needs a declared-ONLY fake"
            assert mapping.get(fake) == live, (
                f"{fake} is swept only because its declaration names {live} - the declared half of "
                "the map is not merged in")

    def test_unmapped_ok_entries_still_trip(self):
        classes = _conftest_classes()
        stale = []
        for name, reason in _UNMAPPED_OK.items():
            assert reason.strip(), f"{name} _UNMAPPED_OK entry needs a plain-English reason"
            if name not in classes:
                stale.append(f"{name}: no such shared-fake class - remove the entry")
            elif _stripped(name) in live_api_facts.SHAPES:
                stale.append(f"{name}: '{_stripped(name)}' now has a SHAPES dump - the auto-map "
                             "sweeps it; remove the entry")
            elif name in _declared_map():
                stale.append(f"{name}: declares a @fusion_fake live_type - remove the entry")
        assert not stale, "stale _UNMAPPED_OK entries:\n  " + "\n  ".join(stale)

    def test_allowlist_entries_still_trip(self):
        classes = _conftest_classes()
        stale = []
        for key, reason in _ALLOWLIST.items():
            assert reason.strip(), f"{key} allowlist entry needs a plain-English reason"
            fake, attr = key.split(".", 1)
            if fake not in classes or attr not in _public_surface(classes[fake], classes):
                stale.append(f"{key}: the fake no longer exposes it - remove the entry")
            elif attr in live_api_facts.SHAPES.get(_effective_map(classes).get(fake, ""), ()):
                stale.append(f"{key}: the attribute exists live - remove the entry")
        assert not stale, "stale allowlist entries:\n  " + "\n  ".join(stale)


# The provenance arm, over the same inventory: a declaration is STRUCTURED DATA on the fake
# (@fusion_fake(live_type=..., facts=(...))), never prose. It states exactly one kind, its facts
# resolve against measure_api.ROWS, a mapped fake declares a live type and at least one row, and a
# def that CONSTRUCTS a declared fake says which one.

_KINDS = ("live_type", "factory_for", "scenario_double")


def claim_ids():
    """Every claim id the live measurement registry defines."""
    return {row["id"] for row in measure_api.ROWS}


def _kind_problems(decls):
    out = []
    for name, declaration in sorted(decls.items()):
        # a blank string is not a classification: the scenario_double kind IS its reason
        kinds = [k for k in _KINDS if str(declaration.get(k) or "").strip()]
        if len(kinds) != 1:
            out.append(f"{name}: a declaration is exactly one of {'/'.join(_KINDS)} - this one is "
                       f"{kinds or 'none of them'} (a scenario_double needs its reason string)")
            continue
        if declaration.get("scenario_double") and declaration.get("facts"):
            out.append(f"{name}: classified as standing for no live type, so it can cite no "
                       "measurement row - put the facts on the fake they measure")
        target = declaration.get("factory_for")
        if target and target not in decls:
            out.append(f"{name}: factory_for names '{target}', which carries no @fusion_fake "
                       "declaration of its own - it names the declared fake this def constructs")
    return out


def _unresolved_facts(decls, known):
    """An id that never entered the registry and one whose row was renamed read the same here."""
    return [f"{name}: '{fact}' is no row id in measure_api.ROWS"
            for name, declaration in sorted(decls.items())
            for fact in declaration.get("facts", ()) if fact not in known]


def _unbacked_mapped(mapping, auto, decls):
    """A mapped fake that declares nothing, declares a live type its own auto-mapped NAME
    contradicts, or names no measurement row. A declaration wins the merge, so the contradiction
    is judged against `auto`, not against `mapping`."""
    out = []
    for fake, live in sorted(mapping.items()):
        declaration = decls.get(fake)
        if declaration is None:
            out.append(f"{fake}: the shape sweep maps it onto live {live}, but it declares no "
                       "@fusion_fake - name the live type and the row(s) that measured it")
        elif declaration.get("live_type") != auto.get(fake, declaration.get("live_type")):
            out.append(f"{fake}: declares live_type {declaration.get('live_type')!r} while its "
                       f"NAME maps it onto {auto[fake]!r} - one of the two is wrong")
        elif not declaration.get("facts"):
            out.append(f"{fake}: declares live {live} and no measurement row - cite the row that "
                       "measured what it encodes")
    return out


def _constructed(node, names):
    return {call.func.id for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            and call.func.id in names}


def _undeclared_factories(functions, decls):
    fakes = {n for n, d in decls.items() if d.get("live_type") or d.get("scenario_double")}
    out = []
    for name, node in sorted(functions.items()):
        built = _constructed(node, fakes)
        if built and name not in decls:
            out.append(f"{name}: constructs {', '.join(sorted(built))} - declare "
                       "@fusion_fake(factory_for=...) naming the fake it constructs")
    return out


class TestApiFactProvenance:
    def test_every_declaration_is_well_formed(self):
        bad = _kind_problems(declarations())
        assert not bad, (
            "@fusion_fake declarations on a shared fake that state no single classification:\n  "
            + "\n  ".join(bad))

    def test_every_declared_fact_resolves_against_the_registry(self):
        bad = _unresolved_facts(declarations(), claim_ids())
        assert not bad, (
            "shared fakes cite claim ids measure_api.py does not carry, so nothing fails when "
            "the claim outlives the measurement. Cite the row that backs it "
            "(VERIFIED_API_FACTS.md's 'encoded in' column usually names the fake) - or, when no row "
            "carries the claim, add a measurement row and measure it live (py -3 "
            "tests/live/measure_api.py with Fusion up):\n  " + "\n  ".join(bad))

    def test_every_mapped_fake_declares_its_live_type_and_a_row(self):
        classes = _conftest_classes()
        bad = _unbacked_mapped(_effective_map(classes),
                               _auto_mapped(classes, live_api_facts.SHAPES),
                               declarations())
        assert not bad, (
            "shared fakes the shape sweep maps onto a live type without saying what measured "
            "them:\n  " + "\n  ".join(bad))

    def test_every_factory_of_a_declared_fake_is_declared(self):
        bad = _undeclared_factories(_conftest_functions(), declarations())
        assert not bad, (
            "shared-fake defs construct a declared fake without naming it, so they sit outside the "
            "inventory the shape sweep and the rules above run on:\n  " + "\n  ".join(bad))

    def test_the_declarations_read_the_real_conftest_fakes(self):
        # a reader that found nothing, or read some other attribute, would pass every rule above
        # green over any set of fake modules at all.
        decls = declarations()
        assert decls, "no shared fake carries a @fusion_fake declaration - the reader is dead"
        assert decls["FakeOccurrence"]["live_type"] == "Occurrence"
        assert "shape-dump-design-world" in decls["FakeOccurrence"]["facts"]
        assert decls["body_proxy"]["factory_for"] == "_OccurrenceProxy"
        assert decls["_EntityProxy"]["scenario_double"].strip()


# The reuse arm, over tests/unit: the two arms above sweep conftest's fakes, and a unit test that
# hand-rolls its own double of a MEASURED type is exactly the shape they cannot see.


def _shared_unit_classes():
    """{class name: ClassDef} for the tests/unit/_*.py fake modules other test files import from -
    a base defined there is followed like one defined in the file itself."""
    found = {}
    for path in _corpus.py_files(_UNIT_DIR):
        if os.path.basename(path).startswith("_"):
            found.update({n.name: n for n in _corpus.tree(path).body
                          if isinstance(n, ast.ClassDef)})
    return found


def _base_refs(cls_node):
    """(name, dotted) per base: a bare `Sketch` reads ('Sketch', False) and a `conftest.Sketch`
    ('Sketch', True) - the dotted one names the module attribute itself, so no binding can shadow
    it, while the bare one is only ever whatever this file bound that name to."""
    refs = []
    for base in cls_node.bases:
        if isinstance(base, ast.Name):
            refs.append((base.id, False))
        elif (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
              and base.value.id == "conftest"):
            refs.append((base.attr, True))
    return refs


def _conftest_bindings(tree, conftest, shared):
    """The names this file BINDS to a shared fake: `from conftest import X [as Y]`, the same import
    straight from `tests.fakes.<family>`, `_Y = X` over one of those, `_Y = conftest.X`, and an
    import of a tests/unit/_*.py fake that itself stands on one. A name any OTHER import, def or
    assignment binds is dropped, so a base is judged by what the file bound it to and never by the
    shared class it happens to be SPELLED like."""
    bound, taken = {}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                name = imported.asname or imported.name
                module = node.module or ""
                if (((module == "conftest" or module.startswith("tests.fakes."))
                     and imported.name in conftest)
                        or module.startswith("_") and imported.name in shared):
                    bound[name] = imported.name
                else:
                    taken.add(name)
        elif isinstance(node, ast.Import):
            for imported in node.names:
                taken.add((imported.asname or imported.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            taken.add(node.name)
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target, value = node.targets[0], node.value
        pairs = (list(zip(target.elts, value.elts))
                 if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple)
                 else [(target, value)])
        for name, expr in pairs:
            if not isinstance(name, ast.Name):
                continue
            if isinstance(expr, ast.Name) and expr.id in bound and expr.id not in taken:
                bound[name.id] = bound[expr.id]
            elif (isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name)
                  and expr.value.id == "conftest" and expr.attr in conftest):
                bound[name.id] = expr.attr
            else:
                taken.add(name.id)
    return set(bound) - taken


def _named_classes(tree):
    """Return every in-scope ClassDef with its separate lexical scope."""
    found = []

    def visit(node, scope):
        child_scope = scope
        if isinstance(node, ast.ClassDef):
            # A class defined directly in another class body belongs to that namespace and remains
            # with its owner; classes inside methods are function locals and stay in the inventory.
            if not scope or scope[-1][0] != "class":
                found.append((node, scope))
            child_scope = scope + (("class", node),)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            child_scope = scope + (("function", node),)
        for child in ast.iter_child_nodes(node):
            visit(child, child_scope)

    visit(tree, ())
    return found


def _scope_map(inventory):
    """Return lexical scope by ClassDef identity without changing cached AST nodes."""
    return {id(node): scope for node, scope in inventory}


def _function_scope(scope):
    """Return the enclosing function identities that participate in lexical lookup."""
    return tuple(node for kind, node in scope if kind == "function")


def _lexical_base(name, cls_node, inventory, scopes):
    """Return the nearest class binding in the definition's lexical context."""
    current = _function_scope(scopes[id(cls_node)])
    candidates = []
    for node, scope in inventory:
        if node is cls_node or node.name != name:
            continue
        held = _function_scope(scope)
        if held == current and node.lineno >= cls_node.lineno:
            continue
        if len(held) <= len(current) and current[:len(held)] == held:
            candidates.append((len(held), node.lineno, node))
    return max(candidates, default=(0, 0, None), key=lambda row: row[:2])[2]


def _stands_on_conftest(cls_node, inventory, scopes, bound, conftest, seen=()):
    """Return whether one class inherits a shared fake in its lexical context."""
    marker = id(cls_node)
    if marker in seen:
        return False
    for base, dotted in _base_refs(cls_node):
        if dotted:
            if base in conftest:
                return True
            continue
        local = _lexical_base(base, cls_node, inventory, scopes)
        if local is not None:
            if _stands_on_conftest(local, inventory, scopes, bound, conftest,
                                   seen + (marker,)):
                return True
        elif base in bound:
            return True
    return False


def _standing_shared(conftest):
    """Return importable helper-fake names that stand on a shared fake in their own file."""
    standing = set()
    for path in _corpus.py_files(_UNIT_DIR):
        if not os.path.basename(path).startswith("_"):
            continue
        tree = _corpus.tree(path)
        own = _named_classes(tree)
        scopes = _scope_map(own)
        bound = _conftest_bindings(tree, conftest, set())
        standing |= {node.name for node, _scope in own
                     if _stands_on_conftest(node, own, scopes, bound, conftest)}
    return standing


def _class_label(node, scope, repeated):
    """Return a diagnostic class label, qualifying names that occur more than once."""
    if not repeated:
        return node.name
    names = [held.name for _kind, held in scope] + [node.name]
    return ".".join(names) + " (line " + str(node.lineno) + ")"


def _local_doubles(shapes, conftest):
    """Return local measured-type doubles that do not stand on a shared fake."""
    shared = _standing_shared(conftest)
    out = []
    for path in _corpus.py_files(_UNIT_DIR):
        tree = _corpus.tree(path)
        own = _named_classes(tree)
        scopes = _scope_map(own)
        bound = _conftest_bindings(tree, conftest, shared)
        counts = {node.name: sum(other.name == node.name for other, _scope in own)
                  for node, _scope in own}
        for node, scope in sorted(own, key=lambda row: (row[0].name, row[0].lineno)):
            live = _stripped(node.name)
            if live in shapes and not _stands_on_conftest(
                    node, own, scopes, bound, conftest):
                out.append((os.path.basename(path),
                            _class_label(node, scope, counts[node.name] > 1), live))
    return out


class TestUnitFakesStandOnTheSharedOnes:
    def test_no_unit_test_hand_rolls_a_double_of_a_measured_type(self):
        offenders = _local_doubles(live_api_facts.SHAPES, _conftest_classes())
        assert not offenders, (
            "unit tests define their own double of a type live Fusion was MEASURED for, so the "
            "double teaches a surface nothing swept - import the shared fake from conftest, or "
            "subclass it and add only the extra the test needs:\n  "
            + "\n  ".join(f"{f}: {c} doubles live {live}" for f, c, live in offenders))

    def test_the_scan_reads_the_real_unit_tests(self):
        # a scan that found no candidate, or a base walk answering the same for everything, would
        # pass the gate above green over any tests/unit at all.
        conftest = _conftest_classes()
        tree = ast.parse("from conftest import Sketch as _S\nfrom elsewhere import Plane\n"
                         "class A(_S): pass\nclass B: pass\nclass C(Plane): pass")
        bound = _conftest_bindings(tree, conftest, set())
        own = _named_classes(tree)
        scopes = _scope_map(own)
        derived, plain, collided = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        assert _stands_on_conftest(derived, own, scopes, bound, conftest)
        assert not _stands_on_conftest(plain, own, scopes, bound, conftest)
        # the hardening: `Plane` is a conftest class NAME, but this file bound it elsewhere
        assert not _stands_on_conftest(collided, own, scopes, bound, conftest)
        assert _shared_unit_classes(), "tests/unit/_*.py defines no class - the file walk is dead"
        in_scope = [n.name for path in _corpus.py_files(_UNIT_DIR)
                    for n in ast.walk(_corpus.tree(path))
                    if isinstance(n, ast.ClassDef) and _stripped(n.name) in live_api_facts.SHAPES]
        assert in_scope, "no tests/unit class is named for a measured type - the scope is dead"
