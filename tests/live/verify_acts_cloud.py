# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""ACT rows: the OPT-IN CLOUD TIER - the data model, the saved document, and the drawing.

The three acts that touch an operator's own Autodesk hub, and so run only where cloud_config names
one (verify_core._cloud_tier_probe). Each act creates run-stamped artifacts under the configured
folder and takes every one back out by name, with the delete read back rather than assumed, and
each act ends by activating the document the session was on when the tier started, so a chunk
boundary can fall between them and a partial run leaves the session where it found it.
"""

import time

from cloud_config import FOLDER, HUB, PROJECT
from verify_core import (
    EXPORT_DIR, MARKER_PNG, _RECALL, _activated, _ctx_get, _document_closed, _driven_slide, _dwell,
    _extruded, _face_up_at, _fg, _home_address, _home_document, _jointed, _made_component,
    _measured, _motion_linked, _new_document, _num, _recall, _refused, _watch, facade)

_STAMP = time.strftime("%Y%m%d-%H%M%S")

# THE ARTIFACTS, all run-stamped so two overlapping runs never collide on one name and a run that
# died leaves artifacts a later one can tell from its own.
RUN_FOLDER = "SweepRun " + _STAMP            # under the configured folder
MOVED_FOLDER = "Moved"                       # under RUN_FOLDER - where the move beat lands the file
SOURCE_DOC = "SweepCloudSource " + _STAMP    # the saved design the drawing is generated from
COPY_DOC = "SweepCloudCopy " + _STAMP        # doc_copy's destination
HOST_DOC = "SweepCloudHost " + _STAMP        # the scratch host the source is inserted into
DERIVE_DOC = "SweepCloudDerive " + _STAMP    # the derive host whose stale link is read after a reopen
LINK_DOC = "SweepCloudLink " + _STAMP        # the xref assembly the both-members drive guard is read in

RUN_PATH = f"{FOLDER}/{RUN_FOLDER}"
MOVED_PATH = f"{RUN_PATH}/{MOVED_FOLDER}"

# The component the source document is built from, in ITS OWN document - see _lit.
SRC_COMP = "CloudSrc"
SRC_SKETCH = "CloudSrcPlate"

# Where the three xref instances of the plate stand in the link document, spaced down X so ONE
# design-wide find_geometry resolves each one's faces by position: every instance answers to the
# same stamped document name, so a name cannot tell them apart. The plate spans 80 x 50 x 10 mm
# from its own origin, and the face centres below are read off that.
_LINK_X = (0.0, 200.0, 400.0)
_PLATE_MID_X, _PLATE_MID_Y, _PLATE_TOP_Z = 40.0, 25.0, 10.0

# The version the restore beat promotes. It must be one the tip has MOVED PAST: MEASURED, promoting
# the latest takes the tool's early return - "already the latest version; nothing to restore", with
# no promote call and no tip to compare - and the cloud's version stream lags the saves that made it,
# so the only number certain to be behind the tip is the first. The parameter a later beat edits
# survives it: promoting a version does not reload the session, and the save after this one writes
# the open document's own state as the new tip.
RESTORE_VERSION = 1

DOWNLOAD_DIR = EXPORT_DIR + "/cloud"


def _lit(args):
    """`args` as a CALLABLE ignoring ctx - a literal dict the layout pass cannot read.

    The packer deals a cell to every chunk whose steps pin a world coordinate, reading a step's dict
    arguments to find them. These acts build in a document of their own, where an authored x is not
    a place in the story field, so their coordinates are handed over as callables and no cell is
    dealt for a component that never appears in that field."""
    return lambda _ctx, _a=args: dict(_a)


def _upload_args(ctx):
    """Start a fresh upload attempt and clear lineage captured by an earlier attempt."""
    for key in ("upload_handle", "cloud_file", "cloud_file_name"):
        ctx.pop(key, None)
    return {"file_path": MARKER_PNG, "project": PROJECT, "folder": RUN_PATH}


def _upload_status_args(ctx):
    """Read the current attempt's exact minted upload handle and invalidate stale lineage."""
    for key in ("cloud_file", "cloud_file_name"):
        ctx.pop(key, None)
    return {"handle": _ctx_get(ctx, "upload_handle", "the upload")}


def _dependent_folder_args(ctx, folder_key, name):
    """Build cleanup args only after the current upload has produced its file lineage."""
    _ctx_get(ctx, "cloud_file", "the uploaded file")
    return {"folder_id": _ctx_get(ctx, folder_key, "the folder"), "confirm_name": name}


# --- predicates: each reads keys the tool PUBLISHES, and reports what it read ------------------

def _hub_is(hub, project):
    """data_get (no scope): the hub this session is signed in to, and the configured project listed
    in it. The capability probe reads the same two values before the act runs; this row is what puts
    them on the ledger, so the receipt says which hub the artifacts below were made in."""
    def check(p):
        names = [str(r.get("name")) for r in (p.get("projects") or [])]
        return _measured(f"active hub '{hub}' lists project '{project}'",
                         {"active_hub": p.get("active_hub"),
                          "project_count": p.get("project_count"),
                          "project_listed": project in names,
                          "time_truncated": p.get("time_truncated")},
                         p.get("active_hub") == hub and project in names
                         and p.get("project_count") == len(names))
    return check


def _folder_created(name, parent):
    """data_create_folder: the folder re-listed under the parent asked for, and NOTHING auto-created
    on the way there. The configured folder is the operator's; a run that had to invent it is
    addressing a project this config does not describe."""
    def check(p):
        return _measured(f"'{name}' created under '{parent}', no parent invented",
                         {"created": p.get("created"), "name": p.get("name"), "id": p.get("id"),
                          "path": p.get("path"),
                          "auto_created_parents": p.get("auto_created_parents")},
                         p.get("created") is True and p.get("name") == name
                         and bool(p.get("id")) and p.get("path") == f"{parent}/{name}"
                         and p.get("auto_created_parents") == [])
    return check


def _upload_started(folder):
    """data_upload_file: the poll handle it minted and the destination it resolved. The tool claims
    no completion here - data_get_upload_status is what says the file landed."""
    def check(p):
        return _measured(f"upload started into '{folder}'",
                         {"upload_started": p.get("upload_started"),
                          "upload_handle": p.get("upload_handle"),
                          "destination_folder": p.get("destination_folder"),
                          "upload_state": p.get("upload_state")},
                         p.get("upload_started") is True and bool(p.get("upload_handle"))
                         and p.get("destination_folder") == folder)
    return check


