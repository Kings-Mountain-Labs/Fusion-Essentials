# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP RICH READ: data_get - the CLOUD data model (hub -> projects -> folders -> files) in one read.

Every call is a NETWORK round-trip (slow, can fail offline / signed-out / with no active hub) -
deliberately separate from doc_get, which reads the in-memory session. Delegates to the
_data_read cores and the data_switch_hub handler so the cloud-error guards and caps live in one place.
"""

import json

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error

_SLICES = ("hubs", "folders")

# Folder-tree depth a caller who names none gets (each level is a slow cloud fetch).
_MAX_DEPTH_DEFAULT = 4


def _walk_limits():
    """The folder walk's default and maximum budgets, read off the core that enforces them."""
    from . import _data_read as data_read
    return (data_read._LF_FOLDER_BUDGET, data_read._LF_FOLDER_BUDGET_MAX,
            int(data_read._TIME_BUDGET_S), int(data_read._TIME_BUDGET_MAX_S))


_FOLDER_BUDGET_DEFAULT, _FOLDER_BUDGET_MAX, _TIME_BUDGET_DEFAULT, _TIME_BUDGET_MAX = _walk_limits()


def _unwrap(result):
    """(payload, None) on ok; (None, error_result) on error - so a cloud failure propagates verbatim."""
    if result.get("isError"):
        return None, result
    try:
        return json.loads(result["content"][0]["text"]), None
    except Exception:
        return None, result


def _normalize_include(include):
    if include in (None, "", []):
        return []
    if isinstance(include, str):
        return [s.strip().lower() for s in include.split(",") if s.strip()]
    return [str(s).strip().lower() for s in include]


