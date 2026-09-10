# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Analyzes the active assembly's occurrences for solid overlap (interference), reporting each
interfering pair by occurrence name with its overlap volume. Coincident/flush faces are excluded by
default. Read-only.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _outputs

app = adsk.core.Application.get()

# What this tool RETURNS: the verdict contract - relation/passed/measured/tolerance_used, enforced.
RETURNS = [_outputs.ReturnsVerdict(relations=("interference_free",))]


def _native_body_owners(occurrences):
    """{_common.native_identity(native body) -> [occurrence fullPathName, ...]} for the analysis
    set. A body whose identity does not read is skipped."""
    # analyzeInterference hands back NATIVE bodies - assemblyContext reads None on both result
    # entities even for an occurrence input - so this map is how the instance gets named. Keyed on
    # native_identity: a bare entityToken is DOCUMENT-LOCAL and merges bodies across x-refs.
    owners = {}
    for occ in occurrences:
        path = safe(lambda o=occ: o.fullPathName)
        comp = safe(lambda o=occ: o.component)
        if not path or comp is None:
            continue
        for b in (safe(lambda c=comp: c.bRepBodies, None) or []):
            ident = _common.native_identity(b)
            if ident is not None:
                owners.setdefault(ident, []).append(path)
    return owners


# How many candidate instance paths a pair row publishes per side; a row whose full list exceeds
# this carries a *_candidates_truncated flag, and the label's "or N more" count is the true total.
_CANDIDATE_CAP = 8


def _owning_occurrence_name(body, owners):
    """The INSTANCE that owns this body, as (label, candidates) - candidates is None when the
    instance is exact and the FULL path list when one native body serves several, since the
    platform cannot say WHICH instance collided. Falls back to the COMPONENT name when the body
    maps to no occurrence."""
    ident = _common.native_identity(body)
    paths = owners.get(ident) if ident is not None else None
    if paths:
        if len(paths) == 1:
            return paths[0], None
        return (f"{paths[0]} (or {len(paths) - 1} more instance(s) whose component owns this same "
                "native body)", list(paths))
    occ = safe(lambda: body.assemblyContext)
    if occ is not None:
        nm = safe(lambda: occ.fullPathName) or safe(lambda: occ.name)
        if nm:
            return nm, None
    pc = safe(lambda: body.parentComponent)
    if pc is not None:
        nm = safe(lambda: pc.name)
        if nm:
            return nm, None
    return safe(lambda: body.name) or "(unknown)", None


