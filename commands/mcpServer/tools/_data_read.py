# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Cloud data-model READ cores data_get delegates to: the project list, a project's file listing,
its folder tree, and ONE file's facts. Each file is read in its own try/except and folder recursion
is depth/count-capped, since these calls hit cloud data on the main thread."""

import datetime
import time

import adsk.core

from ._common import ok, error, safe
from ._data_common import (_data, _find_project, _folder_path_string, navigate_folder_path,
                           resolve_file_reference)

app = adsk.core.Application.get()

MAP_BLURB = ("the cloud READ cores data_get delegates to - list_projects_handler (the active "
             "hub's projects), list_project_files_handler (one project's files, folder-scoped), "
             "list_folders_handler (the bounded folder TREE, whole project or one folder) "
             "and file_facts_handler (ONE file's metadata + link state) - over _walk_folder and "
             "_folder_tree_bounded, the capped/deadlined walks RECORDING an unreadable folder")

# Every DataFile property read and every dataFolders/dataFiles enumeration is a synchronous cloud
# round-trip on Fusion's MAIN thread, so these caps bound a whole-project walk; a bigger project is
# read a folder at a time (folder=<path>), the next step data_get's 'truncated' note names.
_MAX_FILES = 200
_MAX_FOLDER_DEPTH = 25
# Each folder visit enumerates that folder's files AND subfolders, so a wide tree stalls a walk even
# when the file cap never trips.
_MAX_FOLDER_VISITS = 40

# WALL-CLOCK budget over the whole walk, on top of the count caps: one cloud round-trip can hang past
# normal latency on a network stall, which no count cap catches. Checked BETWEEN items (an in-flight
# call cannot be interrupted) and published as 'time_truncated', apart from an ordinary size cap.
_TIME_BUDGET_S = 20.0

# The ceiling a CALLER's own budget is clamped into (floor 1). A walk this long still blocks Fusion's
# main thread, so a caller sizing a read for absence buys more of it, not an unbounded one.
_TIME_BUDGET_MAX_S = 120.0


def _budget(value, default, ceiling):
    """A caller's budget as a float clamped into [1, ceiling]; anything that is not a number (NaN
    included) takes the default. The effective value rides in the payload."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(default)
    if v != v:
        return float(default)
    return max(1.0, min(v, float(ceiling)))


# ---------------------------------------------------------------------------
# data_get default scope: the active hub's projects
# ---------------------------------------------------------------------------

def list_projects_handler() -> dict:
    """Return the projects in the active hub: name + id."""
    data = app.data
    projects = []
    hub_name = None
    time_truncated = False
    try:
        if data.activeHub:
            hub_name = data.activeHub.name
    except Exception:
        pass

    deadline = time.monotonic() + _TIME_BUDGET_S
    try:
        for proj in data.dataProjects.asArray():
            if time.monotonic() > deadline:
                # Between items only - stop before reading the NEXT project, never mid-read.
                time_truncated = True
                break
            try:
                projects.append({"name": proj.name, "id": proj.id})
            except Exception:
                # Skip a project we can't read rather than failing the whole call.
                continue
    except Exception as e:
        return error(f"Could not list projects: {e}")

    payload = {"active_hub": hub_name, "project_count": len(projects), "projects": projects,
        "time_truncated": time_truncated}
    return ok(payload)


# ---------------------------------------------------------------------------
# data_get(project=...) scope: a project's files
# ---------------------------------------------------------------------------

def _folder_scope(root, folder):
    """(start folder, its cleaned path, None) for a 'folder' scope, else (None, None, error result).
    An unread sibling list and a genuine miss are DIFFERENT refusals; '' scopes to `root` itself."""
    want = (folder or "").strip().strip("/")
    if not want:
        return root, "", None
    start, path, miss = navigate_folder_path(root, want)
    if miss is None:
        return start, path, None
    if miss["available"] is None:
        # The sibling list did not enumerate: '(none)' here would report an unread folder
        # as an empty one, and 'not found' would be a verdict this walk never reached.
        return None, None, error(f"Folder '{folder}' could not be resolved: the subfolders of "
                                 f"'{miss['at']}' could not be read, so whether '{miss['segment']}' "
                                 "is there is unknown - nothing was listed. Retry, or scope with a "
                                 "folder path that opens.")
    return None, None, error(f"Folder '{folder}' not found: no subfolder '{miss['segment']}' in "
                             f"'{miss['at']}'. Subfolders there: "
                             f"{', '.join(miss['available']) or '(none)'}.")


