# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Launch CAM toolpath generation asynchronously and return a poll handle. Generation runs in the
background at its own pace once launched. The live GenerateToolpathFuture must stay referenced
across calls - see _cam_common.register_future - or Fusion abandons the in-progress generation."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import named_with_remainder, ok, error, safe
from . import _outputs
from . import _cam_common   # the shared CAM substrate: op_labels, the entitlement flags, the registry

RETURNS = [
    _outputs.ReturnsValue("handle", "a generation handle - check cam_get_status(handle) until "
                          "completed", consumers=["cam_get_status"]),
]


# The registry and the handles it mints live in _cam_common - the ONE registration path every
# launch goes through, this tool's and the inline ones in cam_select_geometry and
# cam_create_operation alike. This name is that same object.
register_future = _cam_common.register_future


_LAUNCH_NOTE = ("Generation is launched and runs in the background at its own pace - the compute "
                "is often minutes. Check cam_get_status(handle) at whatever cadence you need the "
                "progress, until completed=true. operations_to_generate is the scope this launch "
                "covers; the progress counters populate on the first check.")

# The remedy for THIS call site: cam_generate takes no strategy, so the operation itself is what
# changes - picking a different strategy is a create-time choice.
_ENTITLEMENT_REMEDY = ("Check the Machining Extension entitlement, or replace one: cam_delete + "
                       "cam_create_operation with an allowed strategy "
                       "(cam_get(include=['strategies']) lists them).")

_ALL_BLOCKED = ("Nothing was launched: every operation in scope reads isGenerationAllowed false on "
                "its own strategy ({names}). " + _ENTITLEMENT_REMEDY)

_UNREAD_ENTITLEMENT = " {n} more: isGenerationAllowed did not read, so not excluded."

# The split launch's own note: the excluded operations and their remedy take the room the whole-
# document sentence would, so this one keeps the poller and the reasons key and drops the rest.
_SPLIT_LAUNCH_NOTE = "Launched - check cam_get_status(handle) until completed=true."

# What launched_operations / launch_reasons describe: this call's OWN pre-launch walk over the
# scope, the same walk operations_to_generate counts.
_REASONS_NOTE = (
    " launch_reasons tallies the state each operation read BEFORE this launch, and "
    "launched_operations names them: out_of_date, no_toolpath (never generated), errored, "
    "valid_forced (valid, and covered anyway) or state_unread.")

# MEASURED: cam.generateToolpath over a SETUP regenerated all four of its operations - two of them
# already valid - under skip_valid=true. The flag narrows the DOCUMENT sweep only, so a scoped
# launch says so rather than leaving the caller to read valid_forced rows as a fault.
_SKIP_VALID_UNUSED = (
    " skip_valid was requested but NOT applied: this launch names a {scope}, and generateToolpath "
    "regenerates its whole target whatever the flag says - the valid_forced rows are that, not a "
    "stale read. Omit 'target' for the document sweep, which the flag does narrow.")


def _scope_nodes(cam, node):
    """The operation NODES one launch covers: the whole document, or the target subtree - an
    operation target is its own single-node scope."""
    if node is None:
        return _cam_common.operation_nodes(cam)
    return [node] if node.kind == "operation" else _cam_common.operation_nodes_under(node)


def _blocked_clause(rows) -> str:
    """The launch note's sentence for the operations this launch EXCLUDED."""
    return (f" {len(rows)} operation(s) EXCLUDED - their strategy reads isGenerationAllowed false: "
            f"{named_with_remainder([r['name'] for r in rows])}. " + _ENTITLEMENT_REMEDY)


# Why an operation is in a launch, off the state it read BEFORE the launch. 'no_toolpath' is the
# never-generated one (OperationStates.NoToolpath); 'valid_forced' is an already-valid operation the
# launch covers anyway; 'state_unread' is an operationState that answered nothing.
_LAUNCH_REASON = {0: "valid_forced", 1: "out_of_date", 3: "no_toolpath"}

# How many launched rows the payload NAMES; past this the tally is what describes the launch.
_LAUNCH_ROWS_CAP = 50


def _launch_reason(facts) -> str:
    """The reason one operation is in this launch - its own pre-launch state, named."""
    if facts.get("has_error"):
        return "errored"
    return _LAUNCH_REASON.get(facts.get("operation_state"), "state_unread")


