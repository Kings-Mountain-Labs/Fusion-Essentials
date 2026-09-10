# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Export-to-disk substrate: filename sanitizing, a component-by-name resolver, a file-landed
verifier, a bounded doEvents wait, and the one-file-per-top-level-occurrence split."""

import os
import time

import adsk.core
import adsk.fusion

from ._common import (safe, counted, all_components, all_occurrences, same_component,
                      named_with_remainder, spelled_as_read, _component_is_named)

MAP_BLURB = ("export-to-disk substrate: sanitize/prepare_out_path (safe filename, extension and "
             "directory prep), find_component/instance_paths (by-name resolve, shared name "
             "refused), top_level_occurrences/split_by_occurrence/failure_detail (one file per "
             "root occurrence), snapshot/verify_written (prove THIS call wrote), applied_pair "
             "(set an option, read it back), stl_unit_enum, pump_until (bounded doEvents wait)")


def sanitize(name):
    """Make an occurrence name safe for a filename (drop the ':1' instance suffix, swap path/illegal
    chars for '_')."""
    base = (name or "part").split(":")[0]
    out = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in base)
    return out or "part"


def prepare_out_path(file_path, ext):
    """The local output path an export writes to: (path, error) - whitespace and quotes stripped,
    `ext` appended when missing, the directory created. An empty request is (None, None)."""
    path = (file_path or "").strip().strip('"')
    if not path:
        return None, None
    if ext and not path.lower().endswith(ext.lower()):
        path = path + ext
    out_dir = os.path.dirname(path)
    if out_dir and not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            return None, f"Could not create output directory '{out_dir}': {e}"
    return path, None


# STLExportOptions.unitType takes DistanceUnits, NOT MeshUnits: the two enums have their mm/cm ints
# SWAPPED, so a MeshUnits value here writes 10x-wrong geometry for the two commonest units.
STL_UNIT_MEMBERS = {
    "mm": "MillimeterDistanceUnits", "cm": "CentimeterDistanceUnits", "m": "MeterDistanceUnits",
    "in": "InchDistanceUnits", "ft": "FootDistanceUnits",
}


def stl_unit_enum(key):
    """The DistanceUnits member for a unit key, or None when the family or the member is absent."""
    du = safe(lambda: adsk.fusion.DistanceUnits)
    member = STL_UNIT_MEMBERS.get(key)
    if du is None or not member:
        return None
    return safe(lambda: getattr(du, member))


# The (value, changed) pair for a knob nothing landed for.
NOT_APPLIED = (None, False)


def applied_pair(opts, prop, val, key):
    """Set ONE export-options property and read it back, pre-reading it too: (key, changed) when the
    property reads the requested value afterwards, else NOT_APPLIED. 'changed' says whether THIS
    assignment put the value there - a property already holding the requested value reads the same
    whether the assignment took or was dropped. Never raises, and never fails the export."""
    before = safe(lambda: getattr(opts, prop))
    safe(lambda: setattr(opts, prop, val))
    if safe(lambda: getattr(opts, prop)) != val:
        return NOT_APPLIED               # the read-back disagreed: nothing landed
    return key, before != val


def instance_paths(design, comps):
    """The fullPathName of every occurrence that PLACES one of `comps`, as a list - the candidate
    spellings a same-name refusal offers. Empty when nothing placed them; falls back to an
    occurrence's plain name where fullPathName does not read."""
    out = []
    for o in all_occurrences(design):
        c = safe(lambda o=o: o.component)
        # `is True`, never a bare truth test: same_component answers None where an identity did not
        # read, and an unproven match here names an occurrence placing something else.
        if c is None or not any(same_component(c, h) is True for h in comps):
            continue
        p = safe(lambda o=o: o.fullPathName) or safe(lambda o=o: o.name)
        if p:
            out.append(p)
    return out


def find_component(design, name):
    """Resolve ONE Component by name design-wide: (component, error_or_None). The match is exact but
    case-insensitive, whitespace stripped (_common._component_is_named); a name SEVERAL components
    carry is refused naming the occurrence paths that place them, a name none carries is
    (None, None) for the caller to word, and a blank name matches nothing."""
    want = (name or "").strip()
    if not want:
        return None, None
    hits = [c for c in all_components(design) if _component_is_named(c, want)]
    if len(hits) > 1:
        # A widened (case-insensitive) hit list must not manufacture an ambiguity: one hit spelled
        # exactly as asked for is the answer.
        cased = [c for c in hits if (safe(lambda c=c: c.name) or "") == want]
        if len(cased) == 1:
            hits = cased
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, None
    # The names as READ, not the query: a case-insensitive match means the hits can be spelled
    # differently from what was asked for, and those spellings are what tells them apart.
    spelled = spelled_as_read(hits, want)
    paths = named_with_remainder(["'" + p + "'" for p in instance_paths(design, hits)])
    where = f" Instances found: {paths}." if paths else ""
    return None, (f"{len(hits)} components match '{want}'{spelled}, so the name does not identify "
                  f"one of them.{where}")


