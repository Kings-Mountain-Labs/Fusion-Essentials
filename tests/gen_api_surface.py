# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Generate tests/api_surface.py - every adsk class's real property names, plus what each
`createInput` factory returns.

A SWIG proxy ACCEPTS an assignment to a name it does not define: the value lands on a dead Python
attribute, the object keeps its API default, and nothing raises. So a misspelled input property
runs the DEFAULT operation while the tool reports the requested one, and no runtime check can catch
it. Comparing the assigned name against the target class's real member list is the only defence,
and that needs the member list as data - which is what this writes.

Regenerate: py -3 tests/gen_api_surface.py   (reads the installed Fusion Python bindings)
"""

import argparse
import ast
import glob
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(REPO_ROOT, "tests", "api_surface.py")

_BINDING_GLOBS = (
    os.path.expanduser("~/AppData/Local/Autodesk/webdeploy/production/*/Api/Python/packages/adsk"),
    os.path.expanduser("~/AppData/Local/Autodesk/webdeploy/pre-production/*/Api/Python/packages/adsk"),
    os.path.expanduser("~/Library/Application Support/Autodesk/webdeploy/production/*/Autodesk "
                       "Fusion 360.app/Contents/Api/Python/packages/adsk"),
)

_MODULES = ("core", "fusion", "cam", "drawing")

# Classes kept BESIDE the factory-return set below. A create* factory hands back an input object,
# which is how most classes earn their row; a COLLECTION (its createInput/add/itemsByEntities
# contract) and a LEAF record reached only by walking one are never returned by a create* method,
# so nothing else pulls them in. Named here they get the same real-member list, which is what a
# test fake impersonating them is checked against.
_MEASURED_FACTORY_RETURNS = {
    # 'module.Class.method' -> (the class a LIVE call hands back, the Fusion build it was read on).
    #
    # A binding's return annotation is hand-maintained text and can name the wrong class. FACTORIES
    # is consumed as a statement about the object a tool actually receives, so where a live call
    # disagrees with the annotation the MEASURED class wins. A row is legitimate only with a
    # measurement behind it - never a correction that merely looks right - and _apply_measured_returns
    # raises on a row the bindings have since fixed or dropped, so the table cannot outlive its defect.
    #
    # Both rows below: classType() read off the returned object on Fusion 2705.0.108. The bindings
    # annotate each as adsk.core.Point2D, which is the CENTER ARGUMENT's class, not the return.
    "core.Arc2D.createByCenter": ("core.Arc2D", "Fusion 2705.0.108"),
    "core.Circle2D.createByCenter": ("core.Circle2D", "Fusion 2705.0.108"),
}

_EXTRA_CLASSES = (
    "fusion.PMIAnnotations",
    "fusion.PMILeaderLineNotes",
    "fusion.PMIHoleThreadNotes",
    "fusion.PMIDatumReference",
    "fusion.PMIDatumModifier",
    "fusion.PMIDatumTarget",
    "fusion.PMIRoughness",
)


def find_bindings(bindings_dir=None):
    """Return an explicit adsk package directory, or the newest installed one by default."""
    if bindings_dir is not None:
        root = os.path.abspath(os.path.expanduser(bindings_dir))
        missing = [name + ".py" for name in _MODULES
                   if not os.path.isfile(os.path.join(root, name + ".py"))]
        if not os.path.isdir(root) or missing:
            detail = ("directory does not exist" if not os.path.isdir(root) else
                     "missing required modules: " + ", ".join(missing))
            raise SystemExit("Explicit --bindings-dir is invalid: " + root + " (" + detail + ")")
        return root
    hits = []
    for pattern in _BINDING_GLOBS:
        hits.extend(glob.glob(pattern))
    hits = [h for h in hits if os.path.isdir(h)]
    if not hits:
        return None
    return max(hits, key=os.path.getmtime)


def _returns_bool(node):
    """True when a def's return annotation is exactly "bool"."""
    ann = node.returns
    if not isinstance(ann, ast.Constant) or not isinstance(ann.value, str):
        return False
    return ann.value.replace("*", "").strip() == "bool"


def _return_class(node):
    """The 'adsk.<mod>.<Class>' a def's return annotation names, or None. The bindings annotate as a
    string literal ("adsk.fusion.MeshCombineFeatureInput"), sometimes with a trailing ' *'."""
    ann = node.returns
    if not isinstance(ann, ast.Constant) or not isinstance(ann.value, str):
        return None
    text = ann.value.replace("*", "").strip()
    m = re.match(r"^adsk\.(core|fusion|cam|drawing)\.(\w+)$", text)
    return f"{m.group(1)}.{m.group(2)}" if m else None


