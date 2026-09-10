# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Shared helpers for the cloud data-model tools: hub/project/folder resolution, path splitting,
URN/web-URL identifier decoding, the one file reference resolver (URN or name-in-a-project), and
the upload registry data_upload_file mints a poll handle into.
"""

import base64
import re
import time

import adsk.core

from ._common import safe

MAP_BLURB = (
    "the cloud data-model substrate: resolve_file_reference / _file_in_folder_by_name - the "
    "URN-or-name-in-a-project and one-folder resolvers, a shared name REFUSED; "
    "navigate_folder_path - the folder-PATH walk from a project root, creating nothing, and the "
    "miss triple a refusal is worded from; active_project - the active document's OWN DataProject; "
    "name_extension - the NAME holds the true extension")

app = adsk.core.Application.get()

_UNREAD = object()      # a collection read that RAISED - distinct from one that came back empty

# Document.save/saveAs has no author field, so the version description carries the attribution every
# save made through this server needs. _agent_description() is the one chokepoint that writes it.
AI_AGENT_SAVE_MARKER = "[AI agent]"


def _agent_description(description: str = "") -> str:
    """Prefix a version description with the AI-agent marker (idempotent)."""
    desc = (description or "").strip()
    if desc.startswith(AI_AGENT_SAVE_MARKER):
        return desc
    return f"{AI_AGENT_SAVE_MARKER} {desc}".strip()


def _data():
    d = app.data
    if not d:
        raise RuntimeError("Data not available (not signed in?).")
    return d


def _find_project(data, name=None, project_id=None):
    """Find a project by id or (case-insensitive) name. Returns (project, available_names)."""
    available = []
    for p in data.dataProjects.asArray():
        nm = None
        try:
            nm = p.name
        except Exception:
            pass
        if nm:
            available.append(nm)
        try:
            if project_id:
                if p.id == project_id:
                    return p, available
            elif name and nm and nm.strip().lower() == name.strip().lower():
                return p, available
        except Exception:
            continue
    return None, available


def active_project():
    """(project, problem): the DataProject the ACTIVE document's own DataFile hands back, and the
    read that failed when it does not - each read taken apart so the problem names one."""
    # Data.activeProject raises InternalValidationError on this build; the document's own DataFile
    # answers the project object itself, which is what a same-named sibling cannot be mistaken for.
    doc = safe(lambda: app.activeDocument)
    if doc is None:
        return None, "there is no active document to read a project from."
    df = safe(lambda: doc.dataFile)
    if df is None:
        return None, ("the active document has no cloud data file, so it is in no project yet - "
                      "save it with doc_save_as first.")
    proj = safe(lambda: df.parentProject)
    if proj is None:
        return None, ("the active document's data file did not answer a parentProject - retry, or "
                      "address the file by its lineage urn instead.")
    return proj, None


def _split_path(path):
    """Split a folder path into clean segments, tolerant of / or \\ and stray slashes."""
    if not path:
        return []
    norm = path.replace("\\", "/")
    return [seg.strip() for seg in norm.split("/") if seg.strip()]


def _child_folder_by_name(folder, name):
    """The immediate child folder matching name (case-insensitive), or None. First match is CORRECT
    here: folder names are UNIQUE within a container - dataFolders.add() with a name a sibling
    carries raises 'CB_NAE - Another object with the same name already exists in this container'.
    A FILE name carries no such rule (_file_in_folder_by_name below refuses that)."""
    want = (name or "").strip().lower()
    try:
        for f in folder.dataFolders.asArray():
            if (safe(lambda: f.name) or "").lower() == want:
                return f
    except Exception:
        pass
    return None


def _resolve_folder_path(root, segments):
    """Walk an existing folder path from `root`. Returns (folder, None) or (None, missing_segment).
    Does NOT create anything. Empty `segments` resolves to `root` itself."""
    cur = root
    for seg in segments:
        nxt = _child_folder_by_name(cur, seg)
        if not nxt:
            return None, seg
        cur = nxt
    return cur, None


def navigate_folder_path(root, path):
    """Walk a raw folder PATH string from `root`, creating nothing: (folder, cleaned path, None) on
    success, else (None, None, {'segment', 'at', 'available'}) for the caller to word its own
    refusal from. available=None and available=[] are DIFFERENT answers - None is an enumeration
    that RAISED (the segment may be there), [] a folder that is genuinely childless."""
    cur, cur_path = root, ""
    for seg in _split_path(path):
        nxt = _child_folder_by_name(cur, seg)
        if nxt is None:
            # _child_folder_by_name answers None for BOTH 'no such child' and 'the enumeration
            # raised', so the sibling read is retaken here with a sentinel to tell them apart.
            folders = safe(lambda: cur.dataFolders.asArray(), _UNREAD)
            names = (None if folders is _UNREAD
                     else [n for n in (safe(lambda f=f: f.name) for f in folders) if n])
            return None, None, {"segment": seg, "at": cur_path or "(project root)",
                                "available": names}
        # The folder's OWN name, not the segment as typed - the match is case-insensitive.
        cur_path = f"{cur_path}/{safe(lambda n=nxt: n.name) or seg}" if cur_path else (
            safe(lambda n=nxt: n.name) or seg)
        cur = nxt
    return cur, cur_path, None


def _ensure_folder_path(root, segments, created_out=None):
    """Walk a folder path from `root`, creating any missing segments (mkdir -p): (deepest_folder,
    created_names) or raises. Pass `created_out` (a list the caller owns) to receive each name AS it
    is created - the return value is lost when a later segment raises, and the folders already made
    are real mutations the caller has to disclose."""
    cur = root
    created = created_out if created_out is not None else []
    for seg in segments:
        nxt = _child_folder_by_name(cur, seg)
        if not nxt:
            nxt = cur.dataFolders.add(seg)
            if not nxt:
                raise RuntimeError(f"Failed to create folder segment '{seg}'.")
            created.append(seg)
        cur = nxt
    return cur, created


def _retained_parents(auto_created):
    """The clause an ERROR appends when mkdir -p already created folders before the call failed -
    real, kept mutations the error is the only place the caller hears about (auto_created_parents
    ships on the ok path only)."""
    if not auto_created:
        return ""
    return (" This call had already created the folder(s) "
            + ", ".join(f"'{n}'" for n in auto_created)
            + " on the way there, and they were NOT removed - delete them with data_delete_folder "
              "if they were not wanted.")


# How many child/parent reference rows a DataFile disclosure spells out.
_MAX_XREFS = 64

# The registry that makes an upload POLLABLE: it keeps the live DataFileFuture referenced, so a
# data_get_upload_status call after the upload handler returns can still read the state. An entry
# is popped once its terminal state has been reported once.
_UPLOADS = {}
_UPLOAD_HANDLE_SEQ = [0]

_UPLOAD_STATE = {0: "processing", 1: "finished", 2: "failed"}


def _files_in_folder_by_name(folder, name):
    """Return (readable exact-name children, incomplete-census problem or None)."""
    want = (name or "").strip().lower()
    out = []
    try:
        files = folder.dataFiles.asArray()
        iterator = iter(files)
    except Exception as exc:
        where = _folder_path_string(folder) or "(project root)"
        return out, (f"Could not completely read files in '{where}': its child-file listing failed "
                     f"({str(exc)[:160]}).")

    unread_names = 0
    while True:
        try:
            f = next(iterator)
        except StopIteration:
            break
        except Exception as exc:
            where = _folder_path_string(folder) or "(project root)"
            known = f" Known matching lineage URNs: {_same_name_rows(out)}." if out else ""
            return out, (f"Could not completely read files in '{where}': iterating its child-file "
                         f"listing failed ({str(exc)[:160]}).{known}")
        try:
            raw_name = f.name
        except Exception:
            unread_names += 1
            continue
        if not isinstance(raw_name, str) or not raw_name.strip():
            unread_names += 1
            continue
        if raw_name.strip().lower() == want:
            out.append(f)
    if unread_names:
        where = _folder_path_string(folder) or "(project root)"
        known = f" Known matching lineage URNs: {_same_name_rows(out)}." if out else ""
        return out, (f"Could not completely read files in '{where}': {unread_names} child file "
                     f"name(s) were unreadable.{known}")
    return out, None


def _same_name_rows(matches):
    """Every candidate's lineage URN, one row per file, an id that will not READ named as such - the
    rendering every same-name disclosure lists its candidates with. One row per file, always: a
    dropped row shows N files under fewer URNs, which reads as though two of them shared one."""
    return "; ".join(safe(lambda f=f: f.id) or "(id unreadable)" for f in matches)


def _same_name_refusal(folder, name, matches):
    """The refusal for a folder already holding SEVERAL files of one name: the count, the folder,
    and every candidate's lineage URN - the thing that IS an identity here. Each caller ENDS it with
    the remedy its own inputs offer."""
    rows = _same_name_rows(matches)
    return (f"'{name}' names {len(matches)} files in "
            f"'{_folder_path_string(folder) or '(project root)'}' - refusing to guess which. A "
            f"folder can hold several files of one name, and the lineage URN is what tells them "
            f"apart: {rows}.")


def _file_in_folder_by_name(folder, name):
    """The ONE immediate child DataFile of `folder` carrying `name`: (file, None) for exactly one
    match, (None, None) when nothing carries it, (None, sentence) when several do - never one of
    several, since those are different lineages. The caller appends its own remedy."""
    matches, problem = _files_in_folder_by_name(folder, name)
    if problem:
        return None, problem
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, _same_name_refusal(folder, name, matches)
    return None, None


def _folder_path_string(folder):
    """Build a human-readable path for a folder by walking parentFolder up to root."""
    parts = []
    cur = folder
    seen = 0
    try:
        while cur and seen < 64:
            seen += 1
            if safe(lambda: cur.isRoot, False):
                break
            nm = safe(lambda: cur.name)
            if nm:
                parts.append(nm)
            cur = safe(lambda: cur.parentFolder)
            if not cur:
                break
    except Exception:
        pass
    return "/".join(reversed(parts))


def _b64url_decode(segment):
    """Decode a base64url path segment to text, or None if it isn't valid base64url."""
    s = segment.replace('-', '+').replace('_', '/')
    s += '=' * (-len(s) % 4)  # restore padding
    try:
        return base64.b64decode(s).decode('utf-8', 'strict')
    except Exception:
        return None


