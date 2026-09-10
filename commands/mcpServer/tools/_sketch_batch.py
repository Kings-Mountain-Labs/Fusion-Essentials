# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The batch loop the sketch write tools share: the entries of one call run in order against ONE
resolved sketch, the first failure stops the run, and the payload says what landed, what failed and
what was not attempted."""

from ._common import error, ok

MAP_BLURB = ("the sketch batch substrate: entries_or_error - the list-shape guard naming a bad "
             "entry and its unknown fields; run_batch - the entries of a sketch write run in order "
             "against ONE resolved sketch, stopping at the first failure, publishing the "
             "landed/failed/not_attempted payload every list tool shares")

# Above this an entry list is refused: a call is one turn's work, not a whole drawing.
_MAX_ENTRIES = 200


def entries_or_error(raw, name, allowed):
    """(entries, error): `raw` must be a non-empty list of objects carrying only `allowed` fields."""
    if not isinstance(raw, list) or not raw:
        return None, f"'{name}' must be a non-empty list of entries."
    if len(raw) > _MAX_ENTRIES:
        return None, f"'{name}' holds {len(raw)} entries; the cap is {_MAX_ENTRIES} per call."
    allowed = set(allowed)
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return None, f"{name}[{i}] is not an object."
        unknown = sorted(k for k in entry if k not in allowed)
        if unknown:
            return None, (f"{name}[{i}] carries unknown field(s): {', '.join(unknown)}. "
                          f"An entry takes: {', '.join(sorted(allowed))}.")
    return raw, None


def run_batch(entries, one, name, verb, sketch_name):
    """The MCP result of running `one(index, entry) -> (result, error)` over `entries` in order,
    stopping at the first error: an error when nothing landed, else ok() carrying `verb` (the
    landed count), 'requested', 'sketch', 'results' (one per landed entry, each with its 'index')
    and, when a failure stopped the run, 'failed' {index, error} and 'not_attempted'."""
    results = []
    failed = None
    for i, entry in enumerate(entries):
        res, err = one(i, entry)
        if err:
            failed = {"index": i, "error": err}
            break
        res = dict(res or {})
        res["index"] = i
        results.append(res)
    requested = len(entries)
    if failed and not results:
        rest = requested - 1
        tail = (f" {rest} later entr{'y was' if rest == 1 else 'ies were'} not attempted."
                if rest else "")
        return error(f"{name}[{failed['index']}]: {failed['error']} Nothing landed.{tail}")
    payload = {verb: len(results), "requested": requested, "sketch": sketch_name,
               "results": results}
    note = f"{len(results)} of {requested} {name} landed."
    if failed:
        payload["failed"] = failed
        payload["not_attempted"] = requested - failed["index"] - 1
        note += (f" Stopped at {name}[{failed['index']}]: {failed['error']} The entries before it "
                 f"are in the sketch; {payload['not_attempted']} after it were not attempted.")
    payload["note"] = note
    return ok(payload)
