# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Deletes ONE timeline object (a feature/sketch/pattern/mirror/joint/etc.) by name, deleting its
associated entity - the way to undo a botched pattern/mirror without rebuilding the document. An
ambiguous name or a timeline GROUP is refused; timeline health is reported before/after so a delete
that breaks a downstream feature is surfaced. WRITES (destructive).
"""

import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _inputs


def _timeline(design):
    """The design's timeline, or None for a direct-modelling design (no history)."""
    return safe(lambda: design.timeline)


# the shared timeline-health walk (before/after edit guard) - one home in _common
from ._common import timeline_health as _timeline_health


def _find_object(timeline, want):
    """(TimelineObject, error_text) for the ONE object named `want`, through the shared by-name
    resolver: an EXACT case-insensitive match, a repeated name refused with its candidates."""
    return _inputs.resolve_timeline_object(_inputs._timeline_objects(timeline), want,
                                           "the feature to delete")


def _name_hits(timeline, name):
    """How many timeline objects carry `name` right now, through the same matcher the delete was
    resolved with - None when the timeline itself could not be read, which is not a count of zero."""
    if timeline is None or _common.counted(lambda: timeline.count) is None:
        return None
    return len(_inputs._match_timeline_objects(_inputs._timeline_objects(timeline), name))


def _remove_features_named(design, name):
    """Every RemoveFeature named `name`, as (feature, component_name) - itemByName over each
    component's features.removeFeatures, across the shared design-wide component walk. A list, so
    the caller refuses a duplicate instead of grabbing the first hit."""
    hits = []
    for comp in _common.all_components(design):
        feats = safe(lambda c=comp: c.features.removeFeatures)
        feat = safe(lambda: feats.itemByName(name)) if feats is not None else None
        if feat is not None:
            hits.append((feat, safe(lambda c=comp: c.name)))
    return hits


def _occurrence_present(design, path):
    """Is an occurrence with `path` as its fullPathName in the design right now? Reads the shared
    assembly-context path census, so a collection that cannot be read answers False (not confirmed)
    rather than raising."""
    return path in _common.occurrence_paths(design)


