# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Set the comment (and/or name) field on the active document's NC programs, one or all of them."""

import adsk.core
import adsk.cam

app = adsk.core.Application.get()

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import iter_collection, ok, error, safe
# The CAM string-parameter codec is the shared substrate's: one home, so the write's quoting and the
# read-back's unquoting cannot be right here and stale in the next CAM tool that compares them.
from ._cam_common import (get_cam, quote_expression as _quote,
                          unquote_expression as _unquote)

_COMMENT_PARAM = "nc_program_comment"
_NAME_PARAM = "nc_program_name"


def _set_param(ncp, internal_name, value):
    """(before, after, error) - set a CAM string parameter on the NC program and CONFIRM it kept
    the value; a parameter can accept the assignment and keep the expression it already held."""
    param = safe(lambda: ncp.parameters.itemByName(internal_name))
    if param is None:
        return None, None, f"parameter '{internal_name}' not found on this NC program"
    if not safe(lambda: param.isEditable, True):
        return None, None, f"parameter '{internal_name}' is not editable"
    before = _unquote(safe(lambda: param.expression))
    wrote = _quote(value)
    try:
        param.expression = wrote
    except Exception as e:
        return before, None, str(e)
    raw_after = safe(lambda: param.expression)
    if raw_after is None:
        return before, None, (f"'{internal_name}' cannot be read back after the write, so the "
                              "change is UNCONFIRMED")
    after = _unquote(raw_after)
    if after != _unquote(wrote):
        return before, after, (f"the write did not take - '{internal_name}' reads back '{after}' "
                               f"after being set to '{_unquote(wrote)}'")
    return before, after, None


def handler(comment: str = "", program: str = "", set_name: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    # Guard against the silent wipe-all: refuse when there's genuinely nothing to write - an
    # empty/whitespace comment AND no set_name. (An empty comment WITH a set_name is fine: the
    # caller is renaming, not clearing comments; an explicit non-empty comment is fine.)
    write_comment = bool((comment or "").strip())
    write_name = bool((set_name or "").strip())
    if not write_comment and not write_name:
        return error("Provide a non-empty 'comment' (and/or 'set_name') - the value(s) to write. "
    "Refusing: an empty comment with no name would blank the comment on every "
    "matched NC program.")

    cam, err = get_cam()
    if err:
        return error(err)

    programs = safe(lambda: cam.ncPrograms)
    count = safe(lambda: programs.count, 0) if programs else 0
    if not count:
        return error("This document has no NC programs.")

    want = (program or "").strip()
    present = [(ncp, safe(lambda ncp=ncp: ncp.name)) for ncp in iter_collection(programs)]
    targets = [(ncp, nm or "") for ncp, nm in present if not want or (nm or "") == want]

    if not targets:
        available = [nm for _, nm in present]
        return error(f"No NC program named '{program}'. Available: "
                      f"{', '.join(str(a) for a in available)}.")

    # Pre-validate every target's params BEFORE writing: there is no CAM transaction here, so a
    # mid-loop failure would leave the earlier programs already mutated.
    for ncp, nm in targets:
        if write_comment:
            p = safe(lambda ncp=ncp: ncp.parameters.itemByName(_COMMENT_PARAM))
            if p is None:
                return error(f"NC program '{nm}' has no '{_COMMENT_PARAM}' parameter; aborting "
    "before any change.")
            if not safe(lambda p=p: p.isEditable, True):
                return error(f"Comment on NC program '{nm}' is not editable; aborting before any "
    "change (nothing was modified).")
        if write_name:
            p = safe(lambda ncp=ncp: ncp.parameters.itemByName(_NAME_PARAM))
            if p is None or not safe(lambda p=p: p.isEditable, True):
                return error(f"Name on NC program '{nm}' is not editable/found; aborting before "
    "any change (nothing was modified).")

    results = []
    for ncp, nm in targets:
        rec = {"program": nm}
        if write_comment:
            before, after, e = _set_param(ncp, _COMMENT_PARAM, comment)
            if e:
                return error(f"Failed to set comment on NC program '{nm}': {e}. NOTE: any "
    "programs processed before this one were already changed.")
            rec["comment_before"] = before
            rec["comment_after"] = after
        if write_name:
            before, after, e = _set_param(ncp, _NAME_PARAM, set_name)
            if e:
                kept = (f" The comment on '{nm}' reads '{rec.get('comment_after')}' and remains."
                        if write_comment else "")
                return error(f"Failed to set name on NC program '{nm}': {e}. NOTE: any programs "
    f"processed before this one were already changed.{kept}")
            rec["name_before"] = before
            rec["name_after"] = after
        results.append(rec)

    return ok({
        "set": True,
        "comment": comment if write_comment else None,
    "set_name": (set_name or None) if write_name else None,
    "programs_changed": len(results),
    "programs": results,
    "note": "NC program comment/name updated. Most posts emit the Comment near the top of "
    "the G-code. (No re-post is performed.)",
    })


TOOL_DESCRIPTION = (
    "Set the COMMENT field of the active document's NC programs - what most posts emit near the "
    "top of the G-code."
)

tool = (
    Tool.create_with_string_input(
        name="cam_set_nc_comment",
        description=TOOL_DESCRIPTION,
        input_param_name="comment",
        input_param_description="Text for the Comment field.",
    )
    .add_input_property("program", {"type": "string",
            "description": "Omit = all programs."})
    .add_input_property("set_name", {"type": "string",
            "description": "Also set each program's Name field to this."})
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # comment_before / comment_after (and the name pair) are re-read off the parameter after the
    # set, so what the payload states as landed is the read-back, never the request - and a
    # read-back that does not match what was written is an error, not an ok carrying both.
    verification=Verification(
        kind="effect",
        evidence_test="tests/unit/test_cam_set_nc_comment.py::TestStuckParameter"
                      "::test_a_stuck_comment_is_an_error_not_a_reported_success",
        rung="value"))


def register_tool():
    register(item)