def _launch_set(rows, skip_valid):
    """(the (label, node, reason) rows a launch builds a toolpath for, suppressed count,
    already-valid count) - a suppressed operation carries no toolpath to build, and skip_valid
    passes over the ones already reading operationState IsValid (0)."""
    covered, parked, already_valid = [], 0, 0
    for label, node in rows:
        facts = _cam_common.op_state_facts(node.obj)
        if _cam_common.op_is_suppressed(facts):
            parked += 1
        elif skip_valid and facts["operation_state"] == 0:
            already_valid += 1
        else:
            covered.append((label, node, _launch_reason(facts)))
    return covered, parked, already_valid


def _launch_rows(payload, covered):
    """Fold the launched operations into `payload`: the reason TALLY over all of them, and one named
    row each up to the cap, with the overflow flagged rather than silently cut."""
    tally = {}
    for _label, _node, reason in covered:
        tally[reason] = tally.get(reason, 0) + 1
    payload["launch_reasons"] = tally
    payload["launched_operations"] = [{"operation": label, "reason": reason}
                                      for label, _node, reason in covered[:_LAUNCH_ROWS_CAP]]
    if len(covered) > _LAUNCH_ROWS_CAP:
        payload["launched_operations_truncated"] = True


def _nothing_to_launch(target_desc, parked, already_valid) -> dict:
    """The payload for a scope no launch was made over - the ONE builder both arms return, so a
    skip cannot read one way here and another there. No Future is minted, so this reports skipped
    rather than sending the caller to poll a generation nobody started, and the hint names whatever
    left the scope empty: an exclusion, or the scope holding no operations at all."""
    payload = {"launched": False, "skipped": True, "target": target_desc}
    if not parked and not already_valid:
        payload["reason"] = "no operations in scope - there is nothing to generate."
        payload["hint"] = ("Add one with cam_create_operation, or read what the document holds "
                           "with cam_get(include=['operations']).")
        return payload
    payload["reason"] = (f"nothing in scope needed a launch ({already_valid} already valid, "
                         f"{parked} suppressed).")
    payload["hint"] = ("Pass skip_valid=false to force-regenerate the valid one(s)."
                       if already_valid else
                       "Restore a suppressed operation with cam_edit_operation(suppressed=false), "
                       "then re-run.")
    return payload


def _launch_around_blocked(cam, keep, blocked, skip_valid, scope, target_desc, resolved_name,
                           unread):
    """The launch for a scope holding entitlement-blocked operations: the whole-scope sweep
    regenerates NOTHING over such a scope, so every operation that did not read false is launched on
    its own, all under one handle."""
    futures, failures, launched = [], [], []
    covered, parked, already_valid = _launch_set(keep, skip_valid)
    for label, node, reason in covered:
        try:
            fut = cam.generateToolpath(node.obj)
        except Exception as e:
            failures.append({"name": label, "error": str(e)})
            continue
        if not fut:
            failures.append({"name": label, "error": "generateToolpath returned no future."})
            continue
        futures.append(fut)
        launched.append((label, node, reason))

    if not futures:
        if failures:
            named = named_with_remainder([f"{f['name']}: {f['error']}" for f in failures])
            return error(f"No generation launched in {target_desc}: every operation outside the "
                         f"{len(blocked)} that read isGenerationAllowed false failed to launch "
                         f"({named}).")
        if not keep:
            return error(_ALL_BLOCKED.format(
                names=named_with_remainder([r["name"] for r in blocked])))
        skipped = _nothing_to_launch(target_desc, parked, already_valid)
        skipped.update({"entitlement_blocked": blocked, "note": _blocked_clause(blocked)})
        return ok(skipped)

    handle, _total = register_future(futures[0], target_desc, scope, skip_valid,
                                     target_name=resolved_name, also=futures[1:])
    payload = {
        "launched": True,
        "handle": handle,
        "target": target_desc,
        "skip_valid": bool(skip_valid),
        "operations_to_generate": len(futures),
        "entitlement_blocked": blocked,
        "note": _SPLIT_LAUNCH_NOTE + _REASONS_NOTE + _blocked_clause(blocked),
    }
    _launch_rows(payload, launched)
    if failures:
        payload["launch_failures"] = failures       # named in the payload, not restated in the note
    if unread:
        payload["entitlement_unread"] = unread
        payload["note"] += _UNREAD_ENTITLEMENT.format(n=unread)
    return ok(payload)


