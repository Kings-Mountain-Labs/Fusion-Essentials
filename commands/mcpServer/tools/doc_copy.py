# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Copy an existing cloud document into a destination project/folder. Cloud-to-cloud: it does not
touch the active session, and the copy PRESERVES external references. WRITES."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import counted, ok, error, read_flag, safe
from ._data_common import (
    _MAX_XREFS, _data, _files_in_folder_by_name, _find_project, _split_path,
    _resolve_folder_path, _ensure_folder_path, _folder_path_string,
    _same_name_refusal, _retained_parents,
)


def _xref_summary(data_file):
    """A DataFile's child references and how many there are: (rows, count), rows capped at
    _MAX_XREFS while count stays the true total. count is None when the reference read did not
    answer - an unreadable read is not "this file references nothing"."""
    if read_flag(lambda: data_file.hasChildReferences) is False:
        return [], 0
    refs = safe(lambda: data_file.childReferences.asArray())
    count = counted(lambda: len(refs))
    out = []
    for r in (refs or []):
        out.append({"name": safe(lambda: r.name), "id": safe(lambda: r.id)})
        if len(out) >= _MAX_XREFS:
            break
    return out, count


# The by-name folder walk's HARD budget. Every folder visited costs TWO cloud fetches (dataFiles +
# dataFolders) on Fusion's MAIN thread at ~0.8 s/folder, so an unbounded walk stalls the UI; a
# bigger project must be addressed by document_id (URN) or narrowed with source_folder.
_WALK_FOLDER_BUDGET = 20


def _find_file_by_name(root_folder, name):
    """Every DataFile named `name` (case-insensitive) under `root_folder`, breadth-first and bounded
    by _WALK_FOLDER_BUDGET: (matches, seen_names, visited, truncated, unread), each match a (file,
    folder_path) pair. Fusion allows same-name files in DIFFERENT folders, so ALL matches are
    collected; `truncated` and `unread` are the holes a caller must refuse on rather than trust."""
    want = (name or "").strip().lower()
    seen = []
    matches = []
    unread = []
    queue = [root_folder] if root_folder is not None else []
    visited = 0
    while queue and visited < _WALK_FOLDER_BUDGET:
        folder = queue.pop(0)
        visited += 1
        unreadable_here = False
        try:
            for f in folder.dataFiles.asArray():
                try:
                    nm = f.name
                except Exception:
                    unreadable_here = True
                    continue
                if not isinstance(nm, str) or not nm.strip():
                    unreadable_here = True
                    continue
                seen.append(nm)
                if nm.strip().lower() == want:
                    matches.append((f, _folder_path_string(folder)))
        except Exception:
            unreadable_here = True
        try:
            for sub in folder.dataFolders.asArray():
                queue.append(sub)
        except Exception:
            unreadable_here = True
        if unreadable_here:
            unread.append(_folder_path_string(folder) or "(project root)")
    return matches, seen, visited, bool(queue), unread