def list_project_files_handler(project: str = "", project_id: str = "",
                               folder: str = "", recursive: bool = True,
                               time_budget_s=None) -> dict:
    """List a project's files (name, lineage id, versionId, fileExtension, versionNumber,
    fusionWebURL), optionally scoped to a folder path; 'recursive' controls descent into subfolders."""
    data = app.data

    if not (project or project_id):
        return error("Provide either 'project' (name) or 'project_id'.")

    try:
        target, available = _find_project(data, name=project or None, project_id=project_id or None)
    except Exception as e:
        return error(f"Could not access projects: {e}")

    if not target:
        ident = project_id or project
        return error(f"Project not found: {ident}. Available: {', '.join(available) or '(none)'}")

    files = []
    truncated = {"value": False}
    seconds = _budget(time_budget_s, _TIME_BUDGET_S, _TIME_BUDGET_MAX_S)
    deadline = time.monotonic() + seconds
    try:
        root = target.rootFolder
    except Exception as e:
        return error(f"Could not access root folder of project '{target.name}': {e}")

    # Scope to a sub-folder path if given (navigate there, then walk only it).
    want_folder = bool((folder or "").strip().strip("/"))
    start_folder, start_path, scope_err = _folder_scope(root, folder)
    if scope_err:
        return scope_err

    try:
        if want_folder and not recursive:
            # immediate files only - do not descend
            for f in start_folder.dataFiles.asArray():
                if len(files) >= _MAX_FILES:
                    truncated["value"] = True
                    break
                if time.monotonic() > deadline:
                    # Between items only - never mid dataFiles.asArray() call.
                    truncated["value"] = True
                    truncated["time_truncated"] = True
                    truncated["time_truncated_at"] = start_path or "(project root)"
                    break
                files.append(_file_summary(f, start_path))
        else:
            _walk_folder(start_folder, files, truncated, depth=0, folder_path=start_path,
                        deadline=deadline)
    except Exception as e:
        return error(f"Could not enumerate files in project '{target.name}': {e}")

    payload = {
    "project": {"name": target.name, "id": target.id},
    "folder": (start_path or "(project root)") if want_folder else "(whole project)",
    "recursive": bool(recursive) if want_folder else True,
    "file_count": len(files),
    "truncated": truncated["value"],
    "time_truncated": truncated.get("time_truncated", False),
    "time_budget_s": seconds,
    "files": files,
    }
    if truncated.get("time_truncated"):
        payload["time_truncated_at"] = truncated.get("time_truncated_at")
    if truncated.get("unread_count"):
        # Folders that would not enumerate: the listing is INCOMPLETE in a way the caps do not
        # describe, so 'files' is not evidence a file is absent from this project.
        payload["folders_unreadable"] = truncated["unread_count"]
        payload["folders_unreadable_at"] = truncated.get("unread", [])
    return ok(payload)


# How many unreadable folder paths the walk names before it just counts them - the flag and the
# count carry the rest.
_MAX_UNREAD_NAMED = 10


def _note_unread(truncated, folder_path):
    """Record a folder whose enumeration raised. The COUNT is complete; the named paths are capped."""
    truncated["unread_count"] = truncated.get("unread_count", 0) + 1
    named = truncated.setdefault("unread", [])
    path = folder_path or "(project root)"
    if path not in named and len(named) < _MAX_UNREAD_NAMED:
        named.append(path)


