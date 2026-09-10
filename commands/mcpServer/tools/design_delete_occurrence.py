# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Deletes ONE occurrence (component instance) from the active design - the counterpart to
model_create_component. Resolves the target via the shared OccurrenceRef logic (ambiguity-refusing);
names any joints the delete removed; reports timeline health before/after. A pattern/mirror child
can't be deleted on its own. WRITES (destructive).
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _inputs

app = adsk.core.Application.get()


# the shared timeline-health walk (before/after edit guard) - one home in _common
from ._common import timeline_health as _timeline_health


def _joint_names(occ):
    """Names of the joints that affect this occurrence (empty if none/unreadable). Deleting the
    occurrence removes these, so we name them in the result rather than dropping them silently."""
    out = []
    for j in _common.iter_collection(safe(lambda: occ.joints)):
        nm = safe(lambda j=j: j.name)
        if nm:
            out.append(nm)
    return out


def handler(occurrence: str = "") -> dict:
    """Delete one occurrence (component instance) - by handle, fullPathName, or name - naming the
    joints that went with it and the timeline health before/after. WRITES (destructive)."""
    design = _common.design()
    if not design:
        return error("No active design with components.")

    occ, occ_err = _inputs._resolve_occurrence("occurrence", occurrence)
    if not occ:
        return error(occ_err)

    name = safe(lambda: occ.name) or occurrence
    full_path = safe(lambda: occ.fullPathName) or name

    joints = _joint_names(occ)
    was_grounded = bool(safe(lambda: occ.isGrounded, False))

    err_before, _, _ = _timeline_health(design)
    # The assembly-context path census BEFORE the delete is what makes the one after it mean
    # something: a census that cannot see this path to begin with (an unreadable fullPathName, an
    # occurrence walk that will not read) proves nothing by not seeing it afterwards either.
    paths_before = _common.occurrence_paths(design)
    try:
        did = occ.deleteMe()
    except Exception as e:
        return error(f"Could not delete '{name}': {e}")
    if not did:
        # deleteMe reports refusal by RETURNING FALSE rather than raising, and the bool carries no
        # reason - so the error states the return value and points at the read that names an owner.
        return error(
            f"Fusion refused to delete '{name}': deleteMe() returned false, which carries no reason. "
            "Read design_get(include=['timeline']) to see which feature built this instance - an "
            "instance a pattern/mirror feature owns is removed by editing or deleting THAT feature "
            "(design_delete_feature), not through the instance.")

    if full_path in paths_before and full_path in _common.occurrence_paths(design):
        return error(f"deleteMe() reported success but '{full_path}' is still in the assembly's "
                     "occurrence walk - it was NOT deleted. Nothing was rolled back; re-read "
                     "design_get(include=['tree']) to see what is actually there.")
    # True only when the re-read PROVED the path left the assembly; null when the census could not
    # settle it. An unreadable check is never counted as absence.
    gone = True if full_path in paths_before else None

    err_after, warn_after, _ = _timeline_health(design)

    out = {
        "deleted": gone,
        "occurrence": name,
        "full_path": full_path,
        "removed_joints": joints,
        "was_grounded": was_grounded,
        "note": "Occurrence deleted. If it was the last instance of its component, the component was "
        "removed too. Pair with workspace_orient / design_get(include=['tree']) to confirm the assembly.",
    }
    if gone is None:
        out["note"] = (f"deleteMe() reported success for '{name}', but its absence is UNVERIFIED: "
                       f"the occurrence walk did not carry '{full_path}' even before the delete, so "
                       "the re-read cannot prove the instance is gone - and a check that could not "
                       "read is not a check that found nothing. Nothing was rolled back; re-read "
                       "design_get(include=['tree']) to see what is actually there.")
    if joints:
        out["joints_warning"] = (
            f"Deleting '{name}' also removed {len(joints)} joint(s) it participated in "
            f"({', '.join(joints[:6])}) - other parts those joints positioned are now free.")
    if len(err_after) > len(err_before):
        out["timeline_warning"] = (
            f"The timeline carries a new error after this call ({err_after}) that it did not carry "
            "before. Nothing was rolled back; 'deleted' says whether the occurrence's absence was "
            "verified (null = it was not). Undo in Fusion if this was not wanted.")
    elif warn_after:
        out["timeline_warnings"] = warn_after
    return ok(out)


_DESC = (
"Delete one component occurrence; if it was the last instance of its component, the component "
"goes too."
)

tool = (
    Tool.create_simple(name="design_delete_occurrence", description=_DESC)
    .add_input_property("occurrence", {"type": "string",
            "description": "An occurrence 'handle' (design_get tree) or fullPathName/name."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_design_delete_occurrence.py::TestAbsenceReRead"
                      "::test_a_survivor_is_an_error_not_a_false_ok"))


def register_tool():
    register(item)