def _urn_candidates(raw):
    """The URN candidates to try for a raw identifier, the raw value first. In a Fusion web URL the
    lineage URN is a base64url-encoded path segment, so each segment is decoded and kept when it
    decodes to a 'urn:adsk...' string."""
    raw = (raw or "").strip()
    seen = []

    def add(c):
        if c and c not in seen:
            seen.append(c)

    # 1) The value as given (covers a plain URN, possibly with a ?version=... suffix).
    add(raw)

    # 2) If it's a URL, decode each path segment and keep decoded 'urn:adsk...' strings.
    if '://' in raw or raw.lower().startswith('http'):
        for seg in re.split(r'[/?#&=]+', raw):
            if len(seg) < 16:
                continue
            decoded = _b64url_decode(seg)
            if decoded and decoded.startswith('urn:adsk'):
                add(decoded)

    # 3) As a last resort, pull any inline 'urn:adsk...' substring out of the raw text.
    for m in re.findall(r'urn:adsk[\w\.\:\-]+', raw):
        add(m)

    return seen


def _resolve_data_file(raw):
    """Resolve a raw identifier (URN or web URL) to (DataFile, resolved_urn, candidates_tried)."""
    candidates = _urn_candidates(raw)
    for cand in candidates:
        df = safe(lambda c=cand: app.data.findFileById(c))
        if df:
            return df, cand, candidates
    return None, None, candidates