def handler(document_id: str = "", name: str = "",
            source_project: str = "", source_project_id: str = "",
            source_folder: str = "",
            project: str = "", project_id: str = "",
            folder: str = "", create_path: bool = False) -> dict:
    """Copy an existing cloud document into a destination project/folder; see TOOL_DESCRIPTION."""
    if not (document_id or name):
        return error("Provide 'document_id' (lineage URN, preferred) or 'name'.")
    if not (project or project_id):
        return error("Provide 'project' (name) or 'project_id' for the destination.")

    try:
        data = _data()
    except Exception as e:
        return error(str(e))

    # --- resolve the source DataFile ---
    src = None
    unread = []          # folders the by-name walk could not enumerate (empty on the URN path)
    if document_id:
        try:
            src = data.findFileById(document_id)
        except Exception as e:
            return error(f"findFileById failed for '{document_id}': {e}")
        if not src:
            return error(f"No file found for document_id '{document_id}'. "
                                      "Pass the file's lineage id (URN) from data_get.")
    else:
        # Name lookup within a source project (needed because names aren't globally unique).
        if not (source_project or source_project_id):
            return error("When using 'name', also provide 'source_project' "
                                      "(name) or 'source_project_id' so the lookup is unambiguous.")
        sproj, savail = _find_project(data, name=source_project or None,
                                      project_id=source_project_id or None)
        if not sproj:
            ident = source_project_id or source_project
            return error(f"Source project not found: {ident}. Available: "
                          f"{', '.join(savail) or '(none)'}")
        start = safe(lambda: sproj.rootFolder)
        if start is None:
            return error(f"Could not access the root folder of source project "
                         f"'{safe(lambda: sproj.name)}'.")
        scope_label = "(project root)"
        sf_segments = _split_path(source_folder)
        if sf_segments:
            start, sf_missing = _resolve_folder_path(start, sf_segments)
            if not start:
                opts = [safe(lambda: f.name) for f in
                        safe(lambda: sproj.rootFolder.dataFolders.asArray(), [])]
                return error(
                    f"source_folder path not found: '{source_folder}' (missing segment "
                    f"'{sf_missing}') in project '{safe(lambda: sproj.name)}'. Folders at project "
                    f"root: {', '.join(n for n in opts if n) or '(none)'}. "
                    "Use data_get(include=['folders']) to see the structure.")
            scope_label = _folder_path_string(start) or scope_label
        matches, seen, visited, truncated, unread = _find_file_by_name(start, name)
        if truncated:
            # A partial search cannot prove the name is unique (an unsearched folder could hold a
            # same-name twin), so a budget-cut walk is REFUSED - never acted on. Name what was
            # searched and the two exact paths that do not need a walk.
            found = "; ".join(
                f"'{safe(lambda f=f: f.name)}' in '{path or '(project root)'}' "
                f"(URN {safe(lambda f=f: f.id)})" for f, path in matches)
            return error(
                f"By-name search stopped at its budget: visited {visited} folders "
                f"(cap {_WALK_FOLDER_BUDGET}) under {scope_label} of project "
                f"'{safe(lambda: sproj.name)}' without covering it ({len(seen)} files seen"
                + (f"; matches so far: {found}" if found else "") + "). The walk is bounded because "
                "each folder is a slow cloud fetch on Fusion's main thread. Pass document_id (the "
                "lineage URN, from data_get" + (" or the matches above" if found else "") + ") to "
                "skip the walk, or narrow it with source_folder='<path>'.")
        if unread:
            found = "; ".join(
                f"'{safe(lambda f=f: f.name)}' in '{path or '(project root)'}' "
                f"(URN {safe(lambda f=f: f.id)})" for f, path in matches)
            return error(
                f"By-name source search could not completely read {len(unread)} folder(s) "
                f"({', '.join(unread)}) under {scope_label} of project "
                f"'{safe(lambda: sproj.name)}'"
                + (f"; matches read so far: {found}" if found else "")
                + ". Refusing because an unread file name or folder can hide a same-name twin. "
                "Retry after the folders are readable, pass document_id (URN), or narrow the "
                "search with source_folder='<path>'.")
        if not matches:
            return error(f"Document '{name}' not found under {scope_label} of source project "
                          f"'{safe(lambda: sproj.name)}'. Files seen: "
                          f"{', '.join(seen[:30]) or '(none)'}. "
                          "Use data_get, or pass document_id (URN).")
        if len(matches) > 1:
            rows = "; ".join(
                f"'{safe(lambda f=f: f.name)}' in '{path or '(project root)'}' "
                f"(URN {safe(lambda f=f: f.id)})"
                for f, path in matches)
            return error(
                f"Source name '{name}' is ambiguous - {len(matches)} files share it in project "
                f"'{safe(lambda: sproj.name)}': {rows}. Fusion allows same-name files in different "
                "folders; refusing rather than copying the wrong one. Pass document_id (the lineage "
                "URN above) to copy one exactly.")
        src = matches[0][0]

    # --- resolve the destination project + folder ---
    dproj, davail = _find_project(data, name=project or None, project_id=project_id or None)
    if not dproj:
        ident = project_id or project
        return error(f"Destination project not found: {ident}. Available: "
                      f"{', '.join(davail) or '(none)'}")
    try:
        root = dproj.rootFolder
    except Exception as e:
        return error(f"Could not access destination project root: {e}")

    target = root
    auto_created = []
    segments = _split_path(folder)
    if segments:
        if create_path:
            try:
                target, auto_created = _ensure_folder_path(root, segments)
            except Exception as e:
                return error(f"Could not prepare destination path '{folder}': {e}")
        else:
            target, missing = _resolve_folder_path(root, segments)
            if not target:
                opts = [safe(lambda: f.name) for f in safe(lambda: root.dataFolders.asArray(), [])]
                return error(
                    f"Destination folder path not found: '{folder}' (missing segment "
                    f"'{missing}'). Folders at project root: "
                    f"{', '.join(n for n in opts if n) or '(none)'}. "
                    "Pass create_path=true, or use data_get(include=['folders']) to see the structure.")

    src_name = safe(lambda: src.name) or "(unknown)"
    # The copied file's intended FINAL name: the requested 'name' if given, else the source's.
    # DataFile.copy() cannot set a name, so a requested rename is applied after the copy below.
    want_name = (name or "").strip()
    final_name = want_name or src_name

    # 'folder' always narrows the collision; 'name' only does on the document_id path, because on
    # the by-name path 'name' IS the source lookup, so a different one copies a different file.
    rename_remedy = (" or give the copy a different 'name'." if document_id else
                     ". On this call 'name' selects the SOURCE file, so changing it copies a "
                     "different document - pass 'document_id' (the source's lineage URN from "
                     "data_get) to free 'name' for the copy.")

    # Duplicate guard scoped to the destination folder, against the FINAL name (what will collide).
    existing_files, census_problem = _files_in_folder_by_name(target, final_name)
    if census_problem:
        return error(census_problem + " Copy was not attempted. Retry after the folder is readable "
                     "or copy into a different 'folder'." + _retained_parents(auto_created))
    if len(existing_files) > 1:
        return error(_same_name_refusal(target, final_name, existing_files)
                     + " Copy into a different 'folder'" + rename_remedy
                     + _retained_parents(auto_created))
    existing = existing_files[0] if existing_files else None
    if existing:
        return error(f"A file named '{final_name}' already exists in "
                      f"'{_folder_path_string(target) or '(project root)'}' "
                      f"(id {safe(lambda: existing.id)}). Copy into a different 'folder'"
                      + rename_remedy + _retained_parents(auto_created))

    xrefs, xref_count = _xref_summary(src)

    try:
        copied = src.copy(target)  # adsk.core: DataFile.copy(targetFolder) -> DataFile
    except Exception as e:
        return error(f"Copy failed for document '{src_name}': {e}"
                     + _retained_parents(auto_created))
    if not copied:
        return error(f"Copy returned nothing for document '{src_name}'."
                     + _retained_parents(auto_created))

    # Apply the requested rename. DataFile.copy() does not accept a name, so the copy lands with
    # the SOURCE's name; set it here (DataFile.name has a setter). Report if the rename fails so a
    # caller can't silently end up with a copy still named after the template.
    rename_error = None
    if want_name and (safe(lambda: copied.name) or "") != want_name:
        try:
            copied.name = want_name
        except Exception as e:
            rename_error = f"copy succeeded but rename to '{want_name}' failed: {e}"
        else:
            # The setter answering nothing is not the name landing: read it back off the copy.
            landed = safe(lambda: copied.name)
            if landed != want_name:
                rename_error = (f"copy succeeded but the rename to '{want_name}' did not take - the "
                                f"copy reads its name back as {landed!r}. Address it by 'copied_id'.")

    result = {
    "copied": True,
    "source_document": src_name,
    "source_id": safe(lambda: src.id),
    "requested_name": want_name or None,
    "copied_name": safe(lambda: copied.name),
    "copied_id": safe(lambda: copied.id),
    "destination_project": safe(lambda: dproj.name),
    "destination_folder": (_folder_path_string(target) or "(project root)"),
    "auto_created_parents": auto_created,
    "external_references": xrefs,
    # counted, never a fabricated 0: a reference read that did not answer is not "no references".
    "external_reference_count": xref_count,
    "note": ("The copy preserves external references: each referenced component still "
        "points at its ORIGINAL source file - the references are not re-copied. This tool "
        "does not offer a Document.saveAs-based copy mode that shares lineage for joint "
        "auto-repair."),
    }
    if xref_count is None:
        result["note"] += (" The source's child references could not be READ, so "
                           "external_reference_count is null (not zero) and 'external_references' is "
                           "empty for that reason, not because the source carries none.")
    if rename_error:
        result["rename_warning"] = rename_error
    return ok(result)