def _upload_complete(p):
    """data_get_upload_status: the cloud reports transfer AND processing finished, and hands back the
    lineage URN every later step addresses the file by. A bare 'ok' here would let the move, the
    download and the delete run at a file that is not there yet."""
    return _measured("upload state 'complete' with a lineage URN",
                     {"state": p.get("state"), "handle": p.get("handle"),
                      "file_id": p.get("file_id"), "version_number": p.get("version_number"),
                      "elapsed_seconds": p.get("elapsed_seconds")},
                     p.get("state") == "complete"
                     and isinstance(p.get("file_id"), str)
                     and p["file_id"].startswith("urn:") and len(p["file_id"]) > 4)


def _file_record(folder_path, complete=True):
    """data_get(file=<urn>): the file's own record - where it sits, and whether the cloud has
    finished with it. 'is_complete' is what the drawing generator needs true of its source."""
    def check(p):
        f, loc, state = p.get("file") or {}, p.get("location") or {}, p.get("state") or {}
        return _measured(f"the file's record reads folder '{folder_path}', is_complete {complete}",
                         {"name": f.get("name"), "id": f.get("id"),
                          "parent_folder": (loc.get("parent_folder") or {}).get("path"),
                          "state": state, "matched_by": p.get("matched_by")},
                         bool(f.get("name")) and str(f.get("id") or "").startswith("urn:")
                         and (loc.get("parent_folder") or {}).get("path") == folder_path
                         and state.get("is_complete") is complete)
    return check


# The cloud finishes with a saved file on its own clock (its record reads is_complete False for
# tens of seconds after the save answers), and both the drawing generator and a delete need it
# finished. Bounded: the budget running out is reported as the state it last read, never as complete.
_SETTLE_POLLS = 12
_SETTLE_GAP_S = 5.0


def _file_settled(folder_path, polls=_SETTLE_POLLS):
    """data_get(file=<urn>) re-read until the record reads is_complete True, then judged by
    _file_record; the LAST read is what a failure prints."""
    record = _file_record(folder_path)

    def check(p):
        call = facade("call")        # resolved when the row runs, after tool_verify has loaded
        for i in range(polls):
            if (p.get("state") or {}).get("is_complete") is True or i == polls - 1:
                break
            time.sleep(_SETTLE_GAP_S)
            is_error, again = call("data_get", {"file": (p.get("file") or {}).get("id") or ""})
            if not is_error and isinstance(again, dict):
                p = again
        return record(p)
    return check


def _moved_to(folder_path):
    """data_move_file: DataFile.move returns a bool, so the tool re-resolves the file and re-reads
    its parent - 'to_folder' is that re-read, and 'verified_by' names what it matched on."""
    def check(p):
        return _measured(f"the file re-reads its parent as '{folder_path}'",
                         {"moved": p.get("moved"), "verified_by": p.get("verified_by"),
                          "from_folder": p.get("from_folder"), "to_folder": p.get("to_folder"),
                          "name": p.get("name")},
                         p.get("moved") is True and p.get("to_folder") == folder_path
                         and bool(p.get("verified_by")))
    return check


def _downloaded(p):
    """data_download_file: the bytes on disk. The API's own bool is not the evidence - the FileLanded
    postcondition stats the path and publishes size_bytes, and that is what arriving means."""
    return _measured("the downloaded file has bytes on disk",
                     {"downloaded": p.get("downloaded"), "file_path": p.get("file_path"),
                      "size_bytes": p.get("size_bytes"), "name": p.get("name")},
                     p.get("downloaded") is True and _num(p.get("size_bytes"))
                     and p["size_bytes"] > 0)


def _file_deleted(p):
    """data_delete_file: deleteMe() returned true (the tool errors on false), the file's own name and
    URN come back, and 'forced' false says no reference check was bypassed to do it."""
    return _measured("the cloud file was deleted",
                     {"deleted": p.get("deleted"), "name": p.get("name"),
                      "document_id": p.get("document_id"),
                      "was_referenced_by": p.get("was_referenced_by"),
                      "forced": p.get("forced")},
                     p.get("deleted") is True and bool(p.get("name"))
                     and str(p.get("document_id") or "").startswith("urn:")
                     and p.get("forced") is False)


def _folder_deleted(name):
    """data_delete_folder on an EMPTIED folder: the census read before the delete says the subtree
    was empty, so 'recursive' false is the delete taking nothing else with it."""
    def check(p):
        return _measured(f"'{name}' deleted with nothing in it",
                         {"deleted": p.get("deleted"), "name": p.get("name"),
                          "contained_files": p.get("contained_files"),
                          "contained_subfolders": p.get("contained_subfolders"),
                          "recursive": p.get("recursive")},
                         p.get("deleted") is True and p.get("name") == name
                         and p.get("recursive") is False
                         and p.get("contained_files") == 0
                         and p.get("contained_subfolders") == 0)
    return check


def _files_gone(*names):
    """data_get(project, folder): none of the run's documents is left in the configured folder - the
    read-back standing apart from what the delete calls reported about themselves.

    This listing has THREE ways of not looking, not two: the size and time caps set 'truncated', and
    a folder whose enumeration RAISED is counted in 'folders_unreadable' (named in
    folders_unreadable_at) while 'truncated' stays false. A file of these names could be sitting in
    exactly that folder, so an unreadable one is a hole in the search space and not an absence."""
    def check(p):
        seen = [str(r.get("name")) for r in (p.get("files") or [])]
        left = [n for n in names if n in seen]
        return _measured(f"the configured folder no longer holds {list(names)}, and was fully read",
                         {"file_count": p.get("file_count"), "still_there": left,
                          "truncated": p.get("truncated"),
                          "time_truncated": p.get("time_truncated"),
                          "folders_unreadable": p.get("folders_unreadable"),
                          "folders_unreadable_at": p.get("folders_unreadable_at")},
                         not left and not p.get("truncated") and not p.get("time_truncated")
                         and not p.get("folders_unreadable"))
    return check


def _saved_as(name, folder):
    """doc_save_as: the document written into the configured folder under this name, with no name
    collision - a second file of one name is a fork this run could not then clean up by name."""
    def check(p):
        return _measured(f"'{name}' saved into '{folder}'",
                         {"saved": p.get("saved"), "name": p.get("name"),
                          "destination_folder": p.get("destination_folder"),
                          "document_id": p.get("document_id"),
                          "auto_created_parents": p.get("auto_created_parents"),
                          "name_collision": p.get("name_collision")},
                         p.get("saved") is True and p.get("name") == name
                         and p.get("destination_folder") == folder
                         and p.get("auto_created_parents") == []
                         and "name_collision" not in p)
    return check