def handler(target: str = "", skip_valid: bool = True) -> dict:
    """Launch toolpath (re)generation over `target` and return a poll handle immediately."""
    cam, err = _cam_common.get_cam()
    if err:
        return error(err)

    want = (target or "").strip()
    node = None
    if want and want.lower() not in ("all", "document", "*"):
        node, rerr = _cam_common.resolve_cam_node(
            cam, want, kinds=("setup", "folder", "operation"), label="setup/folder/operation")
        if rerr:
            return error(rerr + " Omit 'target' to generate the whole document.")
        # generateToolpath has no skip_valid flag; it regenerates the given target. When the
        # caller asked to skip valid and this single target is already valid+current, short out.
        if skip_valid and node.kind == "operation" and safe(lambda: node.obj.operationState) == 0:
            return ok({"launched": False, "skipped": True, "target": want,
        "reason": "operation already valid and up to date (skip_valid=true).",
        "hint": "Pass skip_valid=false to force-regenerate it."})

    scope = "document" if node is None else (node.kind or "target")
    target_desc = "all setups" if node is None else f"{scope} '{want}'"
    resolved_name = "" if node is None else (node.name or want)

    # The entitlement pre-flight, before the launch: the whole-scope sweep regenerates nothing over a
    # scope holding operations that read isGenerationAllowed false, so those are launched around.
    nodes = _scope_nodes(cam, node)
    labels = _cam_common.op_labels(nodes)
    flags = _cam_common.entitlement_flags([n.obj for n in nodes])
    blocked = [{"name": label, "strategy": safe(lambda n=n: n.obj.strategy)}
               for label, n, flag in zip(labels, nodes, flags) if flag is False]
    unread = sum(1 for flag in flags if flag is None)
    if blocked:
        keep = [(label, n) for label, n, flag in zip(labels, nodes, flags) if flag is not False]
        return _launch_around_blocked(cam, keep, blocked, skip_valid, scope, target_desc,
                                      resolved_name, unread)

    # generateToolpath regenerates its whole target whatever skip_valid says; only the document
    # sweep is handed the flag, so only there does it narrow what this launch covers.
    narrowing = bool(skip_valid) and node is None
    # The count is this call's OWN walk, not future.numberOfOperations - that counter reads a
    # different collection (2 over a six-operation turning setup, measured) - and it runs BEFORE
    # the launch, which moves the operation_state the walk reads.
    covered, parked, already_valid = _launch_set(list(zip(labels, nodes)), narrowing)
    if not covered:
        return ok(_nothing_to_launch(target_desc, parked, already_valid))

    try:
        future = (cam.generateAllToolpaths(bool(skip_valid)) if node is None
                  else cam.generateToolpath(node.obj))
    except Exception as e:
        return error(f"Failed to launch generation for {scope}: {e}")

    if not future:
        return error("Generation launch returned no future (nothing to generate?).")

    handle, _total = register_future(future, target_desc, scope, skip_valid,
                                     target_name=resolved_name)

    payload = {
        "launched": True,
        "handle": handle,
        "target": target_desc,
        "skip_valid": bool(skip_valid),
        "operations_to_generate": len(covered),
        "note": _LAUNCH_NOTE + _REASONS_NOTE,
    }
    _launch_rows(payload, covered)
    if skip_valid and node is not None:
        payload["skip_valid_applied"] = False    # absent = the flag narrowed this launch
        payload["note"] += _SKIP_VALID_UNUSED.format(scope=scope)
    if unread:
        payload["entitlement_unread"] = unread
        payload["note"] += _UNREAD_ENTITLEMENT.format(n=unread)
    return ok(payload)


TOOL_DESCRIPTION = (
    "Launch CAM toolpath (re)generation from the MANUFACTURE workspace.\n"
    + _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="cam_generate", description=TOOL_DESCRIPTION)
    .add_input_property("target", {"type": "string"})
    .add_input_property("skip_valid", {"type": "boolean",
            "description": "Skips operations that are already valid."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="deferred", poller="cam_get_status",
        evidence_test="tests/unit/test_cam_generate.py::TestLaunchHandsOffToTheStatusRead"
                      "::test_the_launch_claims_no_completion_and_names_the_poller",
        rung="exists"))


def register_tool():
    register(item)