TOOL_DESCRIPTION = (
    "Copy a saved cloud document into a destination project/folder. Cloud-to-cloud: the active "
    "session is not touched."
)

tool = (
    Tool.create_simple(
        name="doc_copy",
        description=TOOL_DESCRIPTION,
    )
    # document_id is OPTIONAL, not required: the handler also accepts the by-name path
    # ('name' + 'source_project'). One of document_id / name must be given (guarded in the handler).
    .add_input_property("document_id", {"type": "string",
        "description": "Lineage URN from data_get."})
    .add_input_property("name", {"type": "string",
        "description": "Source name (needs source_project); the copy's name with document_id."})
    .add_input_property("source_project", {"type": "string"})
    .add_input_property("source_project_id", {"type": "string"})
    .add_input_property("source_folder", {"type": "string",
        "description": "Folder path scoping the 'name' lookup."})
    .add_input_property("project", {"type": "string", "description": "Destination project name."})
    .add_input_property("project_id", {"type": "string"})
    .add_input_property("folder", {"type": "string",
        "description": "Destination folder path, e.g. 'Parts/WidgetA'."})
    .add_input_property("create_path", {"type": "boolean",
        "description": "Create missing folders."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # copied_name/copied_id are read off the DataFile copy() handed back, and a requested 'name' is
    # read back after the set and disclosed when the copy does not carry it.
    verification=Verification(
        kind="effect",
        evidence_test="tests/unit/test_doc_copy.py::TestCopyDocument"
                      "::test_a_rename_that_silently_does_not_take_is_disclosed",
        rung="value")
)


def register_tool():
    register(item)