def _document_is(name, saved=True):
    """doc_get: which document the session is actually on, and - for a saved one - the lineage URN
    it is addressed by from here on.

    The writes below act on the ACTIVE document, so this is the row that says which one that is. The
    URN is asserted rather than merely saved because doc_get reads it through a guarded getter: a
    read that raised publishes null, this row would still pass on the NAME, and every step after it
    would address None - the opens, the closes and the deletes alike, which is the teardown leaving
    real documents behind in the operator's hub."""
    def check(p):
        active = p.get("active") or {}
        urn = str(active.get("document_id") or "")
        return _measured(f"the active document is '{name}' (saved {saved})",
                         {"name": active.get("name"), "has_data_file": active.get("has_data_file"),
                          "document_id": active.get("document_id")},
                         active.get("name") == name and active.get("has_data_file") is saved
                         and (urn.startswith("urn:") if saved else True))
    return check


def _versioned(name):
    """doc_save: a new cloud version of the same file. 'saved' is what save() returned - the tool
    errors on false - and 'already_current' absent is the document having had something to version."""
    def check(p):
        return _measured(f"'{name}' saved as a new version",
                         {"saved": p.get("saved"), "document_name": p.get("document_name"),
                          "already_current": p.get("already_current"),
                          "description": p.get("description")},
                         p.get("saved") is True and p.get("document_name") == name
                         and not p.get("already_current"))
    return check


def _milestoned(milestone, name):
    """doc_save_milestone: the two INDEPENDENT read-backs the tool keeps apart - 'cloud_tip_advanced'
    is a fresh fetch showing a NEW version number, 'milestone_confirmed' the mark on it. The mark
    lags the version by seconds, so it rides in the evidence unasserted; the version is the claim."""
    def check(p):
        return _measured(f"'{milestone}' versioned '{name}' (the mark may still be pending)",
                         {"save_call_returned_true": p.get("save_call_returned_true"),
                          "milestone_name": p.get("milestone_name"),
                          "document_name": p.get("document_name"),
                          "latest_version_before": p.get("latest_version_before"),
                          "latest_version_after": p.get("latest_version_after"),
                          "cloud_tip_advanced": p.get("cloud_tip_advanced"),
                          "milestone_confirmed": p.get("milestone_confirmed"),
                          "pending": p.get("pending")},
                         p.get("milestone_name") == milestone
                         and p.get("document_name") == name
                         and p.get("cloud_tip_advanced") is True
                         and "lineage_changed" not in p)
    return check


def _versions_read(least):
    """doc_get(include=['versions']): the lineage this act built, read off the cloud - available,
    enumerated, and holding at least `least` version ROWS, with version_count len() over the rows
    actually read so the two cannot disagree.

    What settles and what lags are DIFFERENT KEYS, measured apart. The rows arrive together with
    their count; the TIP NUMBER trails them - read latest_version_number 1 beside version_count 2
    and two rows read, on a run whose doc_save_milestone had already reported cloud_tip_advanced
    true. So the rows are what this asserts, and the tip and the milestone count are published as
    evidence and asserted nowhere: the milestone's own row proves the version it made, and repeating
    the claim here would only be asserting how fast the metadata caught up."""
    def check(p):
        v = p.get("versions") or {}
        rows = v.get("versions") or []
        return _measured(f"at least {least} version rows, the history readable",
                         {"available": v.get("available"),
                          "latest_version_number": v.get("latest_version_number"),
                          "version_count": v.get("version_count"),
                          "history_readable": v.get("history_readable"),
                          "milestone_count": v.get("milestone_count"),
                          "rows_read": len(rows)},
                         v.get("available") is True and v.get("history_readable") is True
                         and _num(v.get("version_count")) and v["version_count"] >= least
                         and len(rows) >= least
                         and v.get("version_count") == len(rows))
    return check


def _restored(version):
    """doc_restore_version: promote() returned true and the tool re-read the tip. 'restored' is that
    comparison - a NEW tip carrying the promoted content - and 'pending' is the tip not having moved
    within the wait, which is reported rather than called a failure."""
    def check(p):
        return _measured(f"version {version} promoted",
                         {"promote_call_returned_true": p.get("promote_call_returned_true"),
                          "restored": p.get("restored"),
                          "restored_version": p.get("restored_version"),
                          "latest_before": p.get("latest_before"),
                          "latest_after": p.get("latest_after"), "pending": p.get("pending")},
                         p.get("promote_call_returned_true") is True
                         and p.get("restored_version") == version
                         and _num(p.get("latest_before")))
    return check


def _copied(source, name, folder):
    """doc_copy: the copy read back off the created DataFile - its own name and lineage URN, which is
    a DIFFERENT lineage from the source it was taken from."""
    def check(p):
        return _measured(f"'{source}' copied to '{name}' in '{folder}'",
                         {"copied": p.get("copied"), "source_document": p.get("source_document"),
                          "copied_name": p.get("copied_name"), "copied_id": p.get("copied_id"),
                          "source_id": p.get("source_id"),
                          "destination_folder": p.get("destination_folder"),
                          "rename_warning": p.get("rename_warning")},
                         p.get("copied") is True and p.get("source_document") == source
                         and p.get("copied_name") == name
                         and p.get("destination_folder") == folder
                         and str(p.get("copied_id") or "").startswith("urn:")
                         and p.get("copied_id") != p.get("source_id"))
    return check


def _inserted(name):
    """doc_insert_occurrence: the occurrence the host gained, and 'is_reference' - the flag saying it
    is an XREF onto the source's saved version rather than a copy of its geometry, which is what
    doc_update_xref then has something to walk."""
    def check(p):
        return _measured(f"'{name}' inserted as a reference",
                         {"inserted": p.get("inserted"), "document_name": p.get("document_name"),
                          "new_occurrence_name": p.get("new_occurrence_name"),
                          "is_reference": p.get("is_reference"),
                          "into_component": p.get("into_component")},
                         p.get("inserted") is True and p.get("document_name") == name
                         and bool(p.get("new_occurrence_name"))
                         and p.get("is_reference") is True)
    return check


def _xrefs(total, updated=None, skipped=None):
    """doc_update_xref(only_out_of_date=True): the host's reference census, with updated and skipped
    PARTITIONING it.

    The flag is what makes the partition measurable: under only_out_of_date=false nothing can land in
    'skipped' - every reference is refreshed whether or not it needed one - so a claim about the two
    buckets read off that call would be a claim about a branch the call cannot take.

    'updated'/'skipped' left None assert the partition and report which bucket the row landed in.
    That is the honest reading for the walk taken straight after an insert: MEASURED, a
    just-inserted occurrence xref came back was_out_of_date true, bound at version 2 while the
    source's stream had reached 3 - so which bucket it falls in depends on how far the cloud's
    version metadata has caught up, and is not this act's to fix in place."""
    def check(p):
        up, sk = p.get("updated"), p.get("skipped")
        want = (f"{updated} updated, {skipped} skipped" if updated is not None
                else "either bucket")
        return _measured(f"{total} reference(s) walked: {want}",
                         {"total_references": p.get("total_references"),
                          "updated_count": p.get("updated_count"),
                          "updated": up, "skipped": sk},
                         isinstance(up, list) and isinstance(sk, list)
                         and p.get("updated_count") == len(up)
                         and p.get("total_references") == total
                         and len(up) + len(sk) == total
                         and (updated is None or len(up) == updated)
                         and (skipped is None or len(sk) == skipped))
    return check


