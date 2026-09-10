# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The material/appearance catalog walk behind design_get's 'materials' and 'appearances' slices."""

import adsk.core

from ._common import counted, error, iter_collection, read_flag, safe

app = adsk.core.Application.get()

MAP_BLURB = ("browse - the ONE material/appearance catalog read: a library census plus the "
             "document-local set when no library is named, one library's filtered and capped "
             "entries when it is; catalog_census/find_library/entries are its leaf ops")

# A Design and a MaterialLibrary both carry .materials and .appearances, so one walk serves both.
KINDS = ("materials", "appearances")

DEFAULT_CAP = 50
MAX_CAP = 200

_CONSUMERS = {
    "materials": "Pass name as material, plus library and material_id, to model_set_material.",
    "appearances": "A document appearance name feeds design_configure(action='set_appearance').",
}


def collection(owner, kind):
    """The Materials/Appearances collection of `kind` on a Design or a MaterialLibrary."""
    return safe(lambda: getattr(owner, kind))


def libraries():
    """Every loaded material library."""
    return list(iter_collection(safe(lambda: app.materialLibraries)))


def catalog_census():
    """(rows, readable) - one row per loaded library: name, id, is_native, both entry counts.
    A count that will not read is null, never 0; `readable` is False when the collection itself
    would not read."""
    coll = safe(lambda: app.materialLibraries)
    rows = []
    for lib in iter_collection(coll):
        rows.append({
            "name": safe(lambda l=lib: l.name),
            "id": safe(lambda l=lib: l.id),
            "is_native": read_flag(lambda l=lib: l.isNative),
            "material_count": counted(lambda l=lib: l.materials.count),
            "appearance_count": counted(lambda l=lib: l.appearances.count),
        })
    return rows, coll is not None


def find_library(name):
    """Resolve a library by exact id or case-insensitive name, refusing multiple matches."""
    raw = (name or "").strip()
    want = raw.lower()
    hits, ids, names = [], [], []
    for lib in libraries():
        nm = safe(lambda l=lib: l.name)
        if safe(lambda l=lib: l.id) == raw:
            ids.append(lib)
        if not nm:
            continue
        names.append(nm)
        if nm.lower() == want:
            hits.append(lib)
    hits = ids or hits
    if len(hits) == 1:
        return hits[0], None
    listed = ", ".join(f"'{n}'" for n in names) or "none"
    if not hits:
        # A None collection is an unreadable catalog, not an empty one.
        if safe(lambda: app.materialLibraries) is None:
            return None, ("The material-library collection could not be read, so whether a library "
                          f"named '{name}' is loaded is UNKNOWN - this is NOT a report that none "
                          "are loaded. Retry, or read the catalog without 'library'.")
        return None, f"No loaded material library named '{name}'. Loaded libraries: {listed}."
    return None, (f"'{name}' is the name of {len(hits)} loaded libraries - refusing to pick one. "
                  f"Loaded libraries: {listed}. Read them without 'library' to see each one's id.")


def _row(obj, name, scope, with_usage):
    """One catalog row: name, the source-asset 'id', scope, and is_used when asked."""
    row = {"name": name, "id": safe(lambda: obj.id), "scope": scope}
    if with_usage:
        row["is_used"] = read_flag(lambda: obj.isUsed)
    return row


def entries(coll, scope, name_filter="", cap=DEFAULT_CAP, with_usage=False):
    """(rows, matched, truncated) from ONE collection, name-substring narrowed and capped -
    `matched` counts every match, not the page."""
    wl = (name_filter or "").strip().lower()
    rows, matched = [], 0
    for obj in iter_collection(coll):
        nm = safe(lambda o=obj: o.name)
        if not nm:
            continue
        if wl and wl not in nm.lower():
            continue
        matched += 1
        if len(rows) < cap:
            rows.append(_row(obj, nm, scope, with_usage))
    return rows, matched, matched > len(rows)


def clamp_cap(max_results):
    """The row cap for one page: absent/unparseable/non-positive -> DEFAULT_CAP, else min(n, MAX_CAP)."""
    try:
        n = int(max_results)
    except (TypeError, ValueError):
        return DEFAULT_CAP
    return min(n, MAX_CAP) if n > 0 else DEFAULT_CAP


def browse(design, kind, library="", name_filter="", max_results=0):
    """(payload, error_result_or_None): without 'library' the document entries plus a library
    census, with it that library's narrowed and capped entries."""
    if kind not in KINDS:
        return None, error(f"Unknown catalog kind '{kind}'. Use one of: {', '.join(KINDS)}.")
    cap = clamp_cap(max_results)
    want_lib = (library or "").strip()

    if want_lib:
        lib, lerr = find_library(want_lib)
        if lerr:
            return None, error(lerr)
        lib_name = safe(lambda: lib.name) or want_lib
        coll = collection(lib, kind)
        rows, matched, truncated = entries(coll, lib_name, name_filter, cap)
        payload = {"kind": kind, "library": lib_name, "library_id": safe(lambda: lib.id),
                   "readable": coll is not None,
                   "count": matched, "returned": len(rows), "entries": rows}
        note = [f"{len(rows)} of {matched} {kind} in '{lib_name}'."]
        if coll is None:
            note.append(f"The library's {kind} collection could not be read (readable=false), so "
                        "count 0 here means UNKNOWN, not empty.")
        if truncated:
            payload["truncated"] = True
            note.append(f"Narrow with name_filter= or raise max_results= (cap {MAX_CAP}).")
        note.append("Names repeat within one library - 'id' tells two same-named entries apart.")
        note.append(_CONSUMERS[kind])
        payload["note"] = " ".join(note)
        return payload, None

    doc_coll = collection(design, kind)
    doc_rows, doc_matched, doc_truncated = entries(doc_coll, "document", name_filter, cap, True)
    doc = {"readable": doc_coll is not None,
           "count": doc_matched, "returned": len(doc_rows), "entries": doc_rows}
    if doc_truncated:
        doc["truncated"] = True
    lib_rows, libs_readable = catalog_census()
    payload = {"kind": kind, "document": doc, "libraries": lib_rows,
               "libraries_readable": libs_readable,
               "unread_libraries": sum(1 for r in lib_rows
                                       if r["material_count"] is None
                                       or r["appearance_count"] is None)}
    note = [
        "Library rows are a census - counts only, no contents.",
        f"Pass library='<name or id>' for one library's {kind}; name_filter= narrows them and "
        f"max_results= sizes the page (default {DEFAULT_CAP}, cap {MAX_CAP}).",
        "Names repeat, and 'id' names the source asset rather than the entry - document rows "
        "copied from one base share an id, so name and id together identify an entry.",
    ]
    if not doc["readable"]:
        note.append(f"The document's {kind} collection could not be read (document.readable=false), "
                    "so its count 0 means UNKNOWN, not empty.")
    if not libs_readable:
        note.append("The material-library collection could not be read (libraries_readable=false) - "
                    "an empty 'libraries' list here is UNKNOWN, not proof none are loaded.")
    if payload["unread_libraries"]:
        note.append(f"{payload['unread_libraries']} library row(s) carry a null count - that "
                    "library's entries could not be counted (unread_libraries).")
    note.append(_CONSUMERS[kind])
    payload["note"] = " ".join(note)
    return payload, None