def read_declarations(path):
    """Return exact source bytes and their parsed declaration tree without importing the module."""
    with open(path, "rb") as fh:
        raw = fh.read()
    return raw, ast.parse(raw.decode("utf-8-sig"), filename=str(path))


def scan_module(path, module_name):
    """(properties, factories, bool_methods, other_methods) for one bindings module.

    properties: 'fusion.MeshCombineFeatureInput' -> sorted real member names. A SWIG property is
    declared as `def _get_x` / `def _set_x`; plain methods are included too, since assigning over a
    method name is just as dead as assigning a typo.
    factories:  'fusion.MeshCombineFeatures.createInput' -> 'fusion.MeshCombineFeatureInput'.
    """
    _, tree = read_declarations(path)
    properties, factories, bools, other = {}, {}, set(), set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        key = f"{module_name}.{node.name}"
        names = set()
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                m = re.match(r"^_(?:get|set)_(\w+)$", item.name)
                if m:
                    names.add(m.group(1))
                elif not item.name.startswith("__"):
                    names.add(item.name)
                    rc = _return_class(item)
                    if rc is not None:
                        factories[f"{key}.{item.name}"] = rc
                        other.add(item.name)
                    elif _returns_bool(item):
                        bools.add(item.name)
                    elif item.returns is not None:
                        other.add(item.name)
            elif isinstance(item, ast.Assign):
                # an enum member: `MemberName = _fusion.Class_MemberName`
                for tgt in item.targets:
                    if isinstance(tgt, ast.Name) and not tgt.id.startswith("_"):
                        names.add(tgt.id)
        if names:
            properties[key] = sorted(names)
    return properties, factories, bools, other


def apply_measured_returns(factories, measured=None):
    """`factories` with each MEASURED runtime return substituted for a broken binding annotation.

    A row the bindings do not declare, and a row whose annotation now AGREES with the measurement,
    are both stale: each raises rather than being applied silently, so a fixed binding cannot leave
    a permanent override behind."""
    table = _MEASURED_FACTORY_RETURNS if measured is None else measured
    out = dict(factories)
    for key, (landed, _read_on) in table.items():
        if key not in out:
            raise SystemExit(
                f"_MEASURED_FACTORY_RETURNS names '{key}', which the bindings no longer declare as "
                "a create* factory - drop the row or re-measure it live.")
        if out[key] == landed:
            raise SystemExit(
                f"_MEASURED_FACTORY_RETURNS overrides '{key}' to '{landed}', which the bindings now "
                "annotate themselves - the override is obsolete, drop the row.")
        out[key] = landed
    return out


def build(bindings_dir=None):
    root = find_bindings() if bindings_dir is None else find_bindings(bindings_dir)
    if root is None:
        return None, None, None, None
    properties, factories, bools, other = {}, {}, set(), set()
    for mod in _MODULES:
        path = os.path.join(root, f"{mod}.py")
        if not os.path.isfile(path):
            continue
        p, f, b, o = scan_module(path, mod)
        properties.update(p)
        factories.update(f)
        bools |= b
        other |= o
    # Only what a create* factory HANDS BACK is kept: those are the objects a tool builds and then
    # assigns properties onto, and the
    # lint ERRORS on an input class missing from the table rather than skipping it, so narrowing
    # here cannot open a silent hole. Keeping all 1,670 classes would be a half-megabyte of churn
    # on every Fusion update for no extra coverage.
    factories = {k: v for k, v in factories.items() if k.rsplit(".", 1)[1].startswith("create")}
    # Before `keep`, so the class a caller ACTUALLY receives is the one whose member list is kept.
    factories = apply_measured_returns(factories)
    keep = set(factories.values()) | set(_EXTRA_CLASSES)
    missing = [c for c in _EXTRA_CLASSES if c not in properties]
    if missing:
        raise SystemExit(f"_EXTRA_CLASSES names classes the bindings do not define: {missing}")
    properties = {k: v for k, v in properties.items() if k in keep}
    # A name that returns something OTHER than bool anywhere in the API is ambiguous at a call site
    # (Features.add returns a feature; ObjectCollection.add returns bool), and this lint only ever
    # sees the name. Keep only names that are bool EVERYWHERE, so a hit cannot be a false positive.
    bools -= other
    return root, properties, factories, bools