def _opened(name):
    """doc_open: the document Fusion opened, named, with the URN it resolved. The switch is ASYNC, so
    'is_active' rides in the evidence unasserted - the doc_get after it says the session is on it."""
    def check(p):
        return _measured(f"'{name}' opened",
                         {"opened": p.get("opened"), "document_name": p.get("document_name"),
                          "is_active": p.get("is_active"), "open_method": p.get("open_method"),
                          "resolved_id": p.get("resolved_id")},
                         p.get("opened") is True and p.get("document_name") == name
                         and str(p.get("resolved_id") or "").startswith("urn:"))
    return check


def _drawing_created(p):
    """drawing_create: the CLOUD drawing file the generator wrote - its own name off the DataFile and
    a lineage URN, which is the identity it is addressed by (Fusion auto-names every drawing from its
    source design, so two drawings of one design share a name)."""
    return _measured("a drawing file was created",
                     {"created": p.get("created"), "drawing_name": p.get("drawing_name"),
                      "file_id": p.get("file_id"), "file_extension": p.get("file_extension")},
                     p.get("created") is True and bool(p.get("drawing_name"))
                     and str(p.get("file_id") or "").startswith("urn:"))


def _sheets_answer(p):
    """drawing_get: the sheet read every write and the export below are taken behind. Its own shape
    claims are what make it a read rather than a pause - export_index 1-based and contiguous (the
    numbering drawing_export's sheet_range takes, obtainable nowhere else), one sheet active."""
    sheets = p.get("sheets") or []
    return _measured("the drawing answers with its sheets",
                     {"sheet_count": p.get("sheet_count"),
                      "export_index": [s.get("export_index") for s in sheets],
                      "active": [s.get("name") for s in sheets if s.get("is_active")]},
                     _num(p.get("sheet_count")) and p["sheet_count"] >= 1
                     and [s.get("export_index") for s in sheets] == list(range(1, len(sheets) + 1))
                     and sum(1 for s in sheets if s.get("is_active")) == 1)


def _sheet_added(name):
    """drawing_edit_sheet(add): the new sheet named, and the count read either side of the add."""
    def check(p):
        return _measured(f"sheet '{name}' added",
                         {"added": p.get("added"), "sheet": p.get("sheet"),
                          "sheet_count": p.get("sheet_count"),
                          "sheet_count_before": p.get("sheet_count_before")},
                         p.get("sheet") == name and _num(p.get("sheet_count_before"))
                         and p.get("sheet_count") == p["sheet_count_before"] + 1)
    return check


def _exported(p):
    """drawing_export: the file's bytes on disk, the standard design_export is held to."""
    return _measured("the exported drawing has bytes on disk",
                     {"file_path": p.get("file_path"), "size_bytes": p.get("size_bytes"),
                      "format": p.get("format")},
                     _num(p.get("size_bytes")) and p["size_bytes"] > 0)


def _drawing_current(p):
    """drawing_update on a drawing generated moments ago: nothing to refresh, and it says so rather
    than reporting a refresh it did not do."""
    return _measured("the fresh drawing is already up to date",
                     {"updated": p.get("updated"), "is_up_to_date": p.get("is_up_to_date")},
                     p.get("updated") is False and p.get("is_up_to_date") is True)


def _drawing_refreshed(p):
    """drawing_update after the source design was edited and saved: the refresh that ran, and how
    many references were stale before it - the number separating a real refresh from a no-op."""
    return _measured("the stale drawing was refreshed",
                     {"updated": p.get("updated"),
                      "stale_references_before": p.get("stale_references_before"),
                      "is_up_to_date": p.get("is_up_to_date")},
                     p.get("updated") is True and _num(p.get("stale_references_before"))
                     and p["stale_references_before"] > 0)


def _dimensioned(p):
    """drawing_dimension: the strategy ran and the drawing document reports itself modified."""
    return _measured("the view was dimensioned",
                     {"dimensioned": p.get("dimensioned"),
                      "document_modified": p.get("document_modified"),
                      "strategy": p.get("strategy")},
                     p.get("dimensioned") is True and p.get("document_modified") is True)


def _image_placed(p):
    """drawing_insert_image: 'position_bounds_checked' says the anchor was compared against the sheet
    BEFORE anything was placed - an off-sheet insert returns success and renders nothing, and an
    image cannot be read back or moved afterwards, so the check having run is the read-back."""
    return _measured("the image was placed inside the sheet",
                     {"inserted": p.get("inserted"), "scale": p.get("scale"),
                      "position_bounds_checked": p.get("position_bounds_checked")},
                     p.get("inserted") is True and p.get("position_bounds_checked") is True)


def _sketch_landed(name, count):
    """drawing_add_sketch: the curves the sheet sketch actually gained, counted off its own
    collections, on the sketch and sheet the payload names back."""
    def check(p):
        return _measured(f"'{name}' landed {count} curve(s)",
                         {"curves_landed": p.get("curves_landed"),
                          "curves_requested": p.get("curves_requested"),
                          "sketch_name": p.get("sketch_name"), "sheet_name": p.get("sheet_name")},
                         p.get("curves_landed") == count and p.get("sketch_name") == name
                         and bool(p.get("sheet_name")))
    return check


def _derived(source):
    """doc_insert_derive: the one-way linked copy this document gained, and the SOURCE VERSION the
    link is bound to - the number a later save of the source moves past, which is what leaves the
    reference stale for the refusal below to be read on."""
    def check(p):
        return _measured(f"'{source}' derived, bound to its saved version",
                         {"derived": p.get("derived"), "feature_name": p.get("feature_name"),
                          "document_name": p.get("document_name"),
                          "source_version": p.get("source_version"),
                          "derived_occurrence": p.get("derived_occurrence"),
                          "bodies_landed": p.get("bodies_landed")},
                         p.get("derived") is True and p.get("document_name") == source
                         and bool(p.get("feature_name")) and _num(p.get("source_version")))
    return check


# How long the source's new version is given to become visible to the CLOUD before the tip read
# below asserts it. doc_save returns as soon as Fusion has written; the version METADATA the derive
# link's freshness is judged against trails it by up to ~20 s (doc_get's own note gives that lag),
# so this is that window with a margin. It is not what makes the row correct - the read after it is
# - so a window that turns out short fails at the tip read, naming both numbers.
_TIP_SETTLE_S = 25.0


