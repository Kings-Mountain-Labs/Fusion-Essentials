# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""cam_inspect_toolpaths - the toolpath validity verdict ("are these toolpaths generated and up to
date?") for the whole document or one named scope. CAM.checkToolpath takes an Operation, Setup,
Folder, or Pattern and covers every operation nested under it; CAM.checkAllToolpaths is the whole
document."""

import adsk.cam

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe, told_apart
from . import _outputs
# The breakdown classifies from the same reads every other CAM tally uses.
from ._cam_common import (clamp_rows, get_cam, resolve_cam_node, operations_under, walk_operations,
                          setups, first_error_line, is_empty_toolpath, op_state_facts,
                          op_primary_state, validity_basis)

RETURNS = [_outputs.ReturnsVerdict(relations=("toolpaths_valid",))]

_ROWS_CAP = 25                                              # default cap on the per-operation rows
_ROWS_MAX = 200        # the ceiling: every row crosses the wire, so max_results cannot lift it away
_SCOPE_KINDS = ("setup", "folder", "pattern", "operation")   # what checkToolpath accepts as its target
# op_primary_state's whole vocabulary: one mutually-exclusive bucket per operation.
_STATE_NAMES = ("valid", "out_of_date", "no_toolpath", "error", "suppressed", "generating",
                "unread")
_EMPTY_NAMES_CAP = 5     # how many empty-setup names the note spells out; the count is always exact
_EMPTY_OP_NAMES_CAP = 10   # how many empty-toolpath operations measured names; its count is exact


def _split_suppressed(ops):
    """(active, suppressed_count) over a walked operation list, split on op_primary_state's own
    'suppressed' bucket: suppressing an operation DISCARDS its toolpath, so it carries none for the
    tally or the rows to describe."""
    active, suppressed = [], 0
    for raw in ops or []:
        op = adsk.cam.Operation.cast(raw)
        if op is not None and op_primary_state(op_state_facts(op)) == "suppressed":
            suppressed += 1
            continue
        active.append(raw)
    return active, suppressed


def _scope_operations(cam, scope):
    """(target, ops, label, err) - no scope is the checkAllToolpaths path over every operation in
    the document; a NAME resolves through the shared CAM resolver and scopes the breakdown."""
    want = (scope or "").strip()
    if not want:
        return None, walk_operations(cam), "document", None
    node, rerr = resolve_cam_node(cam, want, kinds=_SCOPE_KINDS,
                                  label="setup/folder/pattern/operation")
    if rerr:
        return None, None, None, rerr + " Omit 'scope' to check the whole document."
    ops = [node.obj] if node.kind == "operation" else operations_under(node.obj)
    return node.obj, ops, f"{node.kind} '{node.name or want}'", None


def _classify(cam, ops, cap):
    """(states, rows, truncated, empty) - ONE walk over the operations bucketed by op_primary_state;
    past the cap the tally keeps counting and only the rows stop. `empty` is the (name,
    discriminator) overlay of operations that generated and CUT NOTHING, which bucket 'valid'."""
    states = {name: 0 for name in _STATE_NAMES}
    rows = []
    empty = []
    truncated = False
    total = 0
    for raw in ops:
        op = adsk.cam.Operation.cast(raw)
        if op is None:
            continue
        facts = op_state_facts(op, cam)
        state = op_primary_state(facts)
        states[state] = states.get(state, 0) + 1
        total += 1
        if is_empty_toolpath(facts):
            name = facts["name"]
            empty.append((name, f"{name} (operation {total})" if name else ""))
        if state == "valid":
            continue
        if len(rows) >= cap:
            truncated = True
            continue
        row = {"operation": facts["name"], "state": state}
        if state == "error":
            row["error"] = first_error_line(op)
        rows.append(row)
    states["total"] = total
    return states, rows, truncated, empty


_FALLBACK = "per-setup fallback"
_OP_FALLBACK = "per-operation fallback"
_EMPTY_SCOPE = "empty scope"

# The paths whose verdict is THIS TOOL's own AND over the operations it counted. Everywhere else
# 'passed' is CAM's own check, which include_suppressed does not narrow: checkToolpath answers False
# for a setup whose only non-valid operation is SUPPRESSED, and for that operation asked directly.
_TOOL_SCOPED_VERDICT = (_OP_FALLBACK, _EMPTY_SCOPE)


def _verdict_counts(checked, tally_counts):
    """Which operations 'passed' covers - NOT always the set the tally counted. Published beside
    the tally's own scope so the two can never be read as one number."""
    return tally_counts if checked in _TOOL_SCOPED_VERDICT else "all_operations"


