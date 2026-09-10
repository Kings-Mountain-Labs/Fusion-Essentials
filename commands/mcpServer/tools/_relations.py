# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Assembly-relations substrate: the per-kind walk over rigid groups, motion links and assembly
constraints, plus the resolve-one-by-name every lifecycle op targets through.

PROBE NEEDED - which relations answer an entityToken, and whether two wrappers of one relation
answer the same value. The walk reads nothing that tells the two de-dup survivor cases apart."""

from ._common import all_components, safe

# One-line "what to reuse from here" for the generated CLAUDE.md helper map (see tests/gen_manifest.py).
MAP_BLURB = ("the substrate assembly_get's relations slice and assembly_edit_relations share. "
             "all_relations - the ONE walk over a design's rigid groups / motion links / assembly "
             "constraints; relation_names/find_relation - the names for an error message and the "
             "EXACT resolve-one that REFUSES a duplicate; rigid_group_members - member paths")

# relation kind keyword -> (the Component collection it lives in, its wire label).
_KINDS = {
    "rigid_group": ("rigidGroups", "rigid group"),
    "motion_link": ("motionLinks", "motion link"),
    "constraint": ("assemblyConstraints", "assembly constraint"),
}

KINDS = tuple(_KINDS)


def kind_label(kind):
    """The human label for a relation kind keyword ('rigid_group' -> 'rigid group')."""
    entry = _KINDS.get(kind)
    return entry[1] if entry else kind


def all_relations(design, kind):
    """Every relation of `kind` as a flat list of (object, owning_component), over every component
    (a relation created inside a sub-assembly lives on THAT component)."""
    # _common.all_components already carries the root, so prepending design.rootComponent reads
    # every root relation twice as two distinct wrappers, which the id() key below never collapses.
    entry = _KINDS.get(kind)
    if entry is None:
        return []
    attr = entry[0]
    out, seen = [], set()
    for c in all_components(design):
        coll = safe(lambda c=c: getattr(c, attr))
        if coll is None:
            continue
        for i in range(safe(lambda coll=coll: coll.count, 0) or 0):
            obj = safe(lambda coll=coll, i=i: coll.item(i))
            if obj is None:
                continue
            token = safe(lambda obj=obj: obj.entityToken)
            # An EMPTY token takes the id() fallback with an unreadable one: keyed on "" every
            # relation answering it would collapse onto one row.
            key = token or id(obj)
            if key in seen:
                continue
            seen.add(key)
            out.append((obj, c))
    return out


def relation_names(design, kind):
    """The names of every relation of `kind`, unreadable ones dropped."""
    return [nm for nm in (safe(lambda obj=obj: obj.name) for obj, _c in all_relations(design, kind))
            if nm]


def find_relation(design, kind, name):
    """Resolve ONE relation of `kind` by case-insensitive exact name; returns (object,
    owning_component, error). A name is not unique across components, so several hits are REFUSED
    with the owning component of each."""
    want = (name or "").strip()
    if not want:
        return None, None, (f"'name' is required - the {kind_label(kind)} to act on "
                            "(assembly_get(include=['relations']) lists them).")
    pairs = all_relations(design, kind)
    hits = [(obj, c) for obj, c in pairs if (safe(lambda obj=obj: obj.name) or "").lower() == want.lower()]
    if not hits:
        names = [nm for nm in (safe(lambda obj=obj: obj.name) for obj, _c in pairs) if nm]
        return None, None, (f"No {kind_label(kind)} named '{want}'. This design holds: "
                            f"{', '.join(names) or '(none)'}. Full list: "
                            "assembly_get(include=['relations']).")
    if len(hits) > 1:
        where = ", ".join(f"'{want}' in {safe(lambda c=c: c.name) or '?'}" for _o, c in hits[:8])
        return None, None, (f"'{want}' names {len(hits)} {kind_label(kind)}s ({where}) - refusing to "
                            "guess which one. Rename one in Fusion so the target is unambiguous.")
    return hits[0][0], hits[0][1], None


def rigid_group_members(rg, cap=None):
    """A rigid group's members as fullPathNames (local name when a path cannot be read), returned
    as (names bounded by `cap`, total member count)."""
    occs = safe(lambda: rg.occurrences)
    if occs is None:
        return [], 0
    total = safe(lambda: occs.count, 0) or 0
    shown = total if cap is None else min(total, max(0, int(cap)))
    names = []
    for i in range(shown):
        o = safe(lambda i=i: occs.item(i))
        if o is None:
            continue
        names.append(safe(lambda o=o: o.fullPathName) or safe(lambda o=o: o.name))
    return names, total