# DataFile.download handles only non-Fusion data and FAILS for these: a design leaves through
# design_export, a drawing through drawing_export.
FUSION_NATIVE_EXTENSIONS = frozenset({"f3d", "f2d", "f3z"})

# How many sibling names an ambiguity/miss error lists before it says "and N more".
_NAME_HINT_LIMIT = 12


def name_extension(name):
    """The extension a DataFile's NAME carries, lowercased ('' when it carries none). The name is the
    trustworthy source: DataFile.fileExtension reads WRONG for a non-CAD upload (an uploaded .txt
    read 'sql' while its name stayed 'probe_note.txt')."""
    base = (name or "").strip()
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _looks_like_identifier(raw):
    """True when `raw` is a URN or a Fusion web URL rather than a file NAME. PREFIX-only: a file NAME
    may legally start with 'http' ('httpd-mount.f3d') or carry '://' anywhere in it, and routing such
    a name down the URN path loses the project-scoped name lookup it needed - the miss then reads as
    'no cloud file resolves from ...' instead of listing the project's names."""
    low = (raw or "").strip().lower()
    return low.startswith("urn:") or low.startswith("http://") or low.startswith("https://")


def _name_hint(names):
    """A bounded, readable rendering of the names available at the point of a miss."""
    kept = [n for n in names if n][:_NAME_HINT_LIMIT]
    if not kept:
        return "(none)"
    more = len(names) - len(kept)
    return ", ".join(kept) + (f", and {more} more" if more > 0 else "")


