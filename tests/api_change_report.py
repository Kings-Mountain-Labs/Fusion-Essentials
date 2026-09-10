# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.
"""Snapshot on-disk API declarations and report candidate tool impacts without importing adsk."""

import argparse
import ast
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import gen_api_surface

NAMESPACES = ("core", "fusion", "cam", "drawing", "sim", "electron", "volume")
FORMAT = "fusion-declarations-v2"
CANDIDATE = "syntactic_candidate"
EVIDENCE = "On-disk declarations only; no live API, loaded-build, or behavioral verification."


def digest(text):
    """Return a SHA-256 digest of UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def annotation(node, module, class_names):
    """Normalize stub and SWIG annotations without evaluating them."""
    if node is None:
        return None
    value = node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ast.unparse(node)
    value = value.replace(" *", "").replace("typing.", "")
    value = re.sub(r"\b(core|fusion|cam|drawing|sim|electron|volume)\.", r"adsk.\1.", value)
    value = value.replace("adsk.adsk.", "adsk.")
    if value in class_names:
        value = f"adsk.{module}.{value}"
    return value


def method_signature(node, module, names):
    """Return comparable argument and return declarations."""
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    rows = []
    for index, (arg, default) in enumerate(zip(positional, defaults)):
        if arg.arg in ("self", "cls"):
            continue
        rows.append({"name": arg.arg, "kind": "posonly" if index < len(args.posonlyargs) else "positional",
                     "type": annotation(arg.annotation, module, names),
                     "default": ast.unparse(default) if default is not None else None})
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        rows.append({"name": arg.arg, "kind": "keyword", "type": annotation(arg.annotation, module, names),
                     "default": ast.unparse(default) if default is not None else None})
    for kind, arg in (("vararg", args.vararg), ("kwarg", args.kwarg)):
        if arg:
            rows.append({"name": arg.arg, "kind": kind, "type": annotation(arg.annotation, module, names)})
    return {"arguments": rows, "returns": annotation(node.returns, module, names)}


def doc_record(node):
    """Return searchable documentation and change markers."""
    doc = " ".join((ast.get_docstring(node) or "").split())
    return {"summary": doc[:320], "doc_sha256": digest(doc),
            "mentions_preview": bool(re.search(r"preview", doc, re.I)),
            "mentions_retired": bool(re.search(r"retired|deprecated", doc, re.I)),
            "mentions_unsupported": "not officially supported" in doc.lower()}


def scan_module(path):
    """Parse public class definitions, properties, methods and enum member names."""
    raw, tree = gen_api_surface.read_declarations(path)
    source = raw.decode("utf-8-sig")
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and not node.name.startswith("_")]
    names = {node.name for node in classes}
    result = {}
    for cls in classes:
        members, bindings, swig_properties = {}, {}, set()
        functions = {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decorators = [ast.unparse(d) for d in node.decorator_list]
                setter = node.name.startswith("_set_") or any(d.endswith(".setter") for d in decorators)
                getter = node.name.startswith("_get_") or "property" in decorators
                name = node.name[5:] if node.name.startswith(("_get_", "_set_")) else node.name
                if name.startswith("_") or name == "thisown":
                    continue
                if node.name.startswith(("_get_", "_set_")):
                    swig_properties.add(name)
                if getter or setter:
                    entry = members.setdefault(name, {"kind": "property", "readable": False, "writable": False})
                    entry["writable" if setter else "readable"] = True
                    if setter:
                        entry["setter_signature"] = method_signature(node, path.stem, names)
                        entry["setter_documentation"] = doc_record(node)
                    if getter:
                        entry.update(doc_record(node))
                        entry.update({"line": node.lineno, "returns": annotation(node.returns, path.stem, names)})
                else:
                    entry = members.setdefault(name, {"kind": "method", "line": node.lineno, "overloads": []})
                    entry["overloads"].append({"signature": method_signature(node, path.stem, names),
                                               "decorators": decorators, **doc_record(node)})
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if not isinstance(target, ast.Name) or target.id.startswith("_") or target.id == "thisown":
                        continue
                    value = node.value
                    if isinstance(value, ast.Call) and ast.unparse(value.func) == "property":
                        bindings[target.id] = value
                        continue
                    if target.id in members:
                        continue
                    literal = value.value if isinstance(value, ast.Constant) else None
                    members[target.id] = {"kind": "constant", "line": node.lineno,
                                          "literal": literal, "literal_available": isinstance(value, ast.Constant),
                                          "expression": ast.unparse(value) if value is not None else None}
        for name in swig_properties - bindings.keys():
            members.pop(name, None)
        for name, binding in bindings.items():
            accessors = dict(zip(("fget", "fset"), binding.args[:2]))
            accessors.update({k.arg: k.value for k in binding.keywords if k.arg in ("fget", "fset")})
            entry = {"kind": "property", "line": binding.lineno}
            for key, flag in (("fget", "readable"), ("fset", "writable")):
                expression = accessors.get(key)
                entry[flag] = expression is not None and not (isinstance(expression, ast.Constant) and expression.value is None)
                helper = functions.get(expression.id) if isinstance(expression, ast.Name) else None
                if helper is not None and key == "fget":
                    entry.update(doc_record(helper))
                    entry["returns"] = annotation(helper.returns, path.stem, names)
                elif helper is not None and key == "fset":
                    entry["setter_signature"] = method_signature(helper, path.stem, names)
                    entry["setter_documentation"] = doc_record(helper)
            members[name] = entry
        result[f"adsk.{path.stem}.{cls.name}"] = {
            "line": cls.lineno, "source": str(path.resolve()), "members": dict(sorted(members.items())),
            "binding_helper": cls.name in ("SwigPyIterator", "Base") or cls.name.endswith("Vector"),
            "bases": [ast.unparse(base) for base in cls.bases], **doc_record(cls)}
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
            "representation": "swig" if "generated by SWIG" in source[:400] else "python",
            "classes": result}



def snapshot(bindings, build_label):
    """Capture binding bytes, declaration format, and explicitly attributed build provenance."""
    bindings = bindings.resolve(strict=True)
    if not bindings.is_dir() or not build_label.strip():
        raise ValueError("Provide a binding directory and nonempty build label")
    paths = {p.stem: p for p in bindings.glob("*.py") if not p.name.startswith("_")}
    modules = {f"adsk.{name}": scan_module(path) for name, path in sorted(paths.items())}
    if not modules:
        raise ValueError(f"No public .py declaration modules at {bindings}")
    parts = bindings.parts
    deployment = None
    for index, part in enumerate(parts[:-2]):
        if part.lower() == "webdeploy":
            deployment = {"channel": parts[index + 1], "directory_id": parts[index + 2]}
    result = {
        "format": FORMAT, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "provenance": {"bindings_path": str(bindings), "build_label": build_label,
                       "build_label_source": "caller-supplied; not a loaded Fusion observation",
                       "webdeploy_path": deployment,
                       "scanner_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                          for p in (Path(__file__), Path(gen_api_surface.__file__))}},
        "namespace_scope": [f"adsk.{name}" for name in NAMESPACES],
        "unavailable_namespaces": [f"adsk.{name}" for name in NAMESPACES if name not in paths],
        "modules": modules, "evidence": EVIDENCE,
    }
    result["content_sha256"] = content_hash(result)
    return result


def content_hash(data):
    """Hash canonical snapshot content excluding its own digest field."""
    return digest(json.dumps({k: v for k, v in data.items() if k != "content_sha256"},
                             sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def validate_snapshot(data):
    """Refuse incompatible, inconsistent, or modified snapshots."""
    if data.get("format") != FORMAT:
        raise ValueError(f"Snapshot format must be {FORMAT}; recapture with this command")
    if data.get("content_sha256") != content_hash(data):
        raise ValueError("Snapshot content hash mismatch; do not edit captured snapshots")
    available, unavailable = set(data["modules"]), set(data["unavailable_namespaces"])
    if available & unavailable or set(data["namespace_scope"]) - available != unavailable:
        raise ValueError("Snapshot namespace availability is inconsistent")
    if any(m.get("representation") not in ("swig", "python") for m in data["modules"].values()):
        raise ValueError("Unknown declaration representation")


def flatten(data, namespaces):
    """Return public class and member declarations in comparable namespaces."""
    result = {}
    for module in sorted(namespaces):
        for name, cls in data["modules"][module]["classes"].items():
            result[name] = {key: value for key, value in cls.items()
                            if key not in ("members", "line", "source", "summary")}
            result[name]["kind"] = "class"
            for member, entry in cls["members"].items():
                result[f"{name}.{member}"] = {k: v for k, v in entry.items() if k not in ("line", "summary")}
    return result


def compare(before, after):
    """Compare compatible snapshots without interpreting missing namespaces as removed symbols."""
    for data in (before, after):
        validate_snapshot(data)
    if before["namespace_scope"] != after["namespace_scope"]:
        raise ValueError("Snapshot namespace scopes differ; recapture with the same scope")
    old_modules, new_modules = set(before["modules"]), set(after["modules"])
    common = old_modules & new_modules
    mismatch = [m for m in sorted(common) if before["modules"][m]["representation"] !=
                after["modules"][m]["representation"]]
    if mismatch:
        raise ValueError("Declaration representation mismatch: " + ", ".join(mismatch))
    if not common:
        raise ValueError("Snapshots have no commonly available namespaces to compare")
    old, new = flatten(before, common), flatten(after, common)
    changes = []
    for symbol in sorted(old.keys() | new.keys()):
        a, b = old.get(symbol), new.get(symbol)
        if a == b:
            continue
        fields = sorted(k for k in (a or {}).keys() | (b or {}).keys() if (a or {}).get(k) != (b or {}).get(k))
        changes.append({"symbol": symbol, "change": "added" if a is None else "removed" if b is None else "changed",
                        "changed_fields": fields, "before": a, "after": b})
    return {"format": "fusion-api-impact-v1", "evidence": EVIDENCE,
            "compared_namespaces": sorted(common),
            "namespaces_unavailable_in_before": sorted(new_modules - old_modules),
            "namespaces_unavailable_in_after": sorted(old_modules - new_modules),
            "unavailable_namespaces_before": before["unavailable_namespaces"],
            "unavailable_namespaces_after": after["unavailable_namespaces"],
            "changes": changes,
            "summary": {kind: sum(c["change"] == kind for c in changes)
                        for kind in ("added", "removed", "changed")}}


def source_references(repo):
    """Index syntactic attributes and local imports without resolving receiver types or executing tools."""
    references, hashes, imports, tools = {}, {}, {}, set()
    for path in sorted((repo / "commands/mcpServer/tools").glob("*.py")):
        raw, tree = gen_api_surface.read_declarations(path)
        relative = path.relative_to(repo).as_posix()
        hashes[relative] = hashlib.sha256(raw).hexdigest()
        imports[path.stem] = set()
        if not path.stem.startswith("_") and any(isinstance(n, ast.FunctionDef) and
                                                 n.name == "register_tool" for n in tree.body):
            tools.add(path.stem)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                imports[path.stem].update([node.module.split(".")[0]] if node.module else
                                          [alias.name for alias in node.names])
            name = node.attr if isinstance(node, ast.Attribute) else None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "setattr", "hasattr"):
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    name = node.args[1].value
            if name:
                references.setdefault(name, set()).add((path.stem, relative, node.lineno))
    return references, hashes, imports, tools


def acceptance_references(repo, tools):
    """Index unit test names, literal live rows, and explicit tool mentions in eval scenarios."""
    references, hashes = {name: [] for name in tools}, {}
    unit_paths = [repo / f"tests/unit/test_{name}.py" for name in sorted(tools)]
    live_paths = sorted((repo / "tests/live").glob("verify_acts_*.py"))
    for path in [p for p in unit_paths if p.is_file()] + live_paths:
        raw, tree = gen_api_surface.read_declarations(path)
        relative = path.relative_to(repo).as_posix()
        hashes[relative] = hashlib.sha256(raw).hexdigest()
        tool = path.stem.removeprefix("test_") if path.parent.name == "unit" else None
        for node in ast.walk(tree):
            if tool and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                references[tool].append({"kind": "unit_test", "case": node.name, "file": relative, "line": node.lineno})
            elif not tool and isinstance(node, ast.Tuple) and node.elts:
                first = node.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value in tools:
                    references[first.value].append({"kind": "live_row", "file": relative, "line": node.lineno})
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in sorted(tools)) + r")\b") if tools else None
    for path in sorted((repo / "tests/live/evals/scenarios").glob("*.md")):
        raw = path.read_bytes()
        relative = path.relative_to(repo).as_posix()
        hashes[relative] = hashlib.sha256(raw).hexdigest()
        for line, text in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
            for tool in sorted(set(pattern.findall(text) if pattern else [])):
                references[tool].append({"kind": "eval_tool_mention", "case": path.stem, "file": relative, "line": line})
    return references, hashes


def add_impacts(report, repo):
    """Attach coarse source matches and candidate checks; never claim semantic reachability or coverage."""
    repo = repo.resolve(strict=True)
    if not (repo / "commands/mcpServer/tools").is_dir():
        raise ValueError(f"No commands/mcpServer/tools directory at {repo}")
    refs, hashes, imports, tools = source_references(repo)
    acceptance, test_hashes = acceptance_references(repo, tools)
    reach = {}
    for tool in sorted(tools):
        seen, pending = set(), {tool}
        while pending:
            name = pending.pop()
            if name not in seen:
                seen.add(name)
                pending.update(imports.get(name, set()) - seen)
        reach[tool] = seen
    affected = set()
    for change in report["changes"]:
        names = [change["symbol"].rsplit(".", 1)[-1]]
        matches = sorted({ref for name in names for ref in refs.get(name, set())})
        candidates = []
        for tool in sorted(tools):
            evidence = [{"file": file, "line": line, "via": "direct" if module == tool else "local_import"}
                        for module, file, line in matches if module in reach[tool]]
            if evidence:
                candidates.append({"tool": tool, "classification": CANDIDATE, "source_matches": evidence})
                affected.add(tool)
        change["candidate_tools"] = candidates
    report["impact_mapping"] = {
        "classification": CANDIDATE, "repository_path": str(repo),
        "method": "Attribute-name matches and transitive local imports; unit filenames, literal live rows, explicit eval tool mentions.",
        "limitations": "No receiver typing or call graph. Imports overmatch; dynamic references and unnamed scenarios can be missed. Empty matches do not prove absence of impact. Checks are not executed or certified sufficient.",
        "source_sha256": dict(sorted((hashes | test_hashes).items())),
        "candidate_acceptance_cases": {name: {"classification": CANDIDATE, "cases": acceptance[name]}
                                       for name in sorted(affected)},
    }
    return report


def load_snapshot(path):
    """Read and validate one snapshot while recording the exact artifact path and byte hash."""
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    validate_snapshot(data)
    return data, {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
                  "content_sha256": data["content_sha256"], "provenance": data["provenance"],
                  "modules": {k: {f: v[f] for f in ("path", "sha256", "representation")}
                              for k, v in data["modules"].items()}}


def write_json(path, data):
    """Create a JSON artifact exclusively; never overwrite an existing snapshot or report."""
    text = json.dumps(data, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("snapshot", help="capture explicit on-disk bindings without importing adsk")
    capture.add_argument("--bindings-dir", type=Path, required=True)
    capture.add_argument("--build-label", required=True)
    capture.add_argument("--out", type=Path, required=True)
    diff = sub.add_parser("diff", help="compare immutable snapshots and identify syntactic impact candidates")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    diff.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    diff.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            data = snapshot(args.bindings_dir, args.build_label)
            result = {"namespaces": sorted(data["modules"]), "unavailable_namespaces": data["unavailable_namespaces"]}
        else:
            before, before_source = load_snapshot(args.before)
            after, after_source = load_snapshot(args.after)
            data = add_impacts(compare(before, after), args.repo)
            data.update({"before_snapshot": before_source, "after_snapshot": after_source})
            result = data["summary"]
        write_json(args.out, data)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
        parser.exit(2, f"API report refused: {exc}\n")
    print(json.dumps({"output": str(args.out.resolve()), **result}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
