# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""sketch_delete_entity - remove one '<type>:<index>' target (a curve/point kind from
_common.ENTITY_REF_KINDS, 'constraint', 'dimension', or 'text') from a named sketch, WITHOUT
rebuilding the whole sketch. The delete is verified by reading the collection count back. WRITES."""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe, all_sketch_names, resolve_entity_ref
from . import _common
# The readable handle on a SketchText's string is textParameter.expression, which holds it QUOTED -
# _unquote is sketch_set_text's own reader for it, imported rather than re-rolled here.
from ._sketch_detail import unquote_text as _unquote
from . import _sketch_detail

app = adsk.core.Application.get()


def _constraint_collection(sketch):
    return safe(lambda: sketch.geometricConstraints)


def _unread_count_error(noun, what):
    """The refusal for a collection count that will not read. A count that will not read is not a
    count of zero: coerced to 0 it satisfies the after < before gate with nothing measured behind
    it, and publishes that fabricated zero as the state of the sketch."""
    return (f"The sketch's {noun} count would not read, so {what} cannot be verified - a count that "
            "will not read is not a count of zero. Re-read the sketch with sketch_get.")


def _resolve_constraint(sketch, idx):
    """A geometric constraint by creation-order index, or (None, error_string)."""
    coll = _constraint_collection(sketch)
    if coll is None:
        return None, "This sketch exposes no geometric constraints collection."
    n = _common.counted(lambda: coll.count)
    if n is None:
        return None, _unread_count_error("constraint", f"'constraint:{idx}'")
    if idx < 0 or idx >= n:
        return None, f"constraint index {idx} out of range - the sketch has {n} constraint(s)."
    return safe(lambda: coll.item(idx)), None


def _dimension_collection(sketch):
    return safe(lambda: sketch.sketchDimensions)


def _resolve_dimension(sketch, idx):
    """A sketch dimension by creation-order index - the SAME index sketch_get's X-ray lists it at -
    or (None, error_string)."""
    coll = _dimension_collection(sketch)
    if coll is None:
        return None, "This sketch exposes no sketch dimensions collection."
    n = _common.counted(lambda: coll.count)
    if n is None:
        return None, _unread_count_error("dimension", f"'dimension:{idx}'")
    if idx < 0 or idx >= n:
        return None, f"dimension index {idx} out of range - the sketch has {n} dimension(s)."
    return safe(lambda: coll.item(idx)), None


def _text_collection(sketch):
    return safe(lambda: sketch.sketchTexts)


def _resolve_text(sketch, idx):
    """A sketch text by creation-order index - the SAME index sketch_set_text edits by - or
    (None, error_string)."""
    coll = _text_collection(sketch)
    if coll is None:
        return None, "This sketch exposes no sketch texts collection."
    n = _common.counted(lambda: coll.count)
    if n is None:
        return None, _unread_count_error("sketch text", f"'text:{idx}'")
    if idx < 0 or idx >= n:
        return None, f"text index {idx} out of range - the sketch has {n} sketch text(s)."
    return safe(lambda: coll.item(idx)), None


def _unverified_delete(ref, noun, before):
    """The refusal for a delete whose AFTER count will not read: the removal is unverified, and the
    coerced zero it replaces both passes the after < before gate and publishes '<noun>s_after: 0'."""
    return error(f"Delete of {ref} was called but the sketch's {noun} count would not read back, so "
                 f"whether it was removed is UNVERIFIED - a count that will not read is not a count "
                 f"of zero. The sketch held {before} {noun}(s) before the call. Re-read it with "
                 "sketch_get before deleting more.")


def handler(sketch_name: str = "", target: str = "", component: str = "") -> dict:
    # Dispatch '<type>:<index>': curves/points via the shared resolve_entity_ref, constraints via a
    # local index into geometricConstraints; both paths gate on a before/after collection count.
    design = _common.design()
    if not design:
        return error("No active design.")

    # Resolve across the whole design, so a sketch in an activated sub-component is reachable.
    wanted = (sketch_name or "").strip()
    sketch, refusal = _sketch_detail.scoped_sketch(design, wanted, component)
    if refusal:
        return error(refusal)
    if not sketch:
        names = all_sketch_names(design)
        return error(f"No sketch named '{wanted}'. Available: "
                     + (", ".join(n for n in names if n) or "(none)") + ". Use sketch_get.")

    ref = (target or "").strip().lower()
    if ":" not in ref:
        return error("Provide 'target' as '<type>:<index>' - type = "
                     + " | ".join(_common.ENTITY_REF_KINDS)
                     + " | constraint | dimension | text (e.g. 'circle:0', 'dimension:2'). "
                     "sketch_get(include_entities=true) lists the curve/constraint/dimension "
                     "indexes; a text index is the one sketch_set_text edits by.")
    kind, _, idx_s = ref.rpartition(":")
    try:
        idx = int(idx_s)
    except Exception:
        return error(f"'{target}' has a non-integer index; use '<type>:<index>' (e.g. 'line:1').")

    # --- constraint path (resolved here; resolve_entity_ref only knows curves/points) ---
    if kind == "constraint":
        coll = _constraint_collection(sketch)
        before = _common.counted(lambda: coll.count) if coll is not None else None
        ent, cerr = _resolve_constraint(sketch, idx)
        if cerr:
            return error(cerr)
        if before is None:
            return error(_unread_count_error("constraint", f"deleting {ref}")
                         + " Nothing was deleted.")
        ctype = safe(lambda: type(ent).__name__)
        try:
            # The MUTATION - not safe-wrapped, so a genuine failure raises and is reported.
            did = ent.deleteMe()
        except Exception as e:
            return error(f"Could not delete {ref}: {e}")
        after = _common.counted(lambda: coll.count)
        if after is None:
            return _unverified_delete(ref, "constraint", before)
        if not did or after >= before:
            return error(f"Delete of {ref} did not take (constraint count {before} -> {after}). "
                         "It may be a fixed/driving constraint the solver won't remove.")
        return ok({
            "deleted": True,
            "target": ref,
            "entity_type": ctype,
            "sketch": safe(lambda: sketch.name),
            "constraints_before": before,
            "constraints_after": after,
            "note": "Constraint removed. Re-constrain if needed (see sketch_constrain).",
        })

    # --- dimension path (sketchDimensions is its own collection, indexed the way the X-ray lists it) ---
    if kind == "dimension":
        coll = _dimension_collection(sketch)
        before = _common.counted(lambda: coll.count) if coll is not None else None
        ent, derr = _resolve_dimension(sketch, idx)
        if derr:
            return error(derr)
        if before is None:
            return error(_unread_count_error("dimension", f"deleting {ref}")
                         + " Nothing was deleted.")
        # Captured BEFORE the mutation: the parameter name is what tells the caller WHICH
        # dimension went.
        pname = safe(lambda: ent.parameter.name)
        try:
            # The MUTATION - not safe-wrapped, so a genuine failure raises and is reported.
            did = ent.deleteMe()
        except Exception as e:
            return error(f"Could not delete {ref}: {e}")
        after = _common.counted(lambda: coll.count)
        if after is None:
            return _unverified_delete(ref, "dimension", before)
        if not did or after >= before:
            return error(f"Delete of {ref} did not take (dimension count {before} -> {after}). "
                         "The dimension is still in the sketch.")
        return ok({
            "deleted": True,
            "target": ref,
            "parameter": pname,
            "sketch": safe(lambda: sketch.name),
            "dimensions_before": before,
            "dimensions_after": after,
            "note": ("Dimension removed. Re-read sketch_get(include_entities=true) for the "
                     "sketch's remaining dimensions and its constrained state."),
        })

    # --- sketch-text path (sketchTexts is neither a SketchCurves sub-collection nor a constraint,
    # so it has its own resolve; sketch_set_text creates and edits these by the same index) ---
    if kind == "text":
        coll = _text_collection(sketch)
        before = _common.counted(lambda: coll.count) if coll is not None else None
        ent, terr = _resolve_text(sketch, idx)
        if terr:
            return error(terr)
        if before is None:
            return error(_unread_count_error("sketch text", f"deleting {ref}")
                         + " Nothing was deleted.")
        # The string is captured BEFORE the mutation - a deleted text's wrapper is not guaranteed to
        # still answer, and this is what tells the caller WHICH text went.
        content = _unquote(safe(lambda: ent.textParameter.expression))
        try:
            # The MUTATION - not safe-wrapped, so a genuine failure raises and is reported.
            did = ent.deleteMe()
        except Exception as e:
            return error(f"Could not delete {ref}: {e}")
        after = _common.counted(lambda: coll.count)
        if after is None:
            return _unverified_delete(ref, "sketch text", before)
        if not did or after >= before:
            return error(f"Delete of {ref} did not take (sketch text count {before} -> {after}). "
                         "The text is still in the sketch.")
        return ok({
            "deleted": True,
            "target": ref,
            "text": content,
            "sketch": safe(lambda: sketch.name),
            "texts_before": before,
            "texts_after": after,
            "note": "Sketch text removed. Create a replacement with sketch_set_text(create=true).",
        })

    # --- curve/point path (shared resolver + count read-back on the matching collection) ---
    if kind not in _common.ENTITY_REF_KINDS:
        return error(f"Unknown target type '{kind}'. Use "
                     + " | ".join(_common.ENTITY_REF_KINDS)
                     + " | constraint | dimension | text.")

    # Count the SAME collection resolve_entity_ref indexes, so the read-back proves this delete.
    coll = _common.entity_collection(sketch, kind)
    before = _common.counted(lambda: coll.count) if coll is not None else None

    ent = resolve_entity_ref(sketch, ref)
    if ent is None:
        held = (f"has {before} {kind}(s)" if before is not None
                else f"reports no readable {kind} count")
        return error(f"Could not resolve {ref} - the sketch {held}. "
                     "Indexes are 0-based in creation order; list them with sketch_get.")
    if before is None:
        return error(_unread_count_error(kind, f"deleting {ref}") + " Nothing was deleted.")
    try:
        did = ent.deleteMe()
    except Exception as e:
        return error(f"Could not delete {ref}: {e}")
    after = _common.counted(lambda: coll.count)
    if after is None:
        return _unverified_delete(ref, kind, before)
    if not did or after >= before:
        return error(f"Delete of {ref} did not take ({kind} count {before} -> {after}). The entity "
                     "may be consumed by a dimension/constraint - remove those first.")
    return ok({
        "deleted": True,
        "target": ref,
        "sketch": safe(lambda: sketch.name),
        f"{kind}s_before": before,
        f"{kind}s_after": after,
        "note": ("Entity removed. Deleting a curve can cascade to constraints/dimensions that "
                 "referenced it; re-read with sketch_get before adding more."),
    })


TOOL_DESCRIPTION = (
    "Delete ONE sketch entity, constraint, dimension or text, named as '<type>:<index>'."
)

tool = (
    Tool.create_with_string_input(
        name="sketch_delete_entity",
        description=TOOL_DESCRIPTION,
        input_param_name="sketch_name",
        input_param_description="The sketch holding it.",
    )
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .add_input_property("target", {"type": "string"})
    .add_required_input("target")
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_sketch_delete_entity.py::TestDeleteCurve"
                      "::test_a_delete_that_reports_true_but_removes_nothing_is_an_error"))


def register_tool():
    register(item)