def _walk_folder(folder, files: list, truncated: dict, depth: int, folder_path: str, deadline=None):
    """Recursively collect files from a DataFolder into `files`, each stamped with `folder_path` ("" =
    project root). Bounded four ways, any of which sets truncated['value']: file count, depth, folder
    VISITS, and a wall-clock `deadline` (also truncated['time_truncated'/'time_truncated_at']). A
    folder whose enumeration RAISES is recorded in truncated['unread'], never silently skipped."""
    if depth > _MAX_FOLDER_DEPTH or len(files) >= _MAX_FILES:
        truncated["value"] = True
        return
    if deadline is not None and time.monotonic() > deadline:
        truncated["value"] = True
        truncated["time_truncated"] = True
        truncated["time_truncated_at"] = folder_path or "(project root)"
        return
    # Count this folder visit (enumerating its files + subfolders below = two round-trips); stop the
    # walk once the budget is spent rather than fan out across every folder on the main thread.
    truncated["visits"] = truncated.get("visits", 0) + 1
    if truncated["visits"] > _MAX_FOLDER_VISITS:
        truncated["value"] = True
        return

    # Files in this folder.
    try:
        for f in folder.dataFiles.asArray():
            if len(files) >= _MAX_FILES:
                truncated["value"] = True
                return
            if deadline is not None and time.monotonic() > deadline:
                truncated["value"] = True
                truncated["time_truncated"] = True
                truncated["time_truncated_at"] = folder_path or "(project root)"
                return
            files.append(_file_summary(f, folder_path))
    except Exception:
        _note_unread(truncated, folder_path)

    # Subfolders.
    try:
        for sub in folder.dataFolders.asArray():
            if len(files) >= _MAX_FILES:
                truncated["value"] = True
                return
            if deadline is not None and time.monotonic() > deadline:
                truncated["value"] = True
                truncated["time_truncated"] = True
                truncated["time_truncated_at"] = folder_path or "(project root)"
                return
            sub_name = None
            try:
                sub_name = sub.name
            except Exception:
                pass
            sub_path = (folder_path + "/" + sub_name) if (folder_path and sub_name) else (sub_name or folder_path)
            _walk_folder(sub, files, truncated, depth + 1, sub_path, deadline=deadline)
    except Exception:
        _note_unread(truncated, folder_path)


def _file_summary(f, folder_path: str = "") -> dict:
    """Best-effort summary of a single DataFile; each field guarded."""
    out = {"folder_path": folder_path or "(project root)"}
    for key, getter in (
        ("name", lambda: f.name),
        ("id", lambda: f.id),                      # lineage URN (stable across versions)
        ("versionId", lambda: f.versionId),        # versioned URN
        ("fileExtension", lambda: f.fileExtension),
        ("versionNumber", lambda: f.versionNumber),
        ("fusionWebURL", lambda: f.fusionWebURL),  # openable browser/Fusion-protocol URL
    ):
        try:
            out[key] = getter()
        except Exception:
            out[key] = None
    return out


# ---------------------------------------------------------------------------
# data_get(file=...) scope: ONE file's metadata + link state
# ---------------------------------------------------------------------------