def _tip_read(p):
    """doc_get(include=['versions']) on the source: the cloud tip, banked for the row that waits on
    it to move."""
    v = p.get("versions") or {}
    return _measured("the source's cloud tip reads",
                     {"available": v.get("available"),
                      "latest_version_number": v.get("latest_version_number"),
                      "version_count": v.get("version_count")},
                     v.get("available") is True and _num(v.get("latest_version_number")))


def _tip_advanced(key):
    """doc_get(include=['versions']): the source's tip has moved PAST the number banked before the
    save - the read the reopen below stands on.

    MEASURED twice, opposite ways, on the same steps: a derive reference read out of date on one run
    and 'already up to date' on the next. What decides it is whether the cloud has published the
    source's new version by the time the host reopens, not how long anything slept - so the dwell
    ahead of this row only spends the expected lag, and THIS row is the gate. Its failure names the
    tip it saw beside the one it wanted, where a short dwell otherwise surfaces as the refresh row
    reporting a reference that is merely current."""
    def check(p):
        v = p.get("versions") or {}
        before, now = _RECALL.get(key), v.get("latest_version_number")
        return _measured(f"the source's tip advanced past version {before}",
                         {"tip_before": before, "latest_version_number": now,
                          "version_count": v.get("version_count"),
                          "history_readable": v.get("history_readable")},
                         _num(before) and _num(now) and now > before)
    return check


def _settled(name, key=None):
    """The read every lineage-URN capture goes through, and the only place the tier reads a URN off
    a just-saved document.

    No hold precedes it: doc_save_as pumps for the urn before it answers and publishes
    urn_wait_seconds, so a dwell here would only re-spend that wait. The failure this read exists
    for is silent at the row that causes it: the save reports 'saved' and the right name, the read
    banks whatever document_id is there, and it is TEN steps later - at an activate, a close and a
    delete - that a local path turns out not to address anything."""
    return [("doc_get", {}, _document_is(name),
             (key, lambda p: p["active"]["document_id"]) if key else None)]


# --- ACT 11a: THE DATA MODEL -------------------------------------------------------------------
# A folder tree of this run's own, one file uploaded into it, moved, read, downloaded - and then
# every one of them taken back out, each delete proven by its own read-back and by the project tree
# read afterwards, which is the witness standing apart from what the deletes reported.
_CLOUD_DATA = [
    # the address the tier comes home to at the end of every act - read, never assumed. A full
    # program hands it the story document; a partial run gets whatever was open, which the tier
    # reads and returns to and never writes.
    ("doc_get", {}, _home_document, ("home_doc", _home_address)),
    ("data_get", {}, _hub_is(HUB, PROJECT), None),
    ("data_create_folder", {"folder_name": RUN_FOLDER, "project": PROJECT,
                            "parent_folder": FOLDER},
     _folder_created(RUN_FOLDER, FOLDER), ("run_folder_id", lambda p: p["id"])),
    ("data_create_folder", {"folder_name": MOVED_FOLDER, "project": PROJECT,
                            "parent_folder": RUN_PATH},
     _folder_created(MOVED_FOLDER, RUN_PATH), ("moved_folder_id", lambda p: p["id"])),
    ("data_upload_file", _upload_args,
     _upload_started(RUN_PATH), ("upload_handle", lambda p: p["upload_handle"])),
    # The runner supplies the terminal response to the predicate and saved-value extractor.
    ("data_get_upload_status", _upload_status_args,
     _upload_complete, ("cloud_file", lambda p: p["file_id"])),
    ("data_get", lambda c: {"file": _ctx_get(c, "cloud_file", "the uploaded file")},
     _file_record(RUN_PATH), ("cloud_file_name", lambda p: p["file"]["name"])),
    ("data_move_file", lambda c: {"file": _ctx_get(c, "cloud_file", "the uploaded file"),
                                  "project": PROJECT, "target_folder": MOVED_PATH},
     _moved_to(MOVED_PATH), None),
    ("data_download_file", lambda c: {"file": _ctx_get(c, "cloud_file", "the uploaded file"),
                                      "destination_folder": DOWNLOAD_DIR, "overwrite": True},
     _downloaded, None),
    # THE GUARDED DELETE, met before the emptying ones: a folder still holding a subtree is refused,
    # so the recursive path is proven without ever running here. The fragments are the BLAST RADIUS
    # the guard measured at this moment - the run folder holds no file of its own and one subfolder,
    # and one file and one subfolder in the subtree below it - so a preview that under-counts what a
    # wipe would take reds here rather than reading as the same refusal.
    ("data_delete_folder", lambda c: _dependent_folder_args(c, "run_folder_id", RUN_FOLDER),
     _refused("is not empty", "immediate files: 0, subfolders: 1",
              "1 file(s) and 1 subfolder(s) total", "recursive_confirm"), None),
    ("data_delete_file", lambda c: {"document_id": _ctx_get(c, "cloud_file", "the uploaded file"),
                                    "confirm_name": _ctx_get(c, "cloud_file_name", "its name")},
     _file_deleted, None),
    ("data_delete_folder", lambda c: _dependent_folder_args(c, "moved_folder_id", MOVED_FOLDER),
     _folder_deleted(MOVED_FOLDER), None),
    ("data_delete_folder", lambda c: _dependent_folder_args(c, "run_folder_id", RUN_FOLDER),
     _folder_deleted(RUN_FOLDER), None),
    # THE DELETE RECEIPT, standing apart from what the delete calls reported about themselves: the
    # run folder is addressed DIRECTLY and the project answers that it has no such subfolder. A
    # scoped miss is a refusal, so it cannot be confused with a walk that ran out of budget - which a
    # project-wide folder-tree read is, measured, on every run against a real project. Moved sat
    # inside the run folder, so a run folder that no longer resolves is both of them gone.
    ("data_get", {"project": PROJECT, "folder": RUN_PATH},
     _refused("not found", "no subfolder", RUN_FOLDER), None),
]