def handler(project: str = "", project_id: str = "", folder: str = "", recursive: bool = True,
            include=None, max_depth: int = _MAX_DEPTH_DEFAULT, file: str = "",
            folder_budget=None, time_budget_s=None) -> dict:
    """See TOOL_DESCRIPTION."""
    inc = _normalize_include(include)
    bad = [s for s in inc if s not in _SLICES]
    if bad:
        return error(f"Unknown include {bad}. Valid: {', '.join(_SLICES)}.")

    have_project = bool(project or project_id)

    # ── scoped to ONE file ───────────────────────────────────────────────────
    if (file or "").strip():
        if inc:
            return error(f"include={inc} does not apply to the 'file' scope (it reads one file's "
                         "record in full). Drop 'file' to use include, or drop include.")
        from . import _data_read as data_read
        out, e = _unwrap(data_read.file_facts_handler(file=file, project=project,
                                                      project_id=project_id, folder=folder))
        if e:
            return e
        out["scope"] = "file"
        out["note"] = (
            "One file's record: metadata, version state and LINK state. Dates are UNIX epoch seconds "
            "with the UTC ISO string beside each. 'file_extension' is unreliable for a non-CAD "
            "upload - the file NAME carries the true extension. Link state is read-only here, so an "
            "unshared file reports public_link.available=false. Next: data_download_file (a design "
            "leaves through design_export), data_move_file, doc_open."
        )
        if out.get("name_scope_truncated"):
            out["note"] += (" The name was matched inside a CAPPED listing - files beyond the cap "
                            "were never compared, so another file there could share this name. Pass "
                            "the lineage URN (or a 'folder') to be exact.")
        if out.get("name_scope_folders_unreadable"):
            out["note"] += (f" {out['name_scope_folders_unreadable']} folder(s) could not be READ "
                            "while resolving that name, so they were never searched - a file of the "
                            "same name could be sitting in one, which would make this match the "
                            "wrong file. Pass the lineage URN to be exact.")
        return ok(out)

    # ── scoped to a project ──────────────────────────────────────────────────
    if have_project:
        from . import _data_read as data_read
        if "folders" in inc:
            out, e = _unwrap(data_read.list_folders_handler(
                project=project, project_id=project_id, max_depth=max_depth, folder=folder,
                folder_budget=folder_budget, time_budget_s=time_budget_s))
            if e:
                return e
            out["scope"] = "folders"
            out["note"] = ("Folder tree under 'folder' (the whole project when none is given). Drop "
                           "include=['folders'] to list a folder's FILES instead.")
            if out.get("truncated"):
                out["note"] += (" The walk hit its folder budget (each folder is a slow cloud fetch "
                                "on Fusion's main thread): nodes flagged folders_truncated were not "
                                "descended. Scope with 'folder'=<path>, lower max_depth, or raise "
                                "folder_budget.")
            if out.get("time_truncated"):
                spent = out.get("time_budget_s")
                out["note"] += ((f" The walk stopped after its {int(spent)}s time budget" if spent
                                 else " The walk stopped after its time budget")
                                + " (a network stall, not the fetch-count cap) - results are "
                                  "PARTIAL. Retry, scope with 'folder'=<path>, or raise "
                                  "time_budget_s.")
            if out.get("folders_unreadable"):
                out["note"] += (f" {out['folders_unreadable']} folder(s) would not enumerate at all "
                                "- the first of them named in folders_unreadable_at, and flagged "
                                "children_unreadable wherever the tree holds their node: what is "
                                "under them was never read, so this tree does not show that a "
                                "folder is absent.")
            return ok(out)
        out, e = _unwrap(data_read.list_project_files_handler(project=project, project_id=project_id,
                                                              folder=folder, recursive=recursive,
                                                              time_budget_s=time_budget_s))
        if e:
            return e
        out["scope"] = "files"
        out["note"] = ("Files in the project (each with its lineage URN + openable fusionWebURL). "
                       "'folder'=<path> scopes to one folder; include=['folders'] shows the folder tree "
                       "instead; 'file'=<name|URN> reads ONE file's full record (dates, authors, "
                       "version and link state). (Cloud read - see 'truncated'.)")
        if out.get("time_truncated"):
            at = out.get("time_truncated_at") or "(project root)"
            spent = out.get("time_budget_s")
            out["note"] += ((f" The walk stopped after its {int(spent)}s time budget" if spent
                             else " The walk stopped after its time budget")
                            + f" at folder '{at}' (a network stall, not the file/folder cap) - "
                              "results are PARTIAL. Narrow with 'folder'=<path>, raise "
                              "time_budget_s, or retry.")
        # a file listing's dominant next action is to OPEN one - name doc_open so the breadcrumb from
        # 'here are the files' to 'open this one by id' is explicit (present-only: only when files exist).
        if out.get("files"):
            out["pointers"] = {"open": "doc_open(file_id=<a file's 'id'>, force_api_open=true) to open one."}
        return ok(out)

    # ── no project: orient at the hub level ──────────────────────────────────
    if "hubs" in inc:
        from . import data_switch_hub
        out, e = _unwrap(data_switch_hub.handler(action="list"))
        if e:
            return e
        out["scope"] = "hubs"
        out["note"] = ("All hubs (is_active flags the current one). Switch with data_switch_hub - it "
                       "CLOSES every open document. Then pass project=<name> to list files.")
        return ok(out)

    from . import _data_read as data_read
    out, e = _unwrap(data_read.list_projects_handler())
    if e:
        return e
    out["scope"] = "projects"
    out["note"] = ("Active hub + its projects. Pass project=<name|id> to list its FILES (add 'folder' to "
                   "scope, or include=['folders'] for the tree); 'file'=<name|URN> reads ONE file's full "
                   "record. include=['hubs'] lists all hubs. This is the CLOUD data model (networked); "
                   "for the open-document SESSION see doc_get.")
    if out.get("time_truncated"):
        out["note"] += (f" The listing stopped after its {int(data_read._TIME_BUDGET_S)}s time budget "
                        "(a network stall, not a size cap) - results are PARTIAL. Retry.")
    return ok(out)


TOOL_DESCRIPTION = (
    "Read the CLOUD data model by scope: hubs and projects, one project's files, its folder tree, "
    "or ONE file's record. Open documents are doc_get."
)

tool = (
    Tool.create_simple(name="data_get", description=TOOL_DESCRIPTION)
    .add_input_property("project", {"type": "string"})
    .add_input_property("project_id", {"type": "string"})
    .add_input_property("folder", {"type": "string",
            "description": "Folder PATH, e.g. 'Parts/Fixtures'."})
    .add_input_property("recursive", {"type": "boolean"})
    .add_input_property("include", {"type": "array",
            "items": {"type": "string", "enum": list(_SLICES)},
            "description": "'folders' reads the folder tree."})
    .add_input_property("max_depth", {"type": "integer",
            "description": "Depth of the folder tree."})
    .add_input_property("folder_budget", {"type": "integer",
            "description": "Folder fetches the walk may spend (default "
                           f"{_FOLDER_BUDGET_DEFAULT}, max {_FOLDER_BUDGET_MAX})."})
    .add_input_property("time_budget_s", {"type": "number",
            "description": f"Seconds the walk may spend (default {_TIME_BUDGET_DEFAULT}, "
                           f"max {_TIME_BUDGET_MAX})."})
    .add_input_property("file", {"type": "string",
            "description": "Lineage URN, web URL, or a name plus 'project'."})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