def resolve_file_reference(raw, project="", project_id="", folder=""):
    """Resolve ONE DataFile from `raw` - a lineage URN / Fusion web URL, or a file NAME scoped to a
    project (optionally a folder path in it): (data_file, meta, err), exactly one of data_file/err
    set. A NAME is not unique across a project's folders, so a name matching several files is
    REFUSED with each candidate's folder path and URN; matching is case-insensitive EXACT."""
    from . import _data_read              # deferred: _data_read imports this module

    ident = (raw or "").strip()
    if not ident:
        return None, None, ("Provide 'file' - a lineage URN (or Fusion web URL), or a file NAME plus "
                            "the 'project' it lives in.")

    if _looks_like_identifier(ident):
        df, resolved, tried = _resolve_data_file(ident)
        if not df:
            return None, None, (f"No cloud file resolves from '{ident}'. Tried: {', '.join(tried)}. "
                                "Get a lineage URN from data_get(project=<name>) ('id' on each file).")
        return df, {"matched_by": "urn", "urn": resolved,
                    "folder_path": _folder_path_string(safe(lambda: df.parentFolder))}, None

    if not (project or project_id):
        return None, None, (f"'{ident}' is a file NAME, which is only unique within a project - pass "
                            "'project' (or 'project_id') to scope it, or pass the file's lineage URN "
                            "instead (data_get(project=<name>) lists both).")

    data = safe(lambda: app.data)
    if not data:
        return None, None, "Data not available (not signed in?)."
    proj, available = _find_project(data, name=project or None, project_id=project_id or None)
    if not proj:
        return None, None, (f"Project not found: {project_id or project}. Available: "
                            f"{', '.join(available) or '(none)'}")
    root = safe(lambda: proj.rootFolder)
    if root is None:
        return None, None, f"Could not access the root folder of project '{safe(lambda: proj.name)}'."

    start, start_path, miss = navigate_folder_path(root, folder)
    if miss:
        # An UNREAD sibling list makes 'not found' an overclaim - the segment may be sitting in a
        # folder listing that never opened, so the refusal says which of the two happened.
        why = (" - the subfolders of that folder could not be READ, so whether the segment is "
               "there is unknown" if miss["available"] is None else "")
        return None, None, (f"Folder '{folder}' not resolved in project "
                            f"'{safe(lambda: proj.name)}' (missing segment '{miss['segment']}' in "
                            f"'{miss['at']}'{why}). See data_get(project=<name>, "
                            "include=['folders']).")

    # ONE traversal: the same capped walk data_get's file listing uses (_data_read._walk_folder) -
    # this resolver only differs in its leaf op, matching a name over the summaries it collects.
    files, truncated = [], {"value": False}
    _data_read._walk_folder(start, files, truncated, depth=0, folder_path=start_path,
                            deadline=time.monotonic() + _data_read._TIME_BUDGET_S)

    want = ident.lower()
    matches = [f for f in files if (f.get("name") or "").strip().lower() == want]
    scope = f"project '{safe(lambda: proj.name)}'" + (f", folder '{start_path}'" if start_path else "")
    capped = (" The listing hit its cap, so files beyond it were not searched - scope with 'folder'."
              if truncated.get("value") else "")
    # A folder that would not enumerate is a hole in the search space, not an empty folder: a second
    # file of this name could be sitting in it, so neither a miss nor a UNIQUE match may be reported
    # as settled without saying so.
    if truncated.get("unread_count"):
        capped += (f" {truncated['unread_count']} folder(s) could not be read and were not searched"
                   + (f" ({', '.join(truncated.get('unread', []))})" if truncated.get("unread") else "")
                   + " - pass the file's id if this answer looks wrong.")

    if not matches:
        return None, None, (f"No file named '{ident}' in {scope}. Files there: "
                            f"{_name_hint([f.get('name') for f in files])}.{capped}")
    if len(matches) > 1:
        rows = "; ".join(f"{m.get('folder_path')} (id {m.get('id')})" for m in matches)
        return None, None, (f"'{ident}' names {len(matches)} files in {scope} - refusing to guess "
                            f"which: {rows}. Pass one of those ids as 'file', or scope with "
                            f"'folder'.{capped}")

    hit = matches[0]
    df, resolved, tried = _resolve_data_file(hit.get("id") or "")
    if not df:
        return None, None, (f"'{ident}' resolved to id {hit.get('id')} in {scope}, but that id does "
                            f"not open as a cloud file. Tried: {', '.join(tried)}.")
    # One match inside a CAPPED listing is not proof of uniqueness - files past the cap were never
    # compared. The flag travels with the result so every caller can say so instead of implying it.
    return df, {"matched_by": "name", "urn": resolved, "folder_path": hit.get("folder_path"),
                "scope_truncated": bool(truncated.get("value")),
                "folders_unreadable": truncated.get("unread_count", 0)}, None