# A DERIVE link's refresh, read after the source moved on and the host was closed and REOPENED.
# A derive is a one-way linked copy bound to the source's saved version. MEASURED by hand on a
# stale one: both refresh routes raise "2 : InternalValidationError : res" and isOutOfDate stays
# true, so doc_update_xref refuses and names delete-and-re-derive as the remedy. This leg is that
# measurement on a run - the rig wants a cloud save, a close and a reopen, none of which a
# measure_api row can build on the main thread.
_CLOUD_DERIVE = [
    ("doc_new", {}, _new_document, None),
    ("doc_insert_derive", lambda c: {"document_id": _ctx_get(c, "source_urn", "the source")},
     _derived(SOURCE_DOC), None),
    ("doc_save_as", {"name": DERIVE_DOC, "project": PROJECT, "folder": FOLDER},
     _saved_as(DERIVE_DOC, FOLDER), None),
] + _settled(DERIVE_DOC, "derive_urn") + [
    # the source moves past the version the derive holds. The edit ADDS a parameter rather than
    # re-valuing one: MEASURED on this rig, a source whose existing parameter changed value saves a
    # new version and the derive still reads 'already up to date', while a source that gained a new
    # parameter reads out of date - a new version alone does not stale a derive link.
    ("doc_activate", lambda c: {"name": _ctx_get(c, "source_urn", "the source")},
     _activated(SOURCE_DOC), None),
    # the tip BEFORE the save, so the row below compares against a number this run read rather than
    # a count of the saves the act has made.
    ("doc_get", {"include": ["versions"]}, _tip_read,
     ("source_tip_before", _recall("source_tip_before",
                                   lambda p: p["versions"]["latest_version_number"]))),
    ("param_add", {"name": "CloudDeriveMark", "expression": "3 mm"}, "ok", None),
    ("doc_save", {"description": "the edit the derive link goes stale against"},
     _versioned(SOURCE_DOC), None),
    _dwell(_TIP_SETTLE_S),
    ("doc_get", {"include": ["versions"]}, _tip_advanced("source_tip_before"), None),
    # CLOSED and REOPENED once the cloud is publishing the new version: the hand measurement was
    # taken on a reference the session had reloaded, not on one held open since the derive landed.
    ("doc_close", lambda c: {"name": _ctx_get(c, "derive_urn", "the derive host"),
                             "save_changes": False}, _document_closed, None),
    _dwell(6.0),
    ("doc_open", lambda c: {"file_id": _ctx_get(c, "derive_urn", "the derive host"),
                            "force_api_open": True}, _opened(DERIVE_DOC), None),
    _dwell(3.0),
    ("doc_get", {}, _document_is(DERIVE_DOC), None),
    # doc_update_xref assigns the version on a derive row and CATCHES Fusion's refusal, so the call
    # errors naming the row and the remedy rather than reporting a refresh it did not do. A row that
    # read CURRENT here would be skipped and the call would answer ok - which is why this asserts the
    # refusal rather than a bucket count.
    ("doc_update_xref", {"only_out_of_date": True},
     _refused("Some references failed to update", "Fusion refused the refresh",
              "doc_insert_derive"), None),
    ("doc_close", lambda c: {"name": _ctx_get(c, "derive_urn", "the derive host"),
                             "save_changes": False}, _document_closed, None),
    ("doc_activate", lambda c: {"name": _ctx_get(c, "source_urn", "the source")},
     _activated(SOURCE_DOC), None),
    _dwell(6.0),
    ("data_delete_file", lambda c: {"document_id": _ctx_get(c, "derive_urn", "the derive host"),
                                    "confirm_name": DERIVE_DOC}, _file_deleted, None),
]


# THE BOTH-MEMBERS DRIVE GUARD, across the save that RE-KEYS the document. Driving both members of a
# motion-linked pair in an xref assembly killed Fusion 2705.1.4, so joint_drive registers each driven
# joint under the document's key and refuses the partner while the pair does not read as wholly
# native. A FIRST save moves that key off the per-instance token onto the file's own id; the registry
# travels with it through _write_guard.on_key_renamed, and the refusal AFTER the save is what says it
# did rather than failing open - the direction that crashes.
_CLOUD_LINK = [
    ("doc_new", {}, _new_document, None),
    ("doc_insert_occurrence",
     lambda c: {"document_id": _ctx_get(c, "source_urn", "the source"), "x": _LINK_X[0]},
     _inserted(SOURCE_DOC), None),
    ("doc_insert_occurrence",
     lambda c: {"document_id": _ctx_get(c, "source_urn", "the source"), "x": _LINK_X[1]},
     _inserted(SOURCE_DOC), None),
    ("doc_insert_occurrence",
     lambda c: {"document_id": _ctx_get(c, "source_urn", "the source"), "x": _LINK_X[2]},
     _inserted(SOURCE_DOC), None),
    # one face per instance, taken design-wide and told apart by WHERE IT SITS and which way it
    # faces: nearest_to answers with the nearest face whether or not it is the one meant, so each
    # row asserts the centroid it expects and an outward +Z normal. The joints are built at HANDLES
    # because all three instances answer to the one stamped document name.
    ("find_geometry", _lit({"kind": "planar_face", "max_results": 1,
                            "nearest_to": [_LINK_X[0] + _PLATE_MID_X, _PLATE_MID_Y, _PLATE_TOP_Z]}),
     _face_up_at(_LINK_X[0] + _PLATE_MID_X, _PLATE_MID_Y, _PLATE_TOP_Z), _fg("link_face_a")),
    ("find_geometry", _lit({"kind": "planar_face", "max_results": 1,
                            "nearest_to": [_LINK_X[1] + _PLATE_MID_X, _PLATE_MID_Y, _PLATE_TOP_Z]}),
     _face_up_at(_LINK_X[1] + _PLATE_MID_X, _PLATE_MID_Y, _PLATE_TOP_Z), _fg("link_face_b")),
    ("find_geometry", _lit({"kind": "planar_face", "max_results": 1,
                            "nearest_to": [_LINK_X[2] + _PLATE_MID_X, _PLATE_MID_Y, _PLATE_TOP_Z]}),
     _face_up_at(_LINK_X[2] + _PLATE_MID_X, _PLATE_MID_Y, _PLATE_TOP_Z), _fg("link_face_c")),
    # two sliders CHAINED through the middle instance: different pairs, so neither meets Fusion's
    # already-jointed-pair refusal, and the chain keeps two degrees of freedom for the link to
    # couple. Both anchor on the top faces - one face serving two pairs is not a second joint on
    # one pair.
    ("joint_create", lambda c: {"occurrence_one": _ctx_get(c, "link_face_a", "the first plate"),
                                "occurrence_two": _ctx_get(c, "link_face_b", "the second plate"),
                                "joint_type": "slider", "axis": "x", "name": "XrefSlideA"},
     _jointed("XrefSlideA"), None),
    ("joint_create", lambda c: {"occurrence_one": _ctx_get(c, "link_face_b", "the second plate"),
                                "occurrence_two": _ctx_get(c, "link_face_c", "the third plate"),
                                "joint_type": "slider", "axis": "x", "name": "XrefSlideB"},
     _jointed("XrefSlideB"), None),
    ("joint_motion_link", {"joint_one": "XrefSlideA", "joint_two": "XrefSlideB", "ratio": 1},
     _motion_linked("XrefSlideA", "XrefSlideB", False), None),
    ("joint_drive", {"joint_name": "XrefSlideA", "distance": 5}, _driven_slide(5), None),
    # the save that re-keys: an unsaved document keys by the token minted on first sight, and this
    # save moves that key onto the file's own id.
    ("doc_save_as", {"name": LINK_DOC, "project": PROJECT, "folder": FOLDER},
     _saved_as(LINK_DOC, FOLDER), None),
] + _settled(LINK_DOC, "link_urn") + [
    # THE REFUSAL, on the other member. What is asserted is the guard's three readings - the link,
    # the partner already driven, and the pair not reading as wholly native. Whether the first
    # drive moved THIS joint through the link is something joint_drive says it does not read, so
    # nothing here claims it either way.
    ("joint_drive", {"joint_name": "XrefSlideB", "distance": 5},
     _refused("'XrefSlideB' is motion-linked to 'XrefSlideA'", "already driven this session",
              "did NOT read as wholly native"), None),
    ("doc_close", lambda c: {"name": _ctx_get(c, "link_urn", "the link assembly"),
                             "save_changes": False}, _document_closed, None),
    ("doc_activate", lambda c: {"name": _ctx_get(c, "source_urn", "the source")},
     _activated(SOURCE_DOC), None),
    _dwell(6.0),
    ("data_delete_file", lambda c: {"document_id": _ctx_get(c, "link_urn", "the link assembly"),
                                    "confirm_name": LINK_DOC}, _file_deleted, None),
]