def _build_id(root):
    """Return the build directory for the declared Windows and macOS binding layouts."""
    parts = os.path.normpath(root).split(os.sep)
    if len(parts) >= 7 and parts[-5] == "Contents" and parts[-6].endswith(".app"):
        return parts[-7]
    return parts[-5] if len(parts) >= 5 else os.path.basename(root)


def _render(root, properties, factories, bools):
    lines = [
        "# GENERATED by tests/gen_api_surface.py from the installed Fusion Python bindings"
        " - DO NOT EDIT.",
        "# Regenerate: py -3 tests/gen_api_surface.py",
        '"""Every adsk class\'s real member names, and what each factory method returns.',
        "",
        "A SWIG proxy accepts an assignment to a name it does not define - the value lands on a",
        "dead Python attribute while the object keeps its API default - so a misspelled input",
        "property cannot raise. test_input_property_names.py checks each assignment against these",
        'lists, which is the only place that mistake is catchable."""',
        "",
        "# The Fusion build the surface was read from. NOT an absolute path - that would differ"
        " per user",
        "# and make the staleness check fail on every machine but the one that generated it.",
        f'BINDINGS_BUILD = "{_build_id(root)}"',
        "",
        "# 'module.Class' -> every real member name (properties and methods).",
        "PROPERTIES = {",
    ]
    for key in sorted(properties):
        names = properties[key]
        lines.append(f'    "{key}": (')
        row = "        "
        for n in names:
            piece = f'"{n}", '
            if len(row) + len(piece) > 96:
                lines.append(row.rstrip())
                row = "        "
            row += piece
        if row.strip():
            lines.append(row.rstrip())
        lines.append("    ),")
    lines.append("}")
    lines.append("")
    lines.append("# 'module.Class.method' -> the 'module.Class' it returns.")
    if _MEASURED_FACTORY_RETURNS:
        lines.append("# A live-measured return overrides a broken binding annotation for: "
                     + ", ".join(sorted(_MEASURED_FACTORY_RETURNS)) + ".")
    lines.append("FACTORIES = {")
    for key in sorted(factories):
        lines.append(f'    "{key}": "{factories[key]}",')
    lines.append("}")
    lines.append("")
    lines.append('# Method names the bindings declare as returning bool - each documents "Returns')
    lines.append('# true if successful", so discarding the answer discards the failure.')
    lines.append("BOOL_METHODS = frozenset({")
    row = "    "
    for n in sorted(bools):
        piece = f'"{n}", '
        if len(row) + len(piece) > 96:
            lines.append(row.rstrip())
            row = "    "
        row += piece
    if row.strip():
        lines.append(row.rstrip())
    lines.append("})")
    lines.append("")
    return "\n".join(lines)


# Failure is raised as SystemExit: gen_all.py checks main()'s return value too, but SystemExit
# also stops a direct `py -3 tests/gen_api_surface.py --check` run with a non-zero exit.
def _fail(msg):
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--bindings-dir", metavar="PATH",
                    help="use this adsk package directory instead of newest installed bindings")
    args = ap.parse_args(argv)
    root, properties, factories, bools = build(args.bindings_dir)
    check = args.check
    if root is None:
        # No bindings means the surface CANNOT be recomputed, so nothing here can tell a current
        # tests/api_surface.py from one generated against a Fusion release ago - and the lint that
        # checks every input-property assignment against that table is only as true as the table.
        # A machine without Fusion therefore fails: it is a visible limitation, not a pass.
        _fail("Fusion Python bindings not found - cannot generate or verify the API surface. "
              "Looked under: " + "; ".join(_BINDING_GLOBS) + ". "
              + ("--check cannot prove tests/api_surface.py is current without them; run this on a "
                 "machine with Fusion installed." if check else ""))
    text = _render(root, properties, factories, bools)
    if check:
        current = ""
        if os.path.isfile(OUT_PATH):
            with open(OUT_PATH, encoding="utf-8") as fh:
                current = fh.read()
        if current.replace("\r\n", "\n") != text:
            _fail("tests/api_surface.py is STALE - regenerate: py -3 tests/gen_api_surface.py")
        print(f"api surface current: {len(properties)} classes, {len(factories)} factories")
        return 0
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"Wrote {OUT_PATH} ({len(properties)} classes, {len(factories)} factory returns) "
          f"from {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