def _and_children(cam, children):
    """The AND of checkToolpath over each child - the ONE fallback both scopes take when the check on
    the wider target raises. A non-boolean verdict is returned as-is for the caller's verdict guard
    to name, never AND-ed into a lie."""
    verdicts = [cam.checkToolpath(c) for c in children]
    odd = [v for v in verdicts if not isinstance(v, bool)]
    return odd[0] if odd else all(verdicts)


def _empty_setup_names(empty):
    """The names of the setups excluded from the per-setup AND, for the note - capped, with the
    overflow MARKED rather than dropped."""
    names = [safe(lambda s=s: s.name) or "(unnamed)" for s in empty[:_EMPTY_NAMES_CAP]]
    dropped = len(empty) - len(names)
    if dropped > 0:
        names.append(f"... and {dropped} more")
    return names


def _document_verdict(cam):
    """(verdict, checked, empty_setups, err) for the whole document. checkAllToolpaths RAISES "the
    operations are not CAM objects" on some documents, so the per-setup AND is the fallback, and a
    setup holding ZERO operations - which raises the same way - is excluded and named instead."""
    try:
        return cam.checkAllToolpaths(), "checkAllToolpaths", [], None
    except Exception as first:
        populated, empty = [], []
        for s in setups(cam):
            (populated if operations_under(s) else empty).append(s)
        try:
            return _and_children(cam, populated), _FALLBACK, empty, None
        except Exception as second:
            return None, None, [], (f"The toolpath validity check failed for the document: "
                                    f"checkAllToolpaths raised {first}; the per-setup fallback "
                                    f"raised {second}.")


def _scoped_verdict(cam, target, ops, label, scope_is_empty):
    """(verdict, checked, err) for ONE named setup/folder/pattern/operation: checkToolpath on the
    target, falling back to the AND over the operations under it. `scope_is_empty` marks a target
    holding NO operations, which raises and has no narrower target to ask."""
    try:
        return cam.checkToolpath(target), "checkToolpath", None
    except Exception as first:
        if scope_is_empty:
            return True, _EMPTY_SCOPE, None
        children = [o for o in (ops or []) if o is not target]
        if not children:
            return None, None, f"The toolpath validity check failed for {label}: {first}."
        try:
            return _and_children(cam, children), _OP_FALLBACK, None
        except Exception as second:
            return None, None, (f"The toolpath validity check failed for {label}: checkToolpath "
                                f"raised {first}; the per-operation fallback raised {second}.")


def _suppressed_clause(checked, suppressed_excluded, include_suppressed) -> str:
    """The clause for the suppressed operations the tally left out, naming whether 'passed' counted
    them anyway - the one condition under which the two numbers cover different sets."""
    if include_suppressed or not suppressed_excluded:
        return ""
    if checked in _TOOL_SCOPED_VERDICT:
        return f"{suppressed_excluded} suppressed op(s) excluded from the tally."
    return f"{suppressed_excluded} suppressed op(s) excluded from the tally, counted by 'passed'."


def _note(passed, label, states, rows, truncated, basis, checked, suppressed_excluded,
          include_suppressed, empty_setups, empty_ops=()):
    """What this read observed and the next step - one clause per condition that changes how the
    payload's own numbers read; every other fact rides as the key that carries it."""
    total = states["total"]
    outside = total - states["valid"]
    verdict = "passed" if passed else "failed"
    if not total:
        parts = [f"{label}: no operations counted; the validity check {verdict} over an empty set."]
    else:
        parts = [f"{label}: the validity check {verdict}; {outside} of {total} counted op(s) "
                 "outside valid (measured.not_valid)."]
    parts.append(_suppressed_clause(checked, suppressed_excluded, include_suppressed))
    if empty_ops:
        # These sit INSIDE states['valid'], so no number in the payload separates them.
        parts.append(f"{len(empty_ops)} counted op(s) read valid and cut nothing "
                     "(measured.empty_toolpaths).")
    if truncated:
        parts.append(f"not_valid capped at {len(rows)} - raise max_results.")
    if basis != "manufacture_verified":
        parts.append("Enter Manufacture to trust op validity.")
    if outside:
        parts.append("cam_generate regenerates the out-of-date ops.")
    return " ".join(p for p in parts if p)


