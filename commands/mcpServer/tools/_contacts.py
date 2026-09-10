# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Contact-set substrate: the design-scoped walk, the resolve-one, and the membership read-back.

The member property is spelled occurencesAndBodies - ONE 'r' - and the correctly spelled name is
accepted silently while changing nothing, so a membership write is confirmed only by re-reading."""

import adsk.fusion

from ._common import safe

# One-line "what to reuse from here" for the generated CLAUDE.md helper map (see tests/gen_manifest.py).
MAP_BLURB = ("the substrate assembly_get's contacts slice and assembly_edit_contacts share. "
             "contact_sets/all_contact_sets/contact_set_names - the DESIGN-scoped contactSets walk; "
             "find_contact_set - the EXACT resolve-one, refusing a duplicate; "
             "membership/member_label - the occurencesAndBodies read-back")


def contact_sets(design):
    """The design's ContactSets collection, or None."""
    return safe(lambda: design.contactSets)


def all_contact_sets(design):
    """Every ContactSet in the design, in collection order."""
    sets = contact_sets(design)
    if sets is None:
        return []
    out = []
    for i in range(safe(lambda: sets.count, 0) or 0):
        cs = safe(lambda i=i: sets.item(i))
        if cs is not None:
            out.append(cs)
    return out


def contact_set_names(design):
    """The names of every contact set, unreadable ones dropped."""
    return [nm for nm in (safe(lambda cs=cs: cs.name) for cs in all_contact_sets(design)) if nm]


def find_contact_set(design, name):
    """Resolve ONE contact set by case-insensitive exact name; returns (contact_set, error)."""
    # Fusion's auto-dedupe of a colliding name is CASE-SENSITIVE, so two sets can answer one
    # case-insensitive query; several hits are REFUSED, never the first.
    want = (name or "").strip()
    if not want:
        return None, ("'name' is required - the contact set to act on "
                      "(assembly_get(include=['contacts']) lists them).")
    sets = all_contact_sets(design)
    hits = [cs for cs in sets if (safe(lambda cs=cs: cs.name) or "").lower() == want.lower()]
    if not hits:
        names = [nm for nm in (safe(lambda cs=cs: cs.name) for cs in sets) if nm]
        return None, (f"No contact set named '{want}'. This design holds: "
                      f"{', '.join(names) or '(none)'}. Full list: "
                      "assembly_get(include=['contacts']).")
    if len(hits) > 1:
        return None, (f"'{want}' names {len(hits)} contact sets - refusing to guess which one. "
                      "Rename one in Fusion so the target is unambiguous.")
    return hits[0], None


def member_label(entity):
    """A member's name - an Occurrence's fullPathName, a BRepBody's name, else None."""
    # A body read back off occurencesAndBodies arrives as an object both casts reject, so it
    # answers None here and membership() counts it as unnamed instead.
    occ = safe(lambda: adsk.fusion.Occurrence.cast(entity))
    if occ is not None:
        return safe(lambda: occ.fullPathName) or safe(lambda: occ.name)
    body = safe(lambda: adsk.fusion.BRepBody.cast(entity))
    if body is not None:
        return safe(lambda: body.name)
    return None


def membership(cs, cap=None):
    """A contact set's members as (names bounded by `cap`, total, unnamed within that bound), or
    (None, None, None) when occurencesAndBodies cannot be read."""
    members = safe(lambda: list(cs.occurencesAndBodies))
    if members is None:
        return None, None, None
    shown = members if cap is None else members[:max(0, int(cap))]
    labels = [member_label(m) for m in shown]
    return [lb for lb in labels if lb], len(members), sum(1 for lb in labels if not lb)