def handler(include_coincident_faces: bool = False) -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design to analyze.")
    root = safe(lambda: design.rootComponent)
    if not root:
        return error("No root component.")

    # The analysis set is EVERY occurrence at every depth plus any solid body the root owns
    # directly; root.occurrences is TOP LEVEL only. The shared census, not a bare
    # root.allOccurrences: that property RAISES on an unresolved external reference.
    walk = _common.occurrence_walk(design)
    occs = adsk.core.ObjectCollection.create()
    occ_list = []
    for o in walk.occurrences:
        occs.add(o)
        occ_list.append(o)
    n_occ = len(occ_list)
    n_root_bodies = 0
    for b in (safe(lambda: root.bRepBodies, None) or []):
        if safe(lambda b=b: b.isSolid):
            occs.add(b)
            n_root_bodies += 1
    n_entities = n_occ + n_root_bodies
    if n_entities < 2:
        # Fewer than two things to compare yields NO verdict. Returning passed=true would let a
        # caller gate a build on an answer this tool never formed, so it refuses instead - the
        # verdict contract has no "unknown" and a fabricated pass is the dangerous direction.
        return error(
            f"Cannot check interference: this design exposes {n_entities} comparable solid "
            f"entit{'y' if n_entities == 1 else 'ies'} ({n_occ} occurrence(s) at any depth, "
            f"{n_root_bodies} root-level solid body(ies)), and interference needs at least two. "
            "No verdict was formed - this is NOT a pass.")

    try:
        inp = design.createInterferenceInput(occs)
        inp.areCoincidentFacesIncluded = bool(include_coincident_faces)
        results = design.analyzeInterference(inp)
    except Exception as e:
        return error(f"Interference analysis failed: {e}")

    owners = _native_body_owners(occ_list)
    items = []
    # Aggregate overlap volume per occurrence pair (a pair can produce several interference bodies).
    pair_vol = {}
    candidates_by_label = {}
    for r in _common.iter_collection(results):
        one, one_cands = _owning_occurrence_name(safe(lambda r=r: r.entityOne), owners)
        two, two_cands = _owning_occurrence_name(safe(lambda r=r: r.entityTwo), owners)
        if one_cands:
            candidates_by_label[one] = one_cands
        if two_cands:
            candidates_by_label[two] = two_cands
        vol = safe(lambda r=r: r.interferenceBody.volume) if safe(lambda r=r: r.interferenceBody) else None
        key = tuple(sorted([one, two]))
        pair_vol.setdefault(key, 0.0)
        if vol:
            pair_vol[key] += float(vol)
    for (one, two), vol in sorted(pair_vol.items(), key=lambda kv: -kv[1]):
        row = {"occurrence_one": one, "occurrence_two": two,
               "overlap_volume_cm3": round(vol, 4)}
        for side, label in (("occurrence_one", one), ("occurrence_two", two)):
            cands = candidates_by_label.get(label)
            if cands:
                row[f"{side}_candidates"] = cands[:_CANDIDATE_CAP]
                if len(cands) > _CANDIDATE_CAP:
                    row[f"{side}_candidates_truncated"] = True
        items.append(row)

    clear = len(items) == 0
    # A CLEAN verdict is a claim about everything; a positive finding is not. So an incomplete
    # analysis set refuses only when it would otherwise report a pass - one interfering pair that
    # WAS found stays true whatever the walk missed.
    if clear and (walk.broken or not walk.complete):
        if walk.broken:
            missing = (f"{len(walk.broken)} occurrence(s) hold an unresolved external reference "
                       f"({', '.join(sorted({b['name'] for b in walk.broken}))}) - their component "
                       "could not be read, so they carry no geometry this analysis could compare")
        else:
            missing = ("the design-wide occurrence walk did not complete, so the analysis set is a "
                       "subset of the assembly")
        return error(
            f"Cannot certify interference-free: {missing}. {n_occ} occurrence(s) and "
            f"{n_root_bodies} root-level solid body(ies) WERE compared and none of them interfere, "
            "but that is not a verdict over the whole assembly - no pass was formed. "
            "Resolve the reference (see workspace_orient health.unresolved_references) and re-run.")
    note = ("No interference - every part fits." if clear else
            f"{len(items)} interfering pair(s) - parts overlap in space. Each lists the two "
            "occurrences and their total overlap volume; fix positioning/sizing/joints. (A "
            "self-pair means two bodies of the same occurrence overlap.)")
    if candidates_by_label:
        note += (" A side with '*_candidates' resolved to ONE native body, and each candidate path "
                 "is an occurrence whose component owns that body - analyzeInterference returns "
                 "native bodies, so the exact instance cannot be read off the result; the candidate "
                 "paths are listed (capped: a *_candidates_truncated flag marks an incomplete list, "
                 "and the side's label carries the true candidate count). Discriminate by position "
                 "(assembly_get occurrence origins) or move one instance and re-check.")
    if walk.broken:
        note += (f" {len(walk.broken)} occurrence(s) with an unresolved external reference were NOT "
                 "compared - their component could not be read, so they carry no geometry for this "
                 "analysis; measured.unresolved_references names them.")
    return ok({
        "relation": "interference_free",
        "passed": clear,
        "measured": {"interference_count": len(items), "occurrences_checked": n_occ,
                     "root_bodies_checked": n_root_bodies,
                     # WHICH walk produced the analysis set, so a caller can tell a design-wide
                     # comparison from one rebuilt around an unreadable allOccurrences.
                     "occurrences_walk": walk.method,
                     "unresolved_references": walk.names(),
                     "interferences": items},
        "tolerance_used": {"coincident_faces_included": bool(include_coincident_faces)},
        "note": note,
    })


TOOL_DESCRIPTION = (
    "Check the active assembly for solid overlap - each interfering pair with its overlap volume "
    "(cm^3). passed=true when nothing interferes.\n"
    + _outputs.produces_block(RETURNS)
)

interference_tool = (
    Tool.create_simple(name="assembly_inspect_interference", description=TOOL_DESCRIPTION)
    .add_input_property("include_coincident_faces", {"type": "boolean",
            "description": "Flush touches count as interference."})
    .strict_schema()
)
interference_item = Item.create_tool_item(tool=interference_tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(interference_item)
