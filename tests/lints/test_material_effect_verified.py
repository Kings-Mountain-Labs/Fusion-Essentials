# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: a write tool whose 'operation' enum offers cut or intersect fails unless its handler (or a
same-module helper it directly calls) takes a MATERIAL reading - volume, face count, lump, body
census, native token - AFTER a features.<x>.add(...) mutation, with an error(...) gate on the
surface. A tool that cannot take one goes in the shrink-only _MATERIAL_EXEMPT with a reason."""

import inspect
import re

from conftest import is_write_tool, register_all_tools

# The mutation this lint anchors on: a features collection's add(). A tool that reaches its
# features collection through a local variable does not match, and needs an exemption saying so.
_MUTATION = re.compile(r"features\.[A-Za-z_][A-Za-z0-9_]*\.add\(")

# MATERIAL evidence - a reading of what the model is MADE OF, taken after the mutation. Word-bounded
# on purpose: a payload key like `input_body_count` echoes a request length and is not a census.
_MATERIAL = re.compile(
    r"\b(?:volume_delta|volumes\(|signed_volume|face_count_delta|lump_count|body_count|"
    r"census_host|native_token|native_identity)\b")

_ERROR_CALL = re.compile(r"\berror\(")
# every name called in a source part - how the handler's own same-module helpers are found.
_CALLED_NAME = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Tools in the target set that cannot take a material reading: name -> the audited reason, either
# 'evidence:' (it verifies with a reading outside this lint's vocabulary) or 'shape:' (its
# cut/intersect removes no material). Shrink-only - never a quiet exit from the detector.
_MATERIAL_EXEMPT = {
    'mesh_combine': 'evidence: a MeshBody carries no volume, and the meshCombineFeatures collection '
                    'is held in a local before its add() - the target mesh TRIANGLE count is read '
                    'before and after instead, and an unchanged count on a cut/intersect is an '
                    'error(...) naming the non-overlapping tool',
    'model_stitch': 'shape: a stitch only sews surface bodies together - its operation selects how a '
                    'result that CLOSES into a solid joins the model and no stitch removes material '
                    'from anything, so there is no material delta to read; the result bodies isSolid '
                    'flags are the effect and became_solid reports the observed truth',
}


def _cut_capable(item):
    """True when this tool's wire 'operation' enum offers cut or intersect - the schema-keyed
    membership test, so no hand list can go stale."""
    props = (item.to_dict().get("inputSchema") or {}).get("properties", {}) or {}
    enum = (props.get("operation") or {}).get("enum") or []
    return bool({"cut", "intersect"} & set(enum))


def _original_handler(item):
    """Unwrap the write-guard/assert chain (__wrapped__) to the tool module's own handler."""
    h = item.handler
    seen = set()
    while h is not None and id(h) not in seen:
        seen.add(id(h))
        nxt = getattr(h, "__wrapped__", None)
        if nxt is None:
            return h
        h = nxt
    return h


def _handler_parts(item):
    """The source parts this lint scans, in order: the handler itself, then every same-module
    function it directly calls (depth 1, sorted by name). Dispatch handlers (action= routers) verify
    inside their _do_*/leaf helpers, so the handler body alone would under-read them; keeping the
    parts SEPARATE keeps the positional check honest (source order across different functions is
    meaningless). Returns None when no source exists."""
    h = _original_handler(item)
    try:
        handler_src = inspect.getsource(h)
    except (OSError, TypeError):
        return None
    module_globals = getattr(h, "__globals__", {})
    parts = [handler_src]
    for called in sorted(set(_CALLED_NAME.findall(handler_src))):
        fn = module_globals.get(called)
        if (callable(fn) and getattr(fn, "__module__", None) == h.__module__
                and called != h.__name__):
            try:
                parts.append(inspect.getsource(fn))
            except (OSError, TypeError):
                pass
    return parts


def _verifies_material(parts):
    """True when some part performs a features.<x>.add(...) mutation with material evidence AFTER it
    in that same part, and an error(...) gate exists on the surface. Parts are kept separate because
    source order across two different functions says nothing about what ran first."""
    if not parts:
        return False
    if not any(_ERROR_CALL.search(p) for p in parts):
        return False
    for part in parts:
        for m in _MUTATION.finditer(part):
            if _MATERIAL.search(part[m.end():]):
                return True
    return False


def _cut_capable_write_tools():
    out = {}
    for it in register_all_tools():
        if not is_write_tool(it):
            continue                        # a read removes no material
        if _cut_capable(it):
            out[it.get_name()] = it
    return out


class TestMaterialEffectVerified:
    def test_every_cut_capable_tool_reads_material_back(self):
        unverified = []
        for name, item in sorted(_cut_capable_write_tools().items()):
            if name in _MATERIAL_EXEMPT:
                continue
            if not _verifies_material(_handler_parts(item)):
                unverified.append(name)
        assert not unverified, (
            "these tools offer a cut/intersect operation but take no MATERIAL reading after their "
            "features.<x>.add(...): a volume/face-count/lump/census/token read, plus an error(...) "
            "gate that fails the call when nothing moved. A feature object, its name, its count and "
            "its healthState all read identically for a cut that removed nothing, so none of them "
            "is evidence. Add the reading, or add a _MATERIAL_EXEMPT entry naming why one cannot be "
            "taken - do NOT weaken this detector:\n  " + "\n  ".join(unverified))

    def test_the_target_set_is_not_empty(self):
        # The membership test reads a wire schema; a rename of the 'operation' input or of the enum
        # values would silently empty the target set and make every assertion above vacuous.
        names = sorted(_cut_capable_write_tools())
        assert len(names) >= 8, (
            f"only {len(names)} cut-capable write tools found ({', '.join(names)}) - the schema key "
            "this lint selects on has moved.")

    def test_exemptions_only_name_real_cut_capable_tools(self):
        targets = _cut_capable_write_tools()
        stale = []
        for name in _MATERIAL_EXEMPT:
            item = targets.get(name)
            if item is None:
                stale.append(f"{name} (not a cut-capable write tool)")
            elif _verifies_material(_handler_parts(item)):
                stale.append(f"{name} (now reads material back - drop the exemption)")
        assert not stale, ("stale _MATERIAL_EXEMPT entries - the table only shrinks:\n  "
                           + "\n  ".join(stale))

