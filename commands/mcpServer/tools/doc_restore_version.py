# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: roll the active cloud document back to a prior version.

DataFile.promote() makes a chosen historical version the latest - it does not overwrite history but
creates a NEW tip version whose content matches the restored one.
"""

import time

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import iter_collection, ok, error, safe
from . import _doc_common
from . import _export

app = adsk.core.Application.get()

# The new tip lands on the cloud AFTER promote() returns, so the confirming read is pumped to a
# deadline instead of sampled once. The pump only advances while the main thread runs, which is what
# _export.pump_until does; the tip-advanced signal below is this tool's own.
_VERSION_DEADLINE_S = 8.0
_POLL_SLEEP = 0.5

# The cloud's version metadata trails a completed save, so a tip that EQUALS the version asked for
# is re-fetched before the call reports there is nothing to restore - but only while a lag is still
# possible: past the window a tip has stood too long to be a stale reading.
_TIP_RECHECK_S = float(_doc_common.VERSION_LAG_WINDOW_S)


def _version_age_seconds(version):
    """Seconds since `version` was created, or None when its date did not read."""
    created = safe(lambda: version.dateCreated)
    if not isinstance(created, (int, float)) or isinstance(created, bool):
        return None
    return max(0, int(time.time() - created))


def _wanted_number(version_number):
    """The version number the caller named as an int, or None when it is not one."""
    try:
        return int(version_number)
    except (TypeError, ValueError):
        return None


def _fresh_tip(lineage, latest_seen):
    """Re-fetch the lineage until its tip passes latest_seen, bounded by _TIP_RECHECK_S:
    (fresh_DataFile_or_None, latest_or_None), the LAST reading either way. With no lineage id there
    is nothing to re-fetch BY, so the wait is not spent - it would buy no reading at all."""
    if not lineage:
        return None, None

    def probe():
        fresh = safe(lambda: app.data.findFileById(lineage))
        latest = safe(lambda: fresh.latestVersionNumber) if fresh is not None else None
        return (latest is not None and latest > latest_seen), (fresh, latest)

    return _export.pump_until(probe, _TIP_RECHECK_S, _POLL_SLEEP)[1]


def _find_version(df, version_number, version_id):
    """Locate the target version among the DataFile itself + df.versions.
    Returns (target_DataFile_or_None, available_version_numbers_desc)."""
    available = []
    candidates = [df]
    candidates.extend(iter_collection(safe(lambda: df.versions)))
    found = None
    for v in candidates:
        if v is None:
            continue
        n = safe(lambda v=v: v.versionNumber)
        if n is not None:
            available.append(n)
        if found is None:
            if version_number is not None and n == int(version_number):
                found = v
            elif version_id and safe(lambda v=v: v.versionId) == version_id:
                found = v
    return found, sorted(set(available), reverse=True)


def _confirm_new_tip(lineage, df, latest_before):
    """Re-read the tip version until it passes latest_before, bounded by _VERSION_DEADLINE_S, and
    return the LAST reading either way. The DataFile is re-fetched FRESH each attempt - the handle
    promote() was issued on can keep its pre-call latestVersionNumber - and latest_before=None
    settles on the first reading, there being no baseline to settle against."""
    def probe():
        if lineage:
            fresh = safe(lambda: app.data.findFileById(lineage))
            # A re-fetch that answers nothing leaves the tip UNKNOWN; the held handle's number is
            # the PRE-call one, so reporting it as the after-reading would invent a reading.
            latest_after = safe(lambda: fresh.latestVersionNumber) if fresh is not None else None
        else:
            latest_after = safe(lambda: df.latestVersionNumber)
        advanced = (latest_before is None
                    or (latest_after is not None and latest_after > latest_before))
        return advanced, latest_after

    _advanced, latest_after = _export.pump_until(probe, _VERSION_DEADLINE_S, _POLL_SLEEP)
    return latest_after


def handler(version_number=None, version_id: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    doc = safe(lambda: app.activeDocument)
    if not doc:
        return error("No active document to restore a version of.")
    df = safe(lambda: doc.dataFile)
    if not df:
        return error("The active document has no cloud DataFile (never saved to the cloud); there is "
                     "no version history to restore. Save it first (doc_save_as).")

    vid = (version_id or "").strip()
    if version_number is None and not vid:
        return error("Specify which version to restore: pass version_number (an integer) or version_id.")

    lineage = safe(lambda: df.id)
    latest_before = safe(lambda: df.latestVersionNumber)
    wanted = f"number {version_number}" if version_number is not None else f"id '{vid}'"
    refetched = False        # the window is paid at most ONCE per call, whichever branch spends it
    target, available = _find_version(df, version_number, vid)
    if target is None:
        # A version numbered ABOVE the tip this read shows is the same lagging read, one step
        # earlier: the history it was searched against is the stale one.
        asked = _wanted_number(version_number)
        if asked is not None and latest_before is not None and asked > latest_before:
            fresh, latest_now = _fresh_tip(lineage, latest_before)
            refetched = bool(lineage)
            if latest_now is not None and latest_now > latest_before:
                target, available = _find_version(fresh, version_number, vid)
                latest_before = latest_now
        if target is None:
            return error(f"No version matching {wanted} in this document's history. "
                         f"Available version numbers (newest-first): {available}.")

    tnum = safe(lambda: target.versionNumber)
    if latest_before is not None and tnum == latest_before:
        # The version asked for sits ON the tip this read shows - which a lagging read also looks
        # like. The re-fetch is only worth its wait while a lag is still possible: past the window a
        # tip is a settled reading, and a tip re-read once already for this call is current.
        age = _version_age_seconds(target)
        settled = age is not None and age >= _doc_common.VERSION_LAG_WINDOW_S
        recheck = bool(lineage) and not settled and not refetched
        fresh, latest_now = _fresh_tip(lineage, latest_before) if recheck else (None, None)
        if latest_now is None or latest_now <= latest_before:
            if refetched:
                why = "the tip was already re-fetched once for this call"
            elif not lineage:
                why = "this document carries no lineage id to re-fetch the tip by"
            elif settled:
                why = (f"it has stood for {age}s, past the "
                       f"{_doc_common.VERSION_LAG_WINDOW_S}s a version read can trail the cloud")
            else:
                why = (f"the cloud tip was re-fetched for {_TIP_RECHECK_S:.0f}s without advancing "
                       f"past {latest_before}")
            return ok({"restored": False, "restored_version": tnum,
                       "latest_version_number": latest_before,
                       "tip_age_seconds": age,
                       "tip_rechecked": recheck,
                       "note": f"Version {tnum} is already the latest version; nothing to restore - "
                               f"{why}."})
        target, available = _find_version(fresh, version_number, vid)
        if target is None:
            return error(f"The cloud tip advanced to {latest_now} while this call re-read it, and "
                         f"{wanted} is not in the refreshed history. Available version numbers "
                         f"(newest-first): {available}.")
        latest_before = latest_now
        tnum = safe(lambda: target.versionNumber)

    # The mutation: promote is NOT wrapped in safe - a raised failure must surface, never a false ok.
    try:
        did = target.promote()
    except Exception as e: # noqa: BLE001 - report the API failure honestly instead of swallowing it
        return error(f"promote() raised while restoring version {tnum}: {e}")
    if not did:
        return error(f"promote() returned false restoring version {tnum}; the restore did not take effect.")

    # Verify-after-write: pump the confirming read until the lineage carries a NEW tip.
    latest_after = _confirm_new_tip(lineage, df, latest_before)
    confirmed = (latest_before is not None and latest_after is not None and latest_after > latest_before)
    result = {
        # The raw API answer and the VERIFIED effect are separate keys: promote() returning true is
        # not proof the lineage got a new tip, so 'restored' says only that the tip advanced.
        "promote_call_returned_true": True,
        "restored": confirmed,
        "restored_version": tnum,
        "latest_before": latest_before,
        "latest_after": latest_after,
    }
    if confirmed:
        result["note"] = (f"Version {tnum} promoted to latest; a new tip version {latest_after} now carries "
                          "its content (history is preserved). Reopen/reload the document to see it in-session.")
    else:
        result["pending"] = True
        seen = latest_after if latest_after is not None else "unreadable"
        if latest_before is None:
            # With no pre-call number the wait had nothing to settle against and returned on its
            # first read: no duration was spent and non-advancement was never observed, so the
            # payload reports the missing baseline instead of a verdict it cannot support.
            result["note"] = (f"promote() returned true, but this document's latestVersionNumber could not "
                              f"be read BEFORE the call - so whether a new tip appeared is not decidable "
                              f"here (the read after the call reports {seen}). Confirm with "
                              "doc_get include=['versions'].")
        else:
            result["note"] = (f"promote() returned true but the latest version has NOT advanced after "
                              f"{_VERSION_DEADLINE_S:.0f}s of re-reading the cloud file (latest reads "
                              f"{seen}, was {latest_before}) - no new tip carrying version {tnum}'s content "
                              "was observed. Confirm with doc_get include=['versions'] before promoting "
                              "again.")
    return ok(result)


TOOL_DESCRIPTION = (
    "Promote a prior version of the ACTIVE cloud document to latest - a NEW tip version carries its "
    "content. Versions come from doc_get include=['versions']."
)

tool = (
    Tool.create_simple(name="doc_restore_version", description=TOOL_DESCRIPTION)
    .add_input_property("version_number", {"type": "integer"})
    .add_input_property("version_id", {"type": "string",
            "description": "The version's id (versionId)."})
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # 'restored' is the lineage's tip re-read until it passes the number held before promote() -
    # a version number that advanced, never the promote call's own bool.
    verification=Verification(
        kind="effect",
        evidence_test="tests/unit/test_doc_restore_version.py::TestRestoreHonesty"
                      "::test_the_pump_gives_up_at_the_bound_with_the_reading_it_last_got",
        rung="value"))


def register_tool():
    register(item)