def snapshot(path):
    """(exists, size_bytes, mtime_ns) for path right now - the baseline verify_written proves a write
    against. A target that does not exist yet is (False, 0, 0)."""
    exists = bool(safe(lambda: os.path.isfile(path), False))
    if not exists:
        return False, 0, 0
    st = safe(lambda: os.stat(path))
    if st is None:
        return True, 0, 0
    return True, safe(lambda: st.st_size, 0) or 0, safe(lambda: st.st_mtime_ns, 0) or 0


def verify_written(path, before=None):
    """Confirm THIS call wrote a non-empty file at path: (size_bytes, note), note None on success.
    `before` is the path's snapshot() from before the write - an already-existing target whose size
    AND mtime are both unchanged is reported as "wrote nothing". Passing no `before` keeps the
    weaker exists-and-non-empty check."""
    # A 3MF is a zip container and zips embed timestamps, so two 3MF exports at identical settings
    # differ in size: a size or byte difference between two 3MF files is never evidence a setting
    # took, and a 3MF re-export never reads as "wrote nothing" here.
    exists, size, mtime = snapshot(path)
    if not exists or not size:
        return 0, f"no file was written to '{path}' (file_exists={exists}, size_bytes={size})"
    if before is not None and before[0] and (size, mtime) == (before[1], before[2]):
        return 0, (f"the file at '{path}' is the one that was already there before this call - its "
                   f"size ({size} bytes) and modification time are both unchanged, so this export "
                   f"wrote nothing. (On a coarse-timestamp filesystem - FAT/exFAT, some network "
                   f"shares - a byte-identical re-export can read the same way; if that is this "
                   f"case, delete the target file and export again)")
    return size, None


def pump_until(probe, timeout_s, poll_sleep):
    """Pump adsk.doEvents until probe() -> (settled, reading) reports settled, bounded by timeout_s;
    returns (settled, last reading) either way. An asynchronous Fusion write only advances while the
    main thread is pumped, so this pumps rather than sleeps. probe() runs before the first pump and
    after every pump, the bound checked between the two."""
    deadline = time.monotonic() + timeout_s
    while True:
        settled, reading = probe()
        if settled:
            return True, reading
        if time.monotonic() >= deadline:
            return False, reading
        safe(lambda: adsk.doEvents())
        time.sleep(poll_sleep)


def top_level_occurrences(design):
    """The root component's top-level occurrences as a plain list, or None when the census could not
    be taken. [] and None are DIFFERENT answers: [] found no occurrence, None means which
    occurrences exist is unknown - a split export refuses on None rather than writing a short set."""
    root = safe(lambda: design.rootComponent)
    occs = safe(lambda: root.occurrences) if root is not None else None
    n = counted(lambda: occs.count) if occs is not None else None
    if n is None:
        return None
    out = []
    for i in range(n):
        occ = safe(lambda i=i: occs.item(i))
        if occ is None:
            return None       # a hole in the census: N-1 files would read as the whole design
        out.append(occ)
    return out


_MAX_DETAILED_FAILURES = 5


def failure_detail(errors, limit=_MAX_DETAILED_FAILURES):
    """The per-occurrence reasons from split_by_occurrence's error list as one '; '-joined line,
    capped at `limit` rows plus a count of the remainder."""
    shown = [f"{e.get('occurrence') or '(unnamed occurrence)'}: {e.get('error')}"
             for e in errors[:limit]]
    more = len(errors) - len(shown)
    return "; ".join(shown) + (f" (+{more} more)" if more > 0 else "")


def split_by_occurrence(occs, out_dir, ext, write_one):
    """Write one file per occurrence via write_one(occ, path) -> (size_bytes_or_None, error_or_None):
    (files, errors), one record each. Filenames are sanitized occurrence names with ext appended,
    de-duplicated when two occurrences produce the same final path."""
    files, errors, used_paths = [], [], set()
    for occ in occs:
        name = safe(lambda occ=occ: occ.name)
        stem = sanitize(name)
        suffix = 1
        while True:
            candidate_stem = stem if suffix == 1 else f"{stem}_{suffix}"
            path = os.path.join(out_dir, candidate_stem + ext)
            path_key = os.path.normcase(os.path.abspath(path))
            if path_key not in used_paths:
                used_paths.add(path_key)
                break
            suffix += 1
        size, eerr = write_one(occ, path)
        if eerr:
            errors.append({"occurrence": name, "error": eerr})
        else:
            files.append({"occurrence": name, "file_path": path, "size_bytes": size})
    return files, errors
