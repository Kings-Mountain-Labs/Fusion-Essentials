# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Save the ACTIVE (possibly never-saved) document into a project/folder via Document.saveAs.
saveAs can raise or return false AFTER the file landed, so the destination is read back before the
call is reported as a failure. WRITES."""

import time

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _doc_common
from . import _export
from ._data_common import (
    _agent_description, _data, _files_in_folder_by_name, _find_project, _same_name_refusal,
    _same_name_rows, _split_path, _resolve_folder_path, _ensure_folder_path, _folder_path_string,
    _retained_parents,
)

app = adsk.core.Application.get()

# Post-saveAs the cloud assigns the lineage URN asynchronously - doc.dataFile.id reads a local
# pre-upload path (not a 'urn:') until it arrives, and the wait is bounded by the same window a
# version read is measured to trail the cloud in. Capped, so a URN that never lands cannot hang.
_URN_WAIT_S = float(_doc_common.VERSION_LAG_WINDOW_S)
_URN_POLL_SLEEP = 0.25

# A DECLINED save - saveAs answered false AND the destination folder holds nothing new - gets a
# brief probe instead of that window: the platform said it wrote nothing, so the read can only
# catch a URN already there, and waiting the full window would delay every honest refusal.
_DECLINED_PROBE_S = 1.0


def _settled_lineage_urn(doc, wait_s=_URN_WAIT_S):
    """(the lineage 'urn:' doc.dataFile.id settles to, seconds spent waiting) - the URN None when it
    did not settle inside `wait_s`. That URN is the stable identity two same-named files differ by."""
    def probe():
        df = safe(lambda: doc.dataFile)
        raw = safe(lambda: df.id) if df else None
        return bool(isinstance(raw, str) and raw.startswith("urn:")), raw

    started = time.monotonic()
    settled, raw = _export.pump_until(probe, wait_s, _URN_POLL_SLEEP)
    return (raw if settled else None), round(time.monotonic() - started, 1)


def _resolve_folder_eventual(root, segments):
    """Resolve an existing folder path with ONE bounded retry: (folder, missing, retried). A cloud
    listing can report a segment MISSING that IS in the freshly enumerated sibling list; only that
    exact self-contradiction is retried, a segment absent from the siblings is a real miss."""
    target, missing = _resolve_folder_path(root, segments)
    if target is not None or not missing:
        return target, missing, False
    siblings = [safe(lambda f=f: f.name) for f in safe(lambda: root.dataFolders.asArray(), []) or []]
    if missing in [s for s in siblings if s]:
        safe(lambda: adsk.doEvents())          # nudge the cloud folder cache to settle, then re-resolve
        target2, missing2 = _resolve_folder_path(root, segments)
        return target2, missing2, True
    return target, missing, False


def handler(name: str = "", project: str = "", project_id: str = "",
            folder: str = "", create_path: bool = False,
            description: str = "", allow_duplicate_name: bool = False) -> dict:
    """Save the ACTIVE (possibly never-saved) document into a project/folder via Document.saveAs."""
    name = (name or "").strip()
    if not name:
        return error("Provide 'name' for the saved document.")
    if not (project or project_id):
        return error("Provide 'project' (name) or 'project_id' for the destination.")

    doc = safe(lambda: app.activeDocument)
    if not doc:
        return error("No active document to save. Open a document first.")

    # Report whether this was an unsaved doc (the expected Phase-3 case) for the caller.
    was_saved = safe(lambda: doc.isSaved, None)

    try:
        data = _data()
    except Exception as e:
        return error(str(e))

    proj, available = _find_project(data, name=project or None, project_id=project_id or None)
    if not proj:
        ident = project_id or project
        return error(f"Destination project not found: {ident}. Available: "
                      f"{', '.join(available) or '(none)'}")
    try:
        root = proj.rootFolder
    except Exception as e:
        return error(f"Could not access destination project root: {e}")

    target = root
    auto_created = []
    folder_retry_note = None
    segments = _split_path(folder)
    if segments:
        if create_path:
            try:
                target, auto_created = _ensure_folder_path(root, segments)
            except Exception as e:
                return error(f"Could not prepare destination path '{folder}': {e}")
        else:
            target, missing, retried = _resolve_folder_eventual(root, segments)
            if not target:
                opts = [safe(lambda: f.name) for f in safe(lambda: root.dataFolders.asArray(), [])]
                return error(
                    f"Destination folder path not found: '{folder}' (missing segment "
                    f"'{missing}'). Folders at project root: "
                    f"{', '.join(n for n in opts if n) or '(none)'}. "
                    "Pass create_path=true, or use data_get(include=['folders']) to see the structure.")
            if retried:
                folder_retry_note = (f"folder '{folder}' did not resolve on the first read though it "
                                     "was present in the project's folder list, then resolved on a "
                                     "retry - cloud folder listings can lag right after a save/outage "
                                     "(eventual-consistency).")

    # Fusion PERMITS same-name documents (identity is the lineage URN, not the name), and saveAs on
    # a colliding name FORKS a new lineage - so a pre-existing same-name file is refused by default,
    # the fork available deliberately via allow_duplicate_name=true.
    existing_files, preflight_problem = _files_in_folder_by_name(target, name)
    if preflight_problem and not allow_duplicate_name:
        return error(
            preflight_problem + " doc_save_as cannot verify that the destination name is free. "
            "Retry after the folder is readable, choose another folder, or pass "
            "allow_duplicate_name=true only if creating a same-name fork is intentional."
            + _retained_parents(auto_created))
    preflight_complete = preflight_problem is None
    existing = existing_files[0] if len(existing_files) == 1 else None
    existing_id = safe(lambda: existing.id) if existing else None
    if len(existing_files) > 1 and not allow_duplicate_name:
        return error(
            _same_name_refusal(target, name, existing_files) + " doc_save_as would add yet ANOTHER "
            "file of that name (a new lineage) - refused by default. To add a version to one of the "
            "files above, open that URN (doc_open) and use doc_save; to create a same-name file "
            "anyway, pass allow_duplicate_name=true." + _retained_parents(auto_created))
    if existing and not allow_duplicate_name:
        return error(
            f"A file named '{name}' already exists in "
            f"'{_folder_path_string(target) or '(project root)'}' (URN {existing_id}). doc_save_as "
            "would FORK a SECOND file with the same name (a new lineage) - refused by default. To "
            "add a version to the EXISTING file, open it by that URN (doc_open) and use doc_save; to "
            "deliberately create a same-name fork anyway, pass allow_duplicate_name=true."
            + _retained_parents(auto_created))

    def _landed_after_error(wait_s):
        """Return (landed proof, readable matches, wait seconds, post-census problem)."""
        now, post_problem = _files_in_folder_by_name(target, name)
        if now and preflight_complete and not existing_files:
            if len(now) == 1 and post_problem is None:
                return safe(lambda: now[0].id) or True, [], None, None
            # A complete empty pre-census proves readable matches appeared, but an incomplete
            # post-census cannot identify one lineage or establish an exhaustive count.
            return True, now, None, post_problem
        if was_saved is False:
            urn, waited = _settled_lineage_urn(doc, wait_s)
            if urn:
                return urn, [], waited, post_problem
        return None, [], None, post_problem

    def _landed_ok(file_id, how, same_name_now=(), waited=None, post_problem=None):
        # doc.dataFile.id names this call's file ONLY for a never-saved document, else null - never
        # a wrong URN. This re-probe spends the FULL window even from the declined path: files under
        # that name DID land, so a URN is settling behind them.
        resolved, waited = ((file_id, waited) if isinstance(file_id, str)
                            else (_settled_lineage_urn(doc)
                                  if was_saved is False else (None, None)))
        payload = {
            "saved": True,
            "name": name,
            "was_previously_saved": was_saved,
            "destination_project": safe(lambda: proj.name),
            "destination_folder": (_folder_path_string(target) or "(project root)"),
            "auto_created_parents": auto_created,
            "document_id": resolved,
            "recovered_from_error": True,
            "note": ("saveAs reported an error but the file DID land in the destination (verified by "
                     "reading the saved document/folder back) - reporting success rather than a false "
                     "negative, which would send a retry into a 'file already exists' collision. " + how),
        }
        if waited is not None:
            payload["urn_wait_seconds"] = waited
        if same_name_now and post_problem:
            payload["known_same_name_document_ids"] = [safe(lambda f=f: f.id)
                                                        for f in same_name_now]
            payload["name_census_incomplete"] = post_problem
            payload["note"] += (
                f" At least {len(same_name_now)} readable file(s) named '{name}' appeared where a "
                "complete pre-save census found none, but the post-save census was incomplete; "
                "the known IDs are not an exhaustive count or proof of this call's lineage. "
                + post_problem)
        elif same_name_now:
            # One entry per file, null where the id would not read - the count and the URNs are the
            # only handles on the duplicate this recovery just measured.
            payload["same_name_document_ids"] = [safe(lambda f=f: f.id) for f in same_name_now]
            payload["note"] += (
                f" {len(same_name_now)} files named '{name}' are in that folder now where none was "
                f"before, so which lineage THIS call wrote is not readable from the folder: "
                f"{_same_name_rows(same_name_now)}.")
        elif post_problem:
            payload["name_census_incomplete"] = post_problem
            payload["note"] += (" The post-save name census was incomplete; recovery uses only the "
                                "independently settled document lineage. " + post_problem)
        if resolved is None:
            payload["note"] += (" 'document_id' is null - nothing read back names this call's file "
                                "exactly. List the folder with data_get(project, folder) and address "
                                "the file you meant by its URN.")
        if preflight_problem:
            payload["preflight_name_census_incomplete"] = preflight_problem
            payload["note"] += (" The caller allowed a same-name fork while the pre-save name "
                                "census was incomplete: " + preflight_problem)
        return ok(payload)

    try:
        did = doc.saveAs(name, target, _agent_description(description), "")  # adsk.core: Document.saveAs(...)
    except Exception as e:
        # saveAs can raise (observed: InternalValidationError) AFTER the file landed - re-read before
        # failing, and give the URN the full window, since a commit may be settling behind the raise.
        landed, same_name_now, waited, post_problem = _landed_after_error(_URN_WAIT_S)
        if landed:
            return _landed_ok(landed, f"Original error: {str(e)[:160]}", same_name_now, waited,
                              post_problem)
        uncertainty = preflight_problem or post_problem
        if uncertainty:
            return error(f"saveAs raised for '{name}' ({str(e)[:160]}), and the destination name "
                         f"census was incomplete, so whether a file landed is unconfirmed: "
                         f"{uncertainty}" + _retained_parents(auto_created))
        return error(f"saveAs failed for '{name}': {e}" + _retained_parents(auto_created))
    if not did:
        landed, same_name_now, waited, post_problem = _landed_after_error(_DECLINED_PROBE_S)
        if landed:
            return _landed_ok(landed, "saveAs returned false.", same_name_now, waited,
                              post_problem)
        uncertainty = preflight_problem or post_problem
        if uncertainty:
            return error(f"saveAs returned false for '{name}', and the destination name census "
                         f"was incomplete, so whether a file landed is unconfirmed: {uncertainty}"
                         + _retained_parents(auto_created))
        return error(f"Fusion declined to save '{name}' (saveAs returned false). The complete "
                     "name readback did not establish whether this call landed, so no success is "
                     "reported."
                     + _retained_parents(auto_created))

    # Report the lineage URN this save wrote - the stable identity that ADDRESSES the file (a name
    # can be shared). It resolves asynchronously, so the call waits for it rather than returning null.
    new_id, urn_wait = _settled_lineage_urn(doc)

    note = ("The saved document becomes the active document. Its 'document_id' is the lineage URN - "
            "the stable identity to address it by (doc_open/doc_activate/data_delete_file); a NAME can "
            "be shared by several files, the URN cannot. 'urn_wait_seconds' is how long this call "
            "waited for it, so a caller needs no dwell of its own.")
    if new_id is None:
        note += (f" It had NOT arrived after {urn_wait}s - what dataFile.id reads meanwhile is a "
                 "LOCAL path, not an address. Read the URN from doc_get before using one.")
    result = {
        "saved": True,
        "name": name,
        "was_previously_saved": was_saved,
        "destination_project": safe(lambda: proj.name),
        "destination_folder": (_folder_path_string(target) or "(project root)"),
        "auto_created_parents": auto_created,
        "document_id": new_id,   # the lineage URN of the file just written (null only if not yet settled)
        "urn_wait_seconds": urn_wait,
    }
    if preflight_problem:
        result["name_census_incomplete"] = preflight_problem
        note = ("NAME CENSUS INCOMPLETE - allow_duplicate_name authorized the save despite an "
                "incomplete pre-save collision check. " + preflight_problem + " " + note)
    elif existing_id and existing_id != new_id:
        result["name_collision"] = {
            "existing_document_id": existing_id,
            "warning": (f"A different file named '{name}' already existed in this folder "
                        f"({existing_id}); this saveAs created a SECOND file with the same name (a new "
                        "lineage - Fusion allows this). To add a version to the EXISTING file instead, "
                        "open it (doc_open by that URN) and use doc_save; or delete one with "
                        "data_delete_file. Address files by URN, not name, from here."),
        }
        note = (f"NAME COLLISION - see 'name_collision'. " + note)
    elif len(existing_files) > 1:
        # The name was ALREADY shared before this save (only reachable with allow_duplicate_name), so
        # every pre-existing lineage is listed - one entry per file, null where the id would not read.
        prior_ids = [safe(lambda f=f: f.id) for f in existing_files]
        result["name_collision"] = {
            "existing_document_ids": prior_ids,
            "warning": (f"{len(existing_files)} files named '{name}' already existed in this folder "
                        f"({_same_name_rows(existing_files)}); this saveAs added another "
                        "one (a new lineage - Fusion allows this). To add a version to one of them "
                        "instead, open that URN (doc_open) and use doc_save. Address files by URN, "
                        "not name, from here."),
        }
        note = (f"NAME COLLISION - see 'name_collision'. " + note)
    if folder_retry_note:
        result["folder_resolve_retried"] = True
        note = folder_retry_note + " " + note
    result["note"] = note
    return ok(result)


TOOL_DESCRIPTION = (
    "Save the ACTIVE document into a cloud project/folder under 'name' - including a design never "
    "saved before."
)

tool = (
    Tool.create_with_string_input(
        name="doc_save_as",
        description=TOOL_DESCRIPTION,
        input_param_name="name",
        input_param_description="Name for the saved document.",
    )
    .add_input_property("project", {"type": "string", "description": "Destination project name."})
    .add_input_property("project_id", {"type": "string"})
    .add_input_property("folder", {"type": "string",
        "description": "Destination folder path, e.g. 'Parts/WidgetA'."})
    .add_input_property("create_path", {"type": "boolean",
        "description": "Create missing folders."})
    .add_input_property("description", {"type": "string"})
    .add_input_property("allow_duplicate_name", {"type": "boolean"})
    .strict_schema()
)
# enforce_timeout=False: saveAs is a blocking, uninterruptible main-thread cloud write that COMMITS,
# and this call then waits out the URN - a timeout would report a false failure for a landed file,
# and a retry on that report forks a duplicate lineage.
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler,
    run_on_main_thread=True, enforce_timeout=False,
    # The destination folder is re-listed for the requested NAME and the saved document's lineage URN
    # is pumped until it settles - those two reads, not saveAs' own answer, decide what is reported.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_doc_save_as.py::TestSaveDocumentAs"
                      "::test_saveas_returns_false_but_file_landed_recovers_as_ok",
        rung="value")
)


def register_tool():
    register(item)
