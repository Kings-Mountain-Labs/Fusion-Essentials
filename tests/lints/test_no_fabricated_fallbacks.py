# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""A failed read may not fall back to a number, or to the request that asked for it.

A `safe(read, default)` fails when the default is one of the handler's own PARAMETERS, or a number
on a `_MEASURED` attribute. None is the honest fallback; `_COUNTISH` and _ALLOWED sites are exempt."""

import ast
import os

import _corpus
from conftest import TOOLS_DIR

# Attributes whose value IS a measurement: a fabricated number here is a false reading, not a
# missing one. Read off the final attribute in the safe() lambda.
_MEASURED = frozenset({
    "value", "mass", "volume", "area", "density", "length", "radius", "diameter",
    "angle", "distance", "perimeter", "thickness", "offset", "depth", "height", "width",
})

# Tallies. A collection that cannot be read holding "0 items" is a defensible reading, and the
# codebase leans on it heavily for adsk collections that are absent rather than empty.
_COUNTISH = frozenset({"count", "len", "quantity", "numberOfFaces", "triangleCount", "nodeCount"})


# Sites where a numeric fallback is NOT a fabricated measurement, keyed (file, the site's own
# source text). Each needs a reason naming why the number is defensible - the value must not reach a
# payload as a measurement. A text repeated in one file exempts each copy. Shrink-only.
_ALLOWED = {
    ("_assembly_detail.py", "safe(lambda: jo.offsetX.value, 0.0)"):
        "a joint origin's offsetX genuinely defaults to 0 (live-verified: a face/sketch-anchored JO "
        "reports geometry.origin as-is)",
    ("_assembly_detail.py", "safe(lambda: jo.offsetY.value, 0.0)"):
        "offsetY, same contract as offsetX",
    ("_assembly_detail.py", "safe(lambda: jo.offsetZ.value, 0.0)"):
        "offsetZ, same contract as offsetX",
    ("_inputs.py", "_common.safe(lambda: vec.length, 0.0)"):
        "a degeneracy GUARD - an unreadable vector length is treated as zero so the direction is "
        "REFUSED, which is the safe direction",
    ("cam_edit_tools.py", "safe(lambda: p.value.value, default)"):
        "a generic CAM-parameter reader whose 'default' is the CALLER's chosen value for an absent "
        "parameter, not the tool's own request",
    ("_joint_inputs.py", "safe(lambda f=f: f.area, 0.0)"):
        "picking the LARGEST face - an unreadable area sorts last and is never published",
    ("sketch_dimension.py", "safe(lambda: geo.radius, 0.0)"):
        "a text-placement offset, immediately replaced by 1.0 when it is not positive; never "
        "published",
    ("surface_trim.py", "safe(lambda i=i: cells.item(i).cellBody.area, 0.0)"):
        "cell areas compared against each other to pick a cell; not published",
    ("surface_untrim.py", "safe(lambda f=f: f.area, 0.0)"):
        "an area SUM compared before/after to prove the untrim moved something",
}


def _iter_tool_files():
    for name in sorted(os.listdir(TOOLS_DIR)):
        if name.endswith(".py") and name != "__init__.py":
            yield name, os.path.join(TOOLS_DIR, name)


def _site(source, node):
    """The site's OWN source text, whitespace-collapsed - the allowlist key, which a line move
    cannot stale."""
    return " ".join((ast.get_source_segment(source, node) or "").split())


def _final_attr(node):
    """The last attribute name a lambda body reads ('mr.value' -> 'value'), or None."""
    while isinstance(node, ast.Call):
        node = node.func
    return node.attr if isinstance(node, ast.Attribute) else None


def _params(fn):
    a = fn.args
    names = [p.arg for p in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)]
    if a.vararg:
        names.append(a.vararg.arg)
    if a.kwarg:
        names.append(a.kwarg.arg)
    return set(names)


def _offenders_in(path):
    """[(lineno, site, kind, detail)] for every safe() whose fallback fabricates a value."""
    out = []
    tree = _corpus.tree(path)
    source = _corpus.text(path)

    def walk(node, params):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = params | _params(node)
        if isinstance(node, ast.Call):
            fname = node.func.attr if isinstance(node.func, ast.Attribute) else \
                getattr(node.func, "id", None)
            if fname == "safe" and len(node.args) >= 2:
                body = node.args[0]
                body = body.body if isinstance(body, ast.Lambda) else body
                attr = _final_attr(body)
                default = node.args[1]
                # -1 parses as UnaryOp(USub, Constant(1)) - unwrap the sign so a negative literal
                # fallback is the same number-for-a-measurement hit a positive one is.
                if (isinstance(default, ast.UnaryOp)
                        and isinstance(default.op, (ast.USub, ast.UAdd))
                        and isinstance(default.operand, ast.Constant)):
                    default = default.operand
                if isinstance(default, ast.Name) and default.id in params:
                    out.append((node.lineno, _site(source, node), "request-as-fallback",
                                f"falls back to the parameter '{default.id}'"))
                elif (isinstance(default, ast.Constant)
                      and isinstance(default.value, (int, float))
                      and not isinstance(default.value, bool)
                      and attr in _MEASURED and attr not in _COUNTISH):
                    out.append((node.lineno, _site(source, node), "number-for-a-measurement",
                                f"reads .{attr} and falls back to {default.value!r}"))
        for child in ast.iter_child_nodes(node):
            walk(child, params)

    walk(tree, set())
    return out


class TestNoFabricatedFallbacks:
    def test_no_read_falls_back_to_a_number_or_to_the_request(self):
        offenders = []
        for name, path in _iter_tool_files():
            for lineno, site, kind, detail in _offenders_in(path):
                if (name, site) in _ALLOWED:
                    continue
                offenders.append(f"{name}:{lineno}: [{kind}] safe(...) {detail}")
        assert not offenders, (
            "a failed read must be None, so the caller can tell an unmeasurable value from a real "
            "one - never the request, and never a fabricated number:\n  " + "\n  ".join(offenders))

    def test_every_allowlisted_site_still_exists(self):
        live = set()
        for name, path in _iter_tool_files():
            for _lineno, site, _kind, _detail in _offenders_in(path):
                live.add((name, site))
        stale = sorted(set(_ALLOWED) - live)
        assert not stale, (
            "these allowlist entries name no fabricated fallback in that file - the site was "
            "reworded or fixed, so the entry now exempts nothing anybody audited. Update the text "
            "or remove them:\n  "
            + "\n  ".join(f"{n}: looked for `{s}`" for n, s in stale))

    def test_the_gate_inspects_a_real_number_of_safe_calls(self):
        """A floor on the raw material the detector works over."""
        seen = 0
        for _name, path in _iter_tool_files():
            for node in ast.walk(_corpus.tree(path)):
                if isinstance(node, ast.Call):
                    fname = node.func.attr if isinstance(node.func, ast.Attribute) else \
                        getattr(node.func, "id", None)
                    if fname == "safe":
                        seen += 1
        assert seen >= 500, f"only {seen} safe() calls found - the fleet shape has changed."