def handler(scope: str = "", max_results: int = _ROWS_CAP,
            include_suppressed: bool = False) -> dict:
    """See TOOL_DESCRIPTION."""
    cam, cerr = get_cam()
    if cerr:
        return error(cerr)

    target, raw_ops, label, serr = _scope_operations(cam, scope)
    if serr:
        return error(serr)

    # ONE split feeds the tally, the rows and the per-operation fallback's AND. CAM's own check
    # paths are not narrowed by it; tolerance_used.verdict_counts says which of the two answered.
    include_suppressed = bool(include_suppressed)
    active_ops, suppressed = _split_suppressed(raw_ops)
    ops = raw_ops if include_suppressed else active_ops
    suppressed_excluded = 0 if include_suppressed else suppressed

    if target is None:
        verdict, checked, empty_setups, verr = _document_verdict(cam)
        if verr:
            return error(verr)
    else:
        empty_setups = []
        # The empty-scope answer keys on the RAW census - a suppression filter must not be able to
        # manufacture an empty scope.
        verdict, checked, verr = _scoped_verdict(cam, target, ops, label, not raw_ops)
        if verr:
            return error(verr)
    if not isinstance(verdict, bool):
        return error(f"The toolpath validity check returned {type(verdict).__name__} for {label}, "
                     "not a true/false verdict - there is no verdict to report.")

    states, rows, truncated, empty_ops = _classify(
        cam, ops, clamp_rows(max_results, _ROWS_CAP, _ROWS_MAX))
    basis = validity_basis()
    tally_counts = "all_operations" if include_suppressed else "active_operations"
    return ok({
        "relation": "toolpaths_valid",
        "passed": verdict,
        "checked": checked,                     # which API path the verdict came from
        "measured": {"scope": label, "states": states, "not_valid": rows,
                     "not_valid_truncated": truncated,
                     # what the tally left out, so a verdict over 13 of 78 operations says so
                     "suppressed_excluded": suppressed_excluded,
                     # A setup holding no operations raises the check and is not asked; the count is
                     # exact and the names are capped, with the overflow marked.
                     "empty_setups_excluded": len(empty_setups),
                     "empty_setups": _empty_setup_names(empty_setups),
                     # the overlay on states['valid'] - generated, up to date, and cutting nothing.
                     # The COUNT is exact; the names are capped (told_apart first, so a name listed
                     # inside the cap is still separated from a namesake outside it).
                     "empty_toolpath_count": len(empty_ops),
                     "empty_toolpaths": told_apart(empty_ops)[:_EMPTY_OP_NAMES_CAP]},
        # tally_counts describes states/not_valid; verdict_counts describes 'passed'. They are
        # SEPARATE keys because they genuinely differ - CAM's own check counts suppressed ops.
        "tolerance_used": {"criterion": "valid_and_up_to_date",
                           "tally_counts": tally_counts,
                           "verdict_counts": _verdict_counts(checked, tally_counts),
                           "validity_basis": basis},
        "note": _note(verdict, label, states, rows, truncated, basis, checked,
                      suppressed_excluded, include_suppressed, empty_setups, empty_ops),
    })


TOOL_DESCRIPTION = (
    "Check whether CAM toolpaths are generated and up to date.\n"
    + _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="cam_inspect_toolpaths", description=TOOL_DESCRIPTION)
    .add_input_property("scope", {"type": "string"})
    .add_input_property("max_results", {"type": "integer"})
    .add_input_property("include_suppressed", {"type": "boolean",
            "description": "Suppressing flips hasToolpath to False and only cam_generate "
                           "brings it back."})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
