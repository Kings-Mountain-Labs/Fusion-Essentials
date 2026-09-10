# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Thread-table lookup shared by model_hole and model_thread."""

from ._common import safe

MAP_BLURB = ("resolve_thread_info - the ONE thread-table walk turning a bare designation "
             "('M5x0.8', '1/4-20 UNC') into a ThreadInfo, shared by model_hole's tap and "
             "model_thread; also returns every thread type carrying that designation")


def thread_types_for(tdq, designation):
    """Every thread type whose table carries `designation`, in library order."""
    hits = []
    for ttype in (safe(lambda: list(tdq.allThreadTypes), []) or []):
        for size in (safe(lambda t=ttype: list(tdq.allSizes(t)), []) or []):
            desigs = safe(lambda t=ttype, s=size: list(tdq.allDesignations(t, s)), []) or []
            if designation in desigs:
                hits.append(ttype)
                break
    return hits


def resolve_thread_info(comp, designation, internal=True, thread_type="", thread_class=""):
    """Build a ThreadInfo for a thread DESIGNATION like 'M5x0.8'. `thread_type` picks the standard
    when several carry it, `thread_class` the fit within that standard.
    Returns (threadInfo, types_carrying_it, None) or (None, types, error)."""
    # An empty ThreadFeatures collection is falsy (count 0) but not None.
    tf = safe(lambda: comp.features.threadFeatures)
    if tf is None:
        return None, [], "This component has no thread features collection."
    tdq = safe(lambda: tf.threadDataQuery)
    if tdq is None:
        return None, [], "Thread data query unavailable."

    hits = thread_types_for(tdq, designation)
    if not hits:
        return None, [], (f"No thread designation '{designation}' found in the thread library. "
                          "Use a standard call-out like 'M5x0.8' or '1/4-20 UNC'.")
    want = (thread_type or "").strip()
    if want:
        chosen = next((t for t in hits if t.lower() == want.lower()), None)
        if chosen is None:
            return None, hits, (f"Thread type '{thread_type}' does not carry '{designation}'. "
                                f"Types that do: {', '.join(hits)}.")
    else:
        # Types sharing a designation build ThreadInfos equal in every scalar but threadType, so
        # library order picks one; the caller is returned `hits` to see what else carried it.
        chosen = hits[0]

    # A class is a fit tolerance, not interchangeable: 4g6g and 6g are different fits of one thread.
    classes = safe(lambda: list(tdq.allClasses(internal, chosen, designation)), []) or []
    want = (thread_class or "").strip()
    if want:
        cls = next((c for c in classes if c.lower() == want.lower()), None)
        if cls is None:
            return None, hits, (f"Thread class '{thread_class}' is not offered for '{designation}' "
                                f"in '{chosen}'. Classes that are: {', '.join(classes) or '(none)'}.")
    else:
        cls = classes[0] if classes else ""
    ti = safe(lambda: tf.createThreadInfo(internal, chosen, designation, cls))
    if ti is None:
        return None, hits, f"createThreadInfo failed for '{designation}' in '{chosen}'."
    return ti, hits, None
