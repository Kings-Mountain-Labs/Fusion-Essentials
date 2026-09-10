# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: COPY existing sketch entities through a transform, into this sketch or
another one. WRITES; the copy is proven by the TARGET's own curve-count delta.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _assert
from . import _common
from . import _sketch_detail
from ._sketch_detail import _prepare, _requested, _transform_wire

app = adsk.core.Application.get()


def _curve_ref_by_token(sketch):
    """entityToken -> '<type>:<index>' for the sketch's curves, MINUS any token two curves share (the
    pieces a split returns carry one token, see _sketch_detail.curve_id) - a shared token names
    neither of them, so it is dropped rather than resolved to the first hit."""
    refs, shared = {}, set()
    # The index IS the published address ('<kind>:<index>' is what _common.resolve_entity_ref reads
    # back with coll.item(index)), so this stays a positional walk: iter_collection drops an
    # unreadable curve, which would mint refs pointing at the wrong entities.
    for kind, coll in _sketch_detail._curve_collections(sketch):
        for i in range(safe(lambda coll=coll: coll.count, 0) if coll else 0):
            tok = safe(lambda coll=coll, i=i: coll.item(i).entityToken)
            if not tok:
                continue
            if tok in refs:
                shared.add(tok)
            else:
                refs[tok] = f"{kind}:{i}"
    for tok in shared:
        refs.pop(tok, None)
    return refs


def _copied_refs(target, items):
    """The '<type>:<index>' refs of the copied curves, resolved through each returned entity's
    nativeObject; a copied endpoint resolves to none, being no curve of the target."""
    # For a sketch a COMPONENT owns, Sketch.copy hands back assembly-context PROXIES while the
    # sketch's collections hold the NATIVE curves, and neither identity nor entityToken crosses that
    # seam. `nativeObject` does, and reads None for an already-native entity.
    by_token = _curve_ref_by_token(target)
    refs = []
    for c in items:
        native = safe(lambda c=c: c.nativeObject) or c
        tok = safe(lambda native=native: native.entityToken)
        ref = by_token.get(tok) if tok else None
        if ref is None:
            ref = _sketch_detail.curve_id(target, native)
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def handler(sketch_name: str = "", entities: str = "", target_sketch: str = "",
            units: str = "mm", dx=None, dy=None, rotation_deg=None, center_x=None,
            center_y=None, scale_factor=None, component: str = "",
            target_component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    design, sketch, ents, refs, coll, matrix, unit, err = _prepare(
        sketch_name, entities, units, dx, dy, rotation_deg, center_x, center_y, scale_factor,
        component)
    if err:
        return err

    name = safe(lambda: sketch.name)
    want_target = (target_sketch or "").strip()
    target = sketch
    if want_target:
        # The destination gets its own scope: it is a SECOND by-name sketch reference, and a copy
        # into a name two components carry has to be able to say which one without renaming either.
        target, refusal = _sketch_detail.scoped_sketch(design, want_target, target_component,
                                                       "target_component")
        if refusal:
            return error(refusal)
        if target is None:
            return error(f"No sketch named '{want_target}' for 'target_sketch'. Available: " + (
                ", ".join(n for n in _common.all_sketch_names(design) if n) or "(none)"))
    target_name = safe(lambda: target.name)

    before_n = safe(lambda: target.sketchCurves.count, 0) or 0
    try:
        # targetSketch is passed ONLY when one was asked for - a same-sketch copy is the measured
        # two-argument form.
        created = sketch.copy(coll, matrix, target) if want_target else sketch.copy(coll, matrix)
    except Exception as e:
        return error(f"Could not copy {', '.join(refs)} from sketch '{name}' into "
                     f"'{target_name}': {e}")
    if created is None:
        return error(f"copy returned no collection for {', '.join(refs)} in sketch '{name}' - "
                     "nothing was copied.")

    # The returned collection counts the copied curves' SKETCH POINTS too (measured: copying ONE
    # line returns 3), so the curve-count delta on the TARGET is the honest read-back, and the
    # nativeObject bridge (see _copied_refs) is what turns the returned entities into refs.
    n = safe(lambda: created.count, 0) or 0
    items = list(_common.iter_collection(created))
    new_curves = _copied_refs(target, items)
    after_n = safe(lambda: target.sketchCurves.count, 0) or 0
    if after_n <= before_n:
        return error(f"copy returned {n} entity(ies) but sketch '{target_name}' still holds "
                     f"{after_n} curve(s) - nothing landed in it.")

    landed = after_n - before_n
    note = ("The new curves' ids are in '" + (target_name or "the target sketch") + "', and an "
            "added curve APPENDS at the end of its kind, so the ids already in use keep their "
            "entities - re-read sketch_get(include_entities=true) for the new ones. "
            "'returned_entity_count' counts the copied endpoints as well as the curves.")
    # The count delta is what proves the copy landed; the refs are the convenience on top. When a
    # copied curve's token does not answer (or two curves share one), say which of the landed curves
    # went unnamed instead of returning a short list that reads as the whole truth.
    if len(new_curves) < landed:
        where = target_name or "the target sketch"
        note = ((f"{landed} curve(s) landed in '{where}' but NONE could be identified by "
                 "entityToken - re-read sketch_get(include_entities=true) for their ids. "
                 if not new_curves else
                 f"{landed} curve(s) landed in '{where}'; only {len(new_curves)} could be "
                 f"identified ({', '.join(new_curves)}) - re-read "
                 "sketch_get(include_entities=true) for the rest. ") + note)
    out = {"copied": True, "sketch": name, "target_sketch": target_name, "entities": refs,
           "new_curves": new_curves, "curve_count_before": before_n, "curve_count_after": after_n,
           "returned_entity_count": n,
           "requested": _requested(unit, dx, dy, rotation_deg, scale_factor),
           "note": note}
    if len(new_curves) < landed:
        out["new_curves_complete"] = False
    return ok(out)


TOOL_DESCRIPTION = (
    "COPY sketch entities, transformed."
)

tool = _transform_wire(
    Tool.create_simple(name="sketch_copy", description=TOOL_DESCRIPTION)
    .add_input_property("sketch_name", {"type": "string"})
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .add_input_property("target_sketch", {"type": "string"})
    .add_input_property(*_sketch_detail.component_scope("target_component",
                                                        narrows="target_sketch")))

# A cross-sketch copy leaves the SOURCE untouched, so the effect is verified on the TARGET. Each
# name is paired with the scope that narrows THAT reference - 'target_sketch' with
# 'target_component' - so the fingerprint reads the sketch the copy wrote, never the source's.
item = Item.create_tool_item(tool=tool, write="write", handler=handler,
                             run_on_main_thread=True,
                             postconditions=[_assert.SketchCurvesChanged(
                                 keys=("target_sketch", "sketch_name"),
                                 scope_keys=("target_component", "component"))])


def register_tool():
    register(item)