def handler(feature: str = "") -> dict:
    """Delete one timeline feature by name (design_get(include=['timeline']) lists them), reporting
    the timeline health before and after. WRITES (destructive)."""
    want = (feature or "").strip()
    if not want:
        return error("Provide 'feature' - the timeline object name to delete (see design_get(include=['timeline'])).")

    design = _common.design()
    if not design:
        return error("No active design (open a document with design geometry).")

    timeline = _timeline(design)
    if timeline is None:
        return error("This design has no timeline (a direct-modelling design has no deletable timeline "
                     "features). Delete bodies/occurrences directly instead.")

    obj, rerr = _find_object(timeline, want)
    if rerr:
        return error(rerr)

    name = safe(lambda: obj.name) or want
    index = safe(lambda: obj.index)

    if safe(lambda: obj.isGroup):
        return error(f"'{name}' is a timeline GROUP, which has no deletable entity. Ungroup it (or "
                     "delete its member features) instead.")

    entity = safe(lambda: obj.entity)
    if entity is None:
        return error(f"'{name}' has no associated entity to delete (it may be a group or an "
                     "unsupported timeline object).")

    # An Occurrence .entity does not say WHICH kind of timeline object this is: a Remove FEATURE
    # reports the REMOVED occurrence (whose deleteMe() raises "2 : InternalValidationError") and an
    # occurrence CREATE the LIVE one, so the name lookup below is what tells them apart.
    remove_feature, removed_path = None, None
    occ_type = safe(lambda: adsk.fusion.Occurrence)
    if occ_type is not None and safe(lambda: isinstance(entity, occ_type), False):
        hits = _remove_features_named(design, name)
        if len(hits) > 1:
            where = ", ".join(c or "?" for _, c in hits[:8])
            return error(f"'{name}' names a RemoveFeature in {len(hits)} components ({where}) - "
                         "refusing to guess which one this timeline object belongs to.")
        if hits:
            # IDENTITY, not the name alone: a RemoveFeature that merely shares the name is not this
            # object, so the reroute is accepted only when the feature's own timelineObject sits at
            # the index that was resolved.
            feat_index = safe(lambda: hits[0][0].timelineObject.index)
            if index is not None and feat_index is not None and feat_index == index:
                # Deleting the Remove feature puts the occurrence back, so its path is captured here
                # to read that restoration back after the delete.
                remove_feature = hits[0][0]
                removed_path = safe(lambda: entity.fullPathName)
                entity = remove_feature
    entity_type = safe(lambda: type(entity).__name__)

    err_before, _, _ = _timeline_health(design)
    # The name census BEFORE the delete is what makes the one after it mean something: a census that
    # already fails to see this object (an unreadable name, a timeline the walk cannot read) proves
    # nothing by not seeing it afterwards either.
    hits_before = _name_hits(timeline, name)
    try:
        did = entity.deleteMe()
    except Exception as e:
        return error(f"Could not delete '{name}': {e}")
    if not did:
        return error(f"Fusion declined to delete '{name}' (deleteMe returned false). It may be "
                     "depended on in a way that blocks deletion.")

    hits_after = _name_hits(_timeline(design), name)
    if hits_before and hits_after is not None and hits_after >= hits_before:
        return error(f"deleteMe() reported success but the timeline still carries {hits_after} "
                     f"object(s) named '{name}' - as many as before the delete ({hits_before}), so "
                     "it was NOT removed. Nothing was rolled back; re-read "
                     "design_get(include=['timeline']) to see what is actually there.")
    # True only when the re-read PROVED one object of this name left the timeline; null when the
    # census could not settle it. An unreadable check is never counted as absence.
    gone = True if (hits_before and hits_after is not None) else None

    err_after, warn_after, _ = _timeline_health(design)

    out = {
        "deleted": gone,
        "feature": name,
        "index": index,
        "entity_type": entity_type,
        "note": "Timeline feature deleted. Geometry it produced is removed; instances it created "
        "(pattern/mirror copies) go with it. Pair with design_get(include=['timeline']) / workspace_orient to confirm.",
    }
    if remove_feature is not None:
        # The OPPOSITE of the generic note: deleting a Remove feature puts its occurrence BACK
        # (live-verified) - the reversal design_remove_feature's own wire text promises.
        out["note"] = ("Remove FEATURE deleted - the occurrence it had taken out is back in the "
                       "assembly. Confirm with design_get(include=['tree']).")
        # Read the restoration back off the occurrence walk. Reported only when the walk FINDS it:
        # an unreadable path and an absent one both mean "not confirmed", and neither may be
        # published as "it did not come back".
        if removed_path and _occurrence_present(design, removed_path):
            out["occurrence_restored"] = removed_path
    if gone is None:
        why = ("the timeline could not be read back after the delete" if hits_after is None else
               f"the timeline census could not see any object named '{name}' before the delete")
        out["note"] = (f"deleteMe() reported success for '{name}', but its absence is UNVERIFIED: "
                       f"{why}, so the re-read cannot prove the object is gone - and a check that "
                       "could not read is not a check that found nothing. Nothing was rolled back; "
                       "re-read design_get(include=['timeline']) to see what is actually there.")
    if len(err_after) > len(err_before):
        out["timeline_warning"] = (
            f"The timeline carries a new error after this call ({err_after}) that it did not carry "
            "before. Nothing was rolled back; 'deleted' says whether the object's absence was "
            "verified (null = it was not). Undo in Fusion if this was not wanted.")
    elif warn_after:
        out["timeline_warnings"] = warn_after
    return ok(out)


_DESC = (
"Delete one timeline feature by name; a pattern/mirror delete takes every instance it created."
)

tool = (
    Tool.create_simple(name="design_delete_feature", description=_DESC)
    .add_input_property("feature", {"type": "string",
            "description": "Timeline object name, from design_get(include=['timeline'])."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_design_delete_feature.py::TestAbsenceReRead"
                      "::test_a_survivor_is_an_error_not_a_false_ok"))


def register_tool():
    register(item)