# --- ACT 11b: THE SAVED DOCUMENT ---------------------------------------------------------------
# A small plate built in a document of its own, saved into the configured folder, versioned,
# milestoned, rolled back, copied, and inserted as an xref into a host saved beside it. The SOURCE is
# left standing - the drawing act generates from it and deletes it; the copy and the host are this
# act's own and go at the end of it.
_CLOUD_DOC = [
    ("doc_new", {}, _new_document, None),
    ("model_create_component", {"name": SRC_COMP, "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": SRC_SKETCH}, "ok", None),
    ("sketch_add_geometry", _lit({"geometry": [{"kind": "rectangle", "x1": 0, "y1": 0,
                                                "x2": 80, "y2": 50}],
                                  "sketch_name": SRC_SKETCH}), "ok", None),
    ("model_extrude", _lit({"sketch_name": SRC_SKETCH, "profile_index": 0, "distance": 10}),
     _extruded, None),
    _watch(SRC_COMP + ":1"),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("doc_save_as", {"name": SOURCE_DOC, "project": PROJECT, "folder": FOLDER},
     _saved_as(SOURCE_DOC, FOLDER), None),
    # the lineage URN read off the SESSION rather than off the save: doc_save_as waits for the urn
    # before it answers, and this read is where the tier refuses a local path in its place.
] + _settled(SOURCE_DOC, "source_urn") + [
    ("param_add", {"name": "CloudPlateH", "expression": "10 mm"}, "ok", None),
    ("doc_save", {"description": "the cloud tier's first version"}, _versioned(SOURCE_DOC), None),
    ("param_set", {"name": "CloudPlateH", "expression": "14 mm"}, "ok", None),
    ("doc_save_milestone", {"milestone_name": "SweepCloudMilestone",
                            "description": "the cloud tier's milestone beat"},
     _milestoned("SweepCloudMilestone", SOURCE_DOC), None),
    ("doc_get", {"include": ["versions"]}, _versions_read(2), None),
    ("doc_restore_version", {"version_number": RESTORE_VERSION}, _restored(RESTORE_VERSION), None),
    ("doc_copy", lambda c: {"document_id": _ctx_get(c, "source_urn", "the saved source"),
                            "name": COPY_DOC, "project": PROJECT, "folder": FOLDER},
     _copied(SOURCE_DOC, COPY_DOC, FOLDER), ("copy_urn", lambda p: p["copied_id"])),
    # THE HOST, saved before anything is put in it so it has a lineage URN of its own - every switch
    # and every delete below addresses a document by URN, which an unsaved one does not have.
    ("doc_new", {}, _new_document, None),
    ("doc_save_as", {"name": HOST_DOC, "project": PROJECT, "folder": FOLDER},
     _saved_as(HOST_DOC, FOLDER), None),
] + _settled(HOST_DOC, "host_urn") + [
    ("doc_insert_occurrence", lambda c: {"document_id": _ctx_get(c, "source_urn", "the source")},
     _inserted(SOURCE_DOC), None),
    # THE FIRST WALK, straight after the insert: one reference, in whichever bucket the cloud's
    # version stream leaves it. MEASURED: an xref inserted moments earlier came back was_out_of_date
    # true at version 2 while the source had reached 3, so 'already current on insert' is not a thing
    # this act can assert - it reports which bucket, and brings the reference current.
    ("doc_update_xref", {"only_out_of_date": True}, _xrefs(1), None),
    # THE SKIP, deterministic because the walk above just brought that reference current: nothing is
    # out of date, so only_out_of_date leaves it alone rather than refreshing it again.
    ("doc_update_xref", {"only_out_of_date": True}, _xrefs(1, 0, 1), None),
    # ...then the source is edited and versioned, and the same walk finds the reference stale. The
    # two rows above and this one put a row in each bucket, which is what makes the partition a
    # measurement rather than a shape.
    ("doc_activate", lambda c: {"name": _ctx_get(c, "source_urn", "the source")},
     _activated(SOURCE_DOC), None),
    ("param_set", {"name": "CloudPlateH", "expression": "16 mm"}, "ok", None),
    ("doc_save", {"description": "the edit the host's reference refresh is read against"},
     _versioned(SOURCE_DOC), None),
    ("doc_activate", lambda c: {"name": _ctx_get(c, "host_urn", "the host")},
     _activated(HOST_DOC), None),
    # doc_activate's own switch is ASYNC - it reports 'pending' where the foreground has not caught
    # up, and its note says to confirm with doc_get before acting on the new document. The walk below
    # reads THE ACTIVE document's references, so an unconfirmed switch does not fail it: it reads
    # some other document's, which is how a walk aimed at the host reported no references at all.
    ("doc_get", {}, _document_is(HOST_DOC), None),
    ("doc_update_xref", {"only_out_of_date": True}, _xrefs(1, 1, 0), None),
    ("doc_open", lambda c: {"file_id": _ctx_get(c, "copy_urn", "the copy"),
                            "force_api_open": True}, _opened(COPY_DOC), None),
    _dwell(3.0),
    ("doc_get", {}, _document_is(COPY_DOC), None),
    ("doc_activate", lambda c: {"name": _ctx_get(c, "source_urn", "the source")},
     _activated(SOURCE_DOC), None),
    # a Fusion design is refused by data_download_file BY NAME, which is what sends a caller to
    # design_export - the branch the PNG round trip in the data act cannot reach.
    ("data_download_file", lambda c: {"file": _ctx_get(c, "source_urn", "the source"),
                                      "destination_folder": DOWNLOAD_DIR},
     _refused("Fusion-native", "design_export"), None),
] + _CLOUD_DERIVE + _CLOUD_LINK + [
    # TEARDOWN of this act's own two. The host goes first: it REFERENCES the source, and a referenced
    # file's delete is refused rather than orphaning what points at it.
    ("doc_close", lambda c: {"name": _ctx_get(c, "host_urn", "the host"), "save_changes": False},
     _document_closed, None),
    ("doc_close", lambda c: {"name": _ctx_get(c, "copy_urn", "the copy"), "save_changes": False},
     _document_closed, None),
    # a delete taken on a just-closed file has been observed to raise until the close settles cloud
    # side, so the hold is here rather than a retry - a delete that still refuses is reported.
    _dwell(6.0),
    ("data_delete_file", lambda c: {"document_id": _ctx_get(c, "host_urn", "the host"),
                                    "confirm_name": HOST_DOC}, _file_deleted, None),
    ("data_delete_file", lambda c: {"document_id": _ctx_get(c, "copy_urn", "the copy"),
                                    "confirm_name": COPY_DOC}, _file_deleted, None),
    ("doc_activate", lambda c: {"name": _ctx_get(c, "home_doc", "the home document")},
     _activated(), None),
]


