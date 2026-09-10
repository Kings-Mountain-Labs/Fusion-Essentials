# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: save the ACTIVE document as a NAMED MILESTONE version.

Document.saveMilestone on a MODIFIED document is a version-creating save - the new tip version IS
the milestone. On a CLEAN document it returns true and creates nothing (measured), so a document
with no unsaved changes is refused here rather than reported as a false success.
"""

from itertools import islice

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import iter_collection, ok, error, safe
from ._data_common import _agent_description
from . import _assert
from . import _doc_common
from . import _outputs

app = adsk.core.Application.get()

# doc_save_milestone PRODUCES the milestoned document's lineage URN.
RETURNS = [
    _outputs.ReturnsUrn("document_id", consumers=["doc_open", "data_get"],
                        absent_when="identity_unreadable"),
]

# Both cloud facts lag the call: a FRESH findFileById reports the new tip within seconds, and the
# MILESTONE mark becomes readable later still. Only the tip is pumped for - holding the main thread
# until the milestone reads freezes Fusion - so the milestone is reported pending for a re-read.
_VERSION_DEADLINE_S = 8.0
_POLL_SLEEP = 0.5
_MILESTONE_WALK_CAP = 200          # this tool's bound on the milestone census, not a platform limit


def _milestone_facts(fresh, name):
    """Milestone state off a FRESH DataFile: (is_milestone, milestone_count, name_present), each None
    only when it could not be read - the mark lags the new version, so a False flag right after the
    save means 'not visible yet'. The name is matched over the ENUMERATION: Milestones.itemByName
    RAISES on a name the collection does not hold. Bounded, so past the cap a miss is None."""
    if fresh is None:
        return None, None, None
    is_milestone = safe(lambda: fresh.isMilestone)
    coll = safe(lambda: fresh.milestones)
    count = safe(lambda: coll.count) if coll is not None else None
    name_present = None
    if count is not None:
        limit = min(count, _MILESTONE_WALK_CAP)
        seen = [safe(lambda m=m: m.name) for m in islice(iter_collection(coll), limit)]
        if name in seen:
            name_present = True
        elif limit >= count:
            name_present = False
    return is_milestone, count, name_present


def _unconfirmed(is_milestone, count, name_present, name):
    """The OBSERVED reason(s) the milestone is not yet confirmed - what was actually read, never a
    guess about which of them is lagging."""
    reasons = []
    if is_milestone is None:
        reasons.append("the new version's isMilestone flag could not be read")
    elif is_milestone is not True:
        reasons.append("the new version's isMilestone flag reads FALSE")
    if count is None:
        reasons.append("the document's Milestones collection could not be read")
    elif name_present is None:
        reasons.append(f"the document's Milestones collection holds {count} entries, more than the "
                       f"{_MILESTONE_WALK_CAP} this call walks, so whether it names '{name}' is "
                       "unknown here")
    elif name_present is not True:
        reasons.append(f"the Milestones collection ({count} entr"
                       f"{'y' if count == 1 else 'ies'} read) holds no entry named '{name}'")
    return reasons


def handler(milestone_name: str = "", description: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    name = (milestone_name or "").strip()
    if not name:
        return error("Provide 'milestone_name'. Fusion accepts an empty name and invents one, but an "
                     "unnamed milestone cannot be found by name in the version history afterwards.")

    doc = safe(lambda: app.activeDocument)
    if not doc:
        return error("No active document to milestone.")
    df = safe(lambda: doc.dataFile)
    if df is None:
        return error("The active document has never been saved to the cloud (no DataFile), and "
                     "saveMilestone cannot create one. Save it first with doc_save_as, then "
                     "milestone the next change.")

    # A CLEAN document is REFUSED: saveMilestone returns true on one while creating no version, no
    # milestone and no description (measured) - reporting that as success would be a false ok.
    if not safe(lambda: doc.isModified, True):
        return error(f"'{safe(lambda: doc.name)}' has no unsaved changes. On an unmodified document "
                     "saveMilestone reports success but creates NO version and NO milestone, so this "
                     "call is refused instead of returning a false success. This tool only creates a "
                     "NEW milestone version - make the change you want milestoned and call again.")

    lineage = safe(lambda: df.id)
    fresh_before = _doc_common.fresh_version_read(app, lineage)
    version_before = fresh_before["version_number"]
    latest_before = fresh_before["latest_version_number"]
    desc = _agent_description(description)

    # The mutation is NOT wrapped in safe - a raised failure must surface, never a false ok.
    try:
        # adsk.core: Document.saveMilestone(milestoneName, versionDescription)
        did = doc.saveMilestone(name, desc)
    except Exception as e:
        return error(f"saveMilestone raised saving '{safe(lambda: doc.name)}' as milestone "
                     f"'{name}': {e}")
    if not did:
        return error(f"saveMilestone returned false for milestone '{name}'; no version and no "
                     "milestone were created.")

    # A save can move the document onto a NEW lineage URN, restarting its versions at 1. Confirming
    # against the pre-save lineage then watches the WRONG version stream, so the check re-anchors on
    # the lineage the document holds now.
    lineage_now = safe(lambda: doc.dataFile.id)
    forked = (isinstance(lineage, str) and isinstance(lineage_now, str)
              and lineage.startswith("urn:") and lineage_now.startswith("urn:")
              and lineage_now != lineage)
    confirm_lineage = (lineage_now if isinstance(lineage_now, str)
                       and lineage_now.startswith("urn:") else None)
    # The wait compares FRESH before/after identities. A fork, unread lineage or missing version
    # baseline returns an unknown verdict after one read instead of comparing unrelated streams.
    advance_verdict, fresh_after = _doc_common.wait_for_version_advance(
        app, confirm_lineage, fresh_before, _VERSION_DEADLINE_S, _POLL_SLEEP)
    fresh = fresh_after["data_file"]
    latest_after = fresh_after["latest_version_number"]
    comparable = advance_verdict is not None
    is_milestone, count_after, name_present = _milestone_facts(fresh, name)
    cloud_tip_advanced = advance_verdict is True
    milestone_confirmed = (is_milestone is True) and (name_present is True)

    result = {
        "save_call_returned_true": True,
        "milestone_name": name,
        "document_name": safe(lambda: doc.name),
        "document_id": confirm_lineage,
        "description": desc,
        "version_before": version_before,
        "version_after": fresh_after["version_number"],
        "version_id_before": fresh_before["version_id"],
        "version_id_after": fresh_after["version_id"],
        "latest_version_before": latest_before,
        "latest_version_after": latest_after,
        # VersionAdvanced reuses this fresh comparison, avoiding a second bounded cloud wait.
        "cloud_tip_advanced": cloud_tip_advanced,
        "milestone_confirmed": milestone_confirmed,
        "milestone_count_after": count_after,
    }
    if confirm_lineage is None:
        result["identity_unreadable"] = True
    if forked:
        result["lineage_changed"] = {"from": lineage, "to": lineage_now}
    if not cloud_tip_advanced:
        result["pending"] = True
        if not comparable:
            if confirm_lineage is None:
                why = "the document's lineage could not be read AFTER the save"
            elif forked:
                why = "the save moved the document onto a new lineage"
            else:
                why = "fresh before/after lineage/version reads were not comparable"
            result["note"] = (f"saveMilestone returned true, but {why}. Cloud version advancement "
                              "is unknown. Read doc_get include=['versions'] before retrying.")
        else:
            result["note"] = ("Cloud version advancement was not observed within "
                              f"{_VERSION_DEADLINE_S:.0f}s of re-fetching; confirmation remains "
                              "pending. Read doc_get include=['versions'] before retrying.")
    elif milestone_confirmed:
        result["note"] = (f"Fresh cloud reads confirmed version advancement and milestone '{name}'. "
                          "Read doc_get include=['versions'] for the version history.")
    else:
        result["pending"] = True
        result["note"] = ("Fresh cloud reads confirmed version advancement, but " + " and ".join(
                              _unconfirmed(is_milestone, count_after, name_present, name))
                          + ". Milestone confirmation remains pending; read "
                          "doc_get include=['versions'] before retrying.")
    if forked:
        result["note"] += (" The save changed lineage; use lineage_changed.to for subsequent "
                           "cloud reads.")
    return ok(result)


TOOL_DESCRIPTION = (
    "Save the ACTIVE document as a NAMED MILESTONE - a new cloud version marked, findable by name.\n"
    + _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="doc_save_milestone", description=TOOL_DESCRIPTION)
    .add_input_property("milestone_name", {"type": "string"})
    .add_input_property("description", {"type": "string",
            "description": "The AI-agent marker is prepended."})
    .add_required_input("milestone_name")
    .strict_schema()
)

item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_assert.VersionAdvanced()])


def register_tool():
    register(item)