def _epoch_iso(ts):
    """A DataFile date (UNIX epoch SECONDS, per the binding) as an ISO-8601 UTC string, or None.

    The raw integer is published beside it, so a caller that distrusts the conversion still has the
    measured value."""
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    return safe(lambda: datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _user_facts(user):
    """A DataFile's createdBy/lastUpdatedBy User flattened to the three fields it carries."""
    if user is None:
        return None
    return {"display_name": safe(lambda: user.displayName),
            "user_name": safe(lambda: user.userName),
            "email": safe(lambda: user.email)}


def _shared_link_facts(df):
    """The file's SharedLink state, READ ONLY. Reading it while the file is unshared is safe
    (is_shared false, an empty linkURL); SETTING isShared is what creates the share, and this server
    does not offer that - so nothing here writes. linkURL is reported only when shared, since the
    binding returns an empty string otherwise."""
    link = safe(lambda: df.sharedLink)
    if link is None:
        return {"readable": False}
    shared = safe(lambda: link.isShared)
    out = {"is_shared": shared,
           "is_download_allowed": safe(lambda: link.isDownloadAllowed),
           "is_password_required": safe(lambda: link.isPasswordRequired)}
    url = safe(lambda: link.linkURL)
    if shared and url:
        out["link_url"] = url
    return out


def _public_link_facts(df):
    """The file's public link, or 'not available' with the reason. Reading publicLink RAISES while
    the file is unshared, so the raised text is kept as the reason rather than sinking the read."""
    try:
        url = df.publicLink
    except Exception as e:
        return {"available": False, "reason": str(e).strip()[:160]}
    if isinstance(url, str) and url:
        return {"available": True, "url": url}
    return {"available": False, "reason": "the file reports an empty public link."}


def file_facts_handler(file: str = "", project: str = "", project_id: str = "",
                       folder: str = "") -> dict:
    """One cloud file's metadata + link state, resolved from a lineage URN or a name in a project."""
    df, meta, err = resolve_file_reference(file, project=project, project_id=project_id,
                                           folder=folder)
    if err:
        return error(err)

    name = safe(lambda: df.name)
    version = safe(lambda: df.versionNumber)
    latest = safe(lambda: df.latestVersionNumber)
    parent_folder = safe(lambda: df.parentFolder)
    parent_project = safe(lambda: df.parentProject)
    created = safe(lambda: df.dateCreated)
    modified = safe(lambda: df.dateModified)

    return ok({
        "matched_by": meta.get("matched_by"),
        "name_scope_truncated": bool(meta.get("scope_truncated")),
        # Folders whose enumeration RAISED while this name was resolved: a hole in the search space
        # the cap flag does not describe, so a unique match over one is not a settled unique match.
        "name_scope_folders_unreadable": meta.get("folders_unreadable", 0),
        "file": {
            "name": name,
            "id": safe(lambda: df.id),                     # lineage URN (stable across versions)
            "version_id": safe(lambda: df.versionId),
            "file_extension": safe(lambda: df.fileExtension),
            "description": safe(lambda: df.description),
            "fusion_web_url": safe(lambda: df.fusionWebURL),
        },
        "version": {
            "number": version,
            "latest_number": latest,
            "is_latest": (version == latest) if (version is not None and latest is not None) else None,
            "version_count": safe(lambda: df.versions.count),
            "is_milestone": safe(lambda: df.isMilestone),
        },
        "dates": {
            "created_unix": created, "created_iso": _epoch_iso(created),
            "modified_unix": modified, "modified_iso": _epoch_iso(modified),
        },
        "created_by": _user_facts(safe(lambda: df.createdBy)),
        "last_updated_by": _user_facts(safe(lambda: df.lastUpdatedBy)),
        "location": {
            "project": {"name": safe(lambda: parent_project.name),
                        "id": safe(lambda: parent_project.id)},
            "parent_folder": {"name": safe(lambda: parent_folder.name),
                              "path": _folder_path_string(parent_folder) or "(project root)"},
        },
        "state": {
            "is_read_only": safe(lambda: df.isReadOnly),
            "is_in_use": safe(lambda: df.isInUse),
            "is_complete": safe(lambda: df.isComplete),
        },
        "shared_link": _shared_link_facts(df),
        "public_link": _public_link_facts(df),
    })


# ---------------------------------------------------------------------------
# data_get(project=..., include=['folders']) core: the project's folder tree
# ---------------------------------------------------------------------------

_LF_MAX_DEPTH = 12

# The folder walk's HARD budget, counting every dataFolders fetch. Each fetch is a cloud round-trip
# on Fusion's MAIN thread (~0.45-0.8 s), so a bigger tree must be read shallow (max_depth) or a
# folder at a time (data_get(project, folder=<path>)).
_LF_FOLDER_BUDGET = 20

# The ceiling a caller's own fetch budget is clamped into (floor 1) - see _TIME_BUDGET_MAX_S.
_LF_FOLDER_BUDGET_MAX = 200


def list_folders_handler(project: str = "", project_id: str = "", max_depth: int = 4,
                         folder: str = "", folder_budget=None, time_budget_s=None) -> dict:
    """Return a folder tree (name, id, path) under a project, or under one folder path in it,
    bounded by depth, fetch budget and time budget - each published as used."""
    if not (project or project_id):
        return error("Provide 'project' (name) or 'project_id'.")
    try:
        data = _data()
    except Exception as e:
        return error(str(e))

    proj, available = _find_project(data, name=project or None, project_id=project_id or None)
    if not proj:
        ident = project_id or project
        return error(f"Project not found: {ident}. Available: {', '.join(available) or '(none)'}")

    try:
        depth = max(1, min(int(max_depth), _LF_MAX_DEPTH))
    except Exception:
        depth = 4

    budget = int(_budget(folder_budget, _LF_FOLDER_BUDGET, _LF_FOLDER_BUDGET_MAX))
    seconds = _budget(time_budget_s, _TIME_BUDGET_S, _TIME_BUDGET_MAX_S)

    try:
        root = proj.rootFolder
    except Exception as e:
        return error(f"Could not read folder tree: {e}")
    start, start_path, scope_err = _folder_scope(root, folder)
    if scope_err:
        return scope_err
    try:
        tree, count, truncated, time_truncated, unread = _folder_tree_bounded(
            start, depth, budget=budget, seconds=seconds, path=start_path)
    except Exception as e:
        return error(f"Could not read folder tree: {e}")

    payload = {"project": safe(lambda: proj.name), "folder": start_path or "(project root)",
        "max_depth": depth, "folder_budget": budget, "time_budget_s": seconds,
        "folder_count": count, "truncated": truncated, "time_truncated": time_truncated,
        "folders": tree}
    if unread.get("unread_count"):
        # Folders whose enumeration RAISED: what is under them was never read, which no cap flag
        # describes - so this tree is not evidence that a folder is absent.
        payload["folders_unreadable"] = unread["unread_count"]
        payload["folders_unreadable_at"] = unread.get("unread", [])
    return ok(payload)


def _folder_tree_bounded(root, max_depth, budget=None, seconds=None, path=""):
    """The nested folder tree under `root`, BREADTH-FIRST and bounded by a fetch budget and a time
    budget: (tree, node_count, truncated, time_truncated, unread). A node whose children were NOT
    fetched carries folders_truncated=true for a budget cut, children_unreadable=true when the fetch
    RAISED, children_unknown=true at the depth cap; `truncated` reports the budget cut only."""
    tree = []
    count = 0
    fetches = 0
    # `path` is where `root` sits in the PROJECT, so a scoped walk still prints the paths its own
    # 'folder' input takes back.
    queue = [(root, tree, None, path, 0)]  # (folder, children-list in the output, its node, path, depth)
    truncated = False
    time_truncated = False
    unread = {}
    budget = _LF_FOLDER_BUDGET if budget is None else budget
    deadline = time.monotonic() + (_TIME_BUDGET_S if seconds is None else seconds)
    while queue:
        folder, children_out, node, path, depth = queue.pop(0)
        stalled = time.monotonic() > deadline
        if fetches >= budget or stalled:
            # budget exhausted (fetch count OR time): this folder's children are NOT enumerated -
            # flag it, never guess.
            truncated = True
            if stalled:
                time_truncated = True
            if node is not None:
                node.pop("folders", None)
                node["folders_truncated"] = True
            continue
        fetches += 1
        try:
            children = folder.dataFolders.asArray()
        except Exception:
            # The fetch RAISED: what is under this folder was never read, which is a different
            # answer from the childless leaf a dropped node would read as.
            _note_unread(unread, path)
            if node is not None:
                node.pop("folders", None)
                node["children_unreadable"] = True
            continue
        for f in children:
            name = safe(lambda f=f: f.name)
            child_path = (path + "/" + name) if path else name
            child = {"name": name, "id": safe(lambda f=f: f.id), "path": child_path}
            count += 1
            children_out.append(child)
            if depth + 1 < max_depth:
                kids = []
                child["folders"] = kids
                queue.append((f, kids, child, child_path, depth + 1))
            else:
                # depth cap: whether this folder has subfolders is UNKNOWN (checking costs a
                # round-trip) - say so instead of implying 'none'.
                child["children_unknown"] = True
    _prune_empty_folder_lists(tree)
    return tree, count, truncated, time_truncated, unread


def _prune_empty_folder_lists(nodes):
    """Drop empty 'folders' lists so a childless folder reads as a leaf, not an empty expansion."""
    for n in nodes:
        kids = n.get("folders")
        if kids:
            _prune_empty_folder_lists(kids)
        elif kids is not None:
            del n["folders"]


# The four handlers above are the project, file-list, folder-tree and single-file read cores
# data_get delegates to (data_get is the registered rich read; these carry the cloud-error guards
# and caps). No register_tool() here - this module exposes cores, not tools.