# --- ACT 11c: THE DRAWING ----------------------------------------------------------------------
# The drawing generated from the saved source, edited, exported, refreshed against a source edit and
# exported again - then the source and its drawing taken back out, and the session left on the
# document the tier started on.
_CLOUD_DRAWING = [
    ("doc_activate", lambda c: {"name": _ctx_get(c, "source_urn", "the source")},
     _activated(SOURCE_DOC), None),
    ("doc_get", {}, _document_is(SOURCE_DOC), None),
    # the generator reads its source from the CLOUD, so the file's own record - is_complete among it
    # - is polled to settled first, and a create refusing behind a source still processing is
    # diagnosed by this row rather than by a retry.
    ("data_get", lambda c: {"file": _ctx_get(c, "source_urn", "the source")},
     _file_settled(FOLDER), None),
    ("drawing_create", {"standard": "iso", "units": "mm", "sheet_size": "a3"},
     _drawing_created, ("drawing", lambda p: [p["file_id"], p["drawing_name"]])),
    ("doc_open", lambda c: {"file_id": _ctx_get(c, "drawing", "the drawing")[0],
                            "force_api_open": True},
     lambda p: p.get("opened") is True, None),
    _dwell(4.0),
    # THE SHEET READ that answers - taken before the writes below, and again before each export.
    ("drawing_get", {}, _sheets_answer, None),
    ("drawing_update", {}, _drawing_current, None),
    ("drawing_dimension", {"view": 0, "strategy": "baseline"}, _dimensioned, None),
    ("drawing_insert_image", {"image_path": MARKER_PNG, "x": 150, "y": 100}, _image_placed, None),
    ("drawing_add_sketch", {"name": "SweepCloudSketch", "geometry": [
        {"kind": "line", "points": [[0, 0], [30, 0], [30, 20]]},
        {"kind": "rectangle", "points": [[40, 5], [70, 25]]},
        {"kind": "circle", "points": [[15, 35]], "radius": 5},
    ]}, _sketch_landed("SweepCloudSketch", 4), None),
    # an add makes the NEW sheet active, so it comes after every beat that needs the generated views.
    ("drawing_edit_sheet", {"action": "add", "new_name": "SweepCloudSheet"},
     _sheet_added("SweepCloudSheet"), None),
    ("drawing_get", {}, _sheets_answer, None),
    ("drawing_export", {"format": "pdf",
                        "file_path": DOWNLOAD_DIR + f"/sweep_drawing_{_STAMP}.pdf"},
     _exported, None),
    # THE ROUND TRIP: edit the source, version it, and refresh the drawing that points at it.
    ("doc_activate", lambda c: {"name": _ctx_get(c, "source_urn", "the source")},
     _activated(SOURCE_DOC), None),
    ("param_set", {"name": "CloudPlateH", "expression": "18 mm"}, "ok", None),
    ("doc_save", {"description": "the edit the drawing refresh is read against"},
     _versioned(SOURCE_DOC), None),
    ("doc_activate", lambda c: {"name": _ctx_get(c, "drawing", "the drawing")[0]},
     _activated(), None),
    _dwell(4.0),
    ("drawing_get", {}, _sheets_answer, None),
    ("drawing_update", {}, _drawing_refreshed, None),
    ("drawing_export", {"format": "dxf",
                        "file_path": DOWNLOAD_DIR + f"/sweep_drawing_{_STAMP}.dxf"},
     _exported, None),
    # TEARDOWN. The drawing REFERENCES the source, so it closes and deletes first, for the same
    # reason the host did.
    ("doc_close", lambda c: {"name": _ctx_get(c, "drawing", "the drawing")[0],
                             "save_changes": False}, _document_closed, None),
    ("doc_close", lambda c: {"name": _ctx_get(c, "source_urn", "the source"),
                             "save_changes": False}, _document_closed, None),
    ("doc_activate", lambda c: {"name": _ctx_get(c, "home_doc", "the home document")},
     _activated(), None),
    _dwell(6.0),
    ("data_delete_file", lambda c: {"document_id": _ctx_get(c, "drawing", "the drawing")[0],
                                    "confirm_name": _ctx_get(c, "drawing", "the drawing")[1]},
     _file_deleted, None),
    # the source was versioned above and closed just now; a delete taken while the cloud is still
    # processing that version raises (InternalValidationError, measured), so the record is polled to
    # settled first and a delete that still refuses is reported.
    ("data_get", lambda c: {"file": _ctx_get(c, "source_urn", "the source")},
     _file_settled(FOLDER), None),
    ("data_delete_file", lambda c: {"document_id": _ctx_get(c, "source_urn", "the source"),
                                    "confirm_name": SOURCE_DOC}, _file_deleted, None),
    # the witness standing apart from the deletes' own reports: the configured folder read back.
    # NOT recursive - the tier's documents were saved into the folder ITSELF, and a recursive walk of a
    # working folder is a walk of every run that ever used it (measured: 49 files over 30-odd
    # subfolders, past the 20 s budget, which would red this row for the folder's size).
    ("data_get", {"project": PROJECT, "folder": FOLDER, "recursive": False},
     _files_gone(SOURCE_DOC, COPY_DOC, HOST_DOC, DERIVE_DOC, LINK_DOC), None),
]
