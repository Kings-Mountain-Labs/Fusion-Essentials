# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Drive the parametric timeline: move the marker, suppress a timeline object, group a range of
items, discard everything after the marker, and tag a feature with an attribute.
Timeline.movetoNextStep carries a lowercase 't' - that is the member name the API exposes.
"""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from . import _common
from . import _inputs
from ._common import error, ok, safe, short_ref, timeline_health

_ACTIONS = ("roll", "suppress", "group", "ungroup", "delete_after_marker",
            "set_attribute", "delete_attribute")
_PLACES = ("before", "after")            # roll to a named item - TimelineObject.rollTo(rollBefore)
_STEPS = ("beginning", "end", "next", "previous")   # roll with no feature - the Timeline marker moves

_PREVIEW_MAX = 12                        # cap the discard preview; the count carries the rest

# This tool's own wire bound on the attribute strings it carries - not a platform limit.
_ATTR_MAX_CHARS = 10000
# Design.findAttributes reads a leading lowercase 're:' as a regular expression rather than a
# literal name, so the check that refuses the prefix matches that exact lower-case spelling -
# 'RE:shop' is an ordinary literal group name.
_REGEX_PREFIX = "re:"

_UNREADABLE = object()      # a read-back that RAISED - distinct from one that reads None

_ACTION = _inputs.Choice("action", list(_ACTIONS), default="roll")
_TO = _inputs.Choice(
    "to", list(_PLACES) + list(_STEPS), default="before",
    description="before/after a named 'feature'; the steps take none.")
# The attribute actions target the FEATURE ENTITY, which is what the FeatureRef kind resolves to;
# it rides on the shared 'feature' input rather than adding a second name for the same thing.
_ATTR_TARGET = _inputs.FeatureRef("feature")


def _objects(timeline):
    """Every TimelineObject in timeline order, unreadable slots dropped - the shared collection walk,
    so this list and the one FeatureRef resolves against skip the same slots. A skip leaves a HOLE, so
    a position here is NOT an object's .index; everything addressing an index reads .index itself."""
    return list(_common.iter_collection(timeline))


def _index(obj):
    """The object's timeline index, or -1 when it cannot be read (never a silent 0)."""
    i = safe(lambda: obj.index)
    return i if isinstance(i, int) else -1


def _label(obj):
    """'name@index' - the exact form _resolve_object accepts back to disambiguate a repeated name."""
    return f"{safe(lambda: obj.name)}@{safe(lambda: obj.index)}"


def _sample(labels):
    head = labels[:_PREVIEW_MAX]
    more = len(labels) - len(head)
    return ", ".join(head) + (f" and {more} more" if more > 0 else "")


def _groups(timeline):
    """Every TimelineGroup, unreadable slots dropped. Read from timelineGroups, NOT from
    timeline.item(): an EXPANDED group is absent from the timeline enumeration entirely - only its
    members appear there - so a walk of timeline.item() sees collapsed groups only."""
    return list(_common.iter_collection(safe(lambda: timeline.timelineGroups)))


def _member_span(group):
    """(first_index, last_index) of an EXPANDED group's members in the timeline's index space, or
    None. A COLLAPSED group's members raise InternalValidationError on .index - the group object
    itself carries the only readable index then, and it is the one timeline.item() enumerates."""
    n = safe(lambda: group.count, 0) or 0
    idx = [i for i in (safe(lambda j=j: group.item(j).index) for j in range(n)) if isinstance(i, int)]
    return (min(idx), max(idx)) if idx else None


def _member_count(obj):
    """How many timeline items `obj` really stands for: a group's member count, else 1. A collapsed
    group is ONE entry in timeline.item() but discarding it discards every member with it."""
    if safe(lambda: obj.isGroup):
        return max(safe(lambda: obj.count, 0) or 0, 1)
    return 1


def _resolve_object(timeline, want, role):
    """(TimelineObject, error_text) for ONE object named `want`, through the shared by-name resolver;
    `role` is this call's noun for the target, and the miss hint names a collapsed group."""
    def collapsed_group_hint(name):
        holder = _group_holding(timeline, name.lower())
        if not holder:
            return None
        return (f"'{name}' is inside the collapsed timeline group '{holder}', so the timeline does "
                "not expose it directly. Expand the group in Fusion, or target "
                f"'{holder}' itself - rolling to a collapsed group works.")

    return _inputs.resolve_timeline_object(_objects(timeline), want, role,
                                           miss_hint=collapsed_group_hint)


def _group_holding(timeline, low_name):
    """The name of the COLLAPSED group holding an item called `low_name` (already lower-cased), or
    None. A collapsed group's members are reachable through the group but not through
    timeline.item(), so this is the only way to tell 'hidden' from 'absent'."""
    for g in _groups(timeline):
        if safe(lambda g=g: g.isCollapsed) is not True:
            continue
        for m in _common.iter_collection(g):
            if (safe(lambda m=m: m.name) or "").lower() == low_name:
                return safe(lambda g=g: g.name) or "(unnamed group)"
    return None


def _marker_facts(timeline, count):
    """{marker_position, timeline_count, rolled_back} for a payload; rolled_back is the number of
    items the marker leaves out of the computed model."""
    pos = safe(lambda: timeline.markerPosition)
    facts = {"marker_position": pos, "timeline_count": count}
    if pos is not None:
        facts["rolled_back"] = max(count - pos, 0)
    return facts


def _do_roll(timeline, feature, to):
    before = safe(lambda: timeline.markerPosition)
    count = safe(lambda: timeline.count, 0) or 0
    if feature:
        if to not in _PLACES:
            return error(f"action='roll' with a 'feature' takes to='before' or to='after' (got '{to}').")
        obj, rerr = _resolve_object(timeline, feature, "the object to roll to")
        if rerr:
            return error(rerr)
        # A COLLAPSED group rolls like any item. An EXPANDED one raises "Associated feature is
        # invalid." - but it is also absent from the timeline enumeration, so _resolve_object never
        # hands one back and the raise below is what reports any such refusal.
        try:
            did = obj.rollTo(to == "before")
        except Exception as e:
            return error(f"Rolling {to} '{_label(obj)}' failed: {e}")
        after = safe(lambda: timeline.markerPosition)
        if not did:
            return error(f"Fusion declined to roll the marker {to} '{_label(obj)}' (rollTo returned "
                         f"false); markerPosition is still {after} of {count}.")
        # a rolled-back object is the one NOT being computed: rolling BEFORE it must leave it out of
        # the model, rolling AFTER it must bring it in.
        rolled = safe(lambda: obj.isRolledBack)
        if rolled is None:
            if after == before:
                return error(f"rollTo reported success but markerPosition is still {before} and "
                             f"'{_label(obj)}' does not report isRolledBack - the roll is unconfirmed.")
        elif bool(rolled) != (to == "before"):
            return error(f"rollTo reported success but '{_label(obj)}' reads isRolledBack={bool(rolled)} "
                         f"after rolling {to} it; markerPosition {before} -> {after}.")
        out = {"rolled": True, "to": to, "feature": safe(lambda: obj.name),
               "marker_position_before": before}
        out.update(_marker_facts(timeline, count))
        out["note"] = ("Items after the marker are rolled back - they are not computed and their "
                       "geometry is absent until the marker returns. Roll to='end' when done.")
        return ok(out)

    if to not in _STEPS:
        return error(f"action='roll' without a 'feature' takes to='beginning'/'end'/'next'/'previous' "
                     f"(got '{to}' - that one names a position relative to a 'feature').")
    # movetoNextStep: the lowercase 't' is the real member name on Timeline.
    move = {"beginning": lambda: timeline.moveToBeginning(),
            "end": lambda: timeline.moveToEnd(),
            "next": lambda: timeline.movetoNextStep(),
            "previous": lambda: timeline.moveToPreviousStep()}[to]
    try:
        did = move()
    except Exception as e:
        return error(f"Moving the marker to the {to} failed: {e}")
    after = safe(lambda: timeline.markerPosition)
    if after is None:
        return error(f"The marker move to the {to} returned {bool(did)} but markerPosition cannot be "
                     "read back, so nothing confirms it.")
    if not did:
        return error(f"Fusion declined to move the marker to the {to} (returned false); it is still "
                     f"at {after} of {count}.")
    expected = {"beginning": 0, "end": count}.get(to)
    if expected is not None and after != expected:
        return error(f"The move to the {to} reported success but markerPosition is {after}, not "
                     f"{expected}.")
    if expected is None and before is not None:
        moved = (after > before) if to == "next" else (after < before)
        if not moved:
            return error(f"The move to the {to} step reported success but markerPosition is still "
                         f"{after} of {count}.")
    out = {"rolled": True, "to": to, "marker_position_before": before}
    out.update(_marker_facts(timeline, count))
    out["note"] = ("Items after the marker are rolled back - they are not computed and their geometry "
                   "is absent until the marker returns. Roll to='end' when done.")
    return ok(out)


def _do_suppress(design, timeline, feature, suppressed):
    if not feature:
        return error("action='suppress' needs 'feature' - the timeline object to suppress or "
                     "unsuppress (from design_get(include=['timeline'])).")
    obj, rerr = _resolve_object(timeline, feature, "the object to suppress")
    if rerr:
        return error(rerr)
    was = safe(lambda: obj.isSuppressed)
    errors_before, _warn, _total = timeline_health(design)
    try:
        obj.isSuppressed = bool(suppressed)
    except Exception as e:
        return error(f"Could not set isSuppressed on '{_label(obj)}': {e}")
    now = safe(lambda: obj.isSuppressed)
    if bool(now) != bool(suppressed):
        return error(f"Setting isSuppressed={bool(suppressed)} on '{_label(obj)}' did not take - it "
                     f"reads {now}.")
    errors_after, warnings_after, _total = timeline_health(design)
    out = {"feature": safe(lambda: obj.name), "index": safe(lambda: obj.index),
           "is_suppressed": bool(now), "was_suppressed": bool(was),
           "note": ("A suppressed item is skipped when the model rebuilds; its geometry is absent "
                    "until it is unsuppressed with suppressed=false.")}
    new_errors = [n for n in errors_after if n not in errors_before]
    if new_errors:
        out["timeline_errors_after"] = new_errors
        out["note"] = (f"The change left {len(new_errors)} feature(s) in error: "
                       + ", ".join(new_errors) + ". A downstream feature consumed what this one "
                       "produced - set suppressed=false to restore it.")
    elif warnings_after:
        out["timeline_warnings"] = warnings_after
    return ok(out)


def _do_group(timeline, feature, end_feature, name):
    if not feature or not end_feature:
        return error("action='group' needs 'feature' (the first item) and 'end_feature' (the last "
                     "item) - the range to group, from design_get(include=['timeline']).")
    start, serr = _resolve_object(timeline, feature, "the first item of the group")
    if serr:
        return error(serr)
    end, eerr = _resolve_object(timeline, end_feature, "the last item of the group")
    if eerr:
        return error(eerr)
    si, ei = safe(lambda: start.index), safe(lambda: end.index)
    if si is None or ei is None:
        return error(f"Could not read the timeline index of '{_label(start)}' / '{_label(end)}', so "
                     "the range to group is unknown.")
    if si > ei:
        return error(f"'{_label(start)}' comes after '{_label(end)}' - pass 'feature' and "
                     "'end_feature' in timeline order.")
    nested = [_label(o) for o in _objects(timeline)
              if si <= _index(o) <= ei and safe(lambda o=o: o.isGroup)]
    if nested:
        return error(f"A timeline group cannot hold another group, and {_sample(nested)} lies in "
                     f"{si}..{ei}. Remove it with action='ungroup' first, or pick a range without it.")
    # An EXPANDED group is invisible to the walk above - only its MEMBERS appear in timeline.item() -
    # and add() does NOT refuse a range covering them: it returns a group, and the two then share
    # members while the timeline enumeration loses items. Refuse the overlap here instead.
    for g in _groups(timeline):
        span = _member_span(g)
        if span and si <= span[1] and span[0] <= ei:
            gname = safe(lambda g=g: g.name) or "a timeline group"
            return error(f"Timeline items {si}..{ei} overlap the expanded group '{gname}' (its "
                         f"members span {span[0]}..{span[1]}), and a timeline item can belong to "
                         "only one group. Remove it with action='ungroup' first, or pick a range "
                         "clear of it.")
    groups = safe(lambda: timeline.timelineGroups)
    if groups is None:
        return error("This timeline exposes no timelineGroups collection to add to.")
    # an adsk collection is FALSY when empty - count it, never test it for truth.
    before_n = safe(lambda: groups.count, 0) or 0
    try:
        group = groups.add(si, ei)
    except Exception as e:
        return error(f"Grouping timeline items {si}..{ei} failed: {e}")
    if group is None:
        return error(f"Fusion returned no group for timeline items {si}..{ei} - nothing was grouped.")
    after_n = safe(lambda: groups.count, 0) or 0
    if after_n <= before_n:
        return error(f"add() returned a group but timelineGroups still holds {after_n} - the group "
                     "did not land.")
    out = {"grouped": True, "group": safe(lambda: group.name), "start_index": si, "end_index": ei,
           "member_count": safe(lambda: group.count, 0) or 0, "groups": after_n,
           "note": "Remove the group with action='ungroup' - its items are kept."}
    want = (name or "").strip()
    if want:
        try:
            group.name = want
        except Exception as e:
            out["name_warning"] = f"The group was created but renaming it to '{want}' failed: {e}"
        actual = safe(lambda: group.name)
        if actual == want:
            out["group"] = actual
        elif "name_warning" not in out:
            out["name_warning"] = f"The group was created but its name reads '{actual}', not '{want}'."
    return ok(out)


def _do_ungroup(timeline, feature):
    if not feature:
        return error("action='ungroup' needs 'feature' - the name of the timeline group to remove "
                     "(from design_get(include=['timeline'])).")
    groups = safe(lambda: timeline.timelineGroups)
    if groups is None:
        return error("This timeline exposes no timelineGroups collection.")
    n = safe(lambda: groups.count, 0) or 0
    items = [g for g in (safe(lambda i=i: groups.item(i)) for i in range(n)) if g is not None]
    low = feature.lower()
    hits = [g for g in items if (safe(lambda g=g: g.name) or "").lower() == low]
    if not hits:
        return error(f"No timeline group named '{feature}'. Groups: "
                     f"{_sample([_label(g) for g in items]) or '(none)'}.")
    if len(hits) > 1:
        return error(f"'{feature}' names {len(hits)} timeline groups "
                     f"({_sample([_label(g) for g in hits])}) - rename one in Fusion, so the target "
                     "is unambiguous.")
    group = hits[0]
    gname = safe(lambda: group.name) or feature
    try:
        # deleteGroupAndContents=False: the group goes, its items stay in the timeline, expanded.
        did = group.deleteMe(False)
    except Exception as e:
        return error(f"Removing timeline group '{gname}' failed: {e}")
    after = safe(lambda: groups.count, 0) or 0
    if not did:
        return error(f"Fusion declined to remove timeline group '{gname}' (deleteMe returned false); "
                     f"{after} group(s) remain.")
    if after >= n:
        return error(f"deleteMe reported success but timelineGroups still holds {after} - '{gname}' "
                     "was not removed.")
    return ok({"ungrouped": True, "group": gname, "groups": after,
               "note": "The group is gone; the items it held stay in the timeline, expanded. Delete "
                       "an item itself with design_delete_feature."})


def _do_delete_after_marker(timeline, confirm):
    marker = safe(lambda: timeline.markerPosition)
    count = safe(lambda: timeline.count, 0) or 0
    if marker is None:
        return error("Could not read markerPosition, so what lies after the marker is unknown - "
                     "nothing was deleted.")
    if marker >= count:
        return error(f"Nothing lies after the marker: it is at {marker} of {count} (the end of the "
                     "timeline). Roll it back first with action='roll'.")
    after_marker = [o for o in _objects(timeline) if _index(o) >= marker]
    # A collapsed group is ONE entry here and takes every member down with it, so the blast radius
    # is the member count, not the entry count - measured: 5 entries discarded 8 features.
    doomed, features = [], 0
    for o in after_marker:
        n = _member_count(o)
        features += n
        doomed.append(f"{_label(o)} ({n} items)" if n > 1 else _label(o))
    if not confirm:
        return error(f"Refusing: this DISCARDS {features} timeline item(s) after the marker at "
                     f"{marker} of {count} - {_sample(doomed)} - and the features and geometry they "
                     "produced. Pass confirm_delete_after_marker=true to proceed. Nothing was deleted.")
    try:
        did = timeline.deleteAllAfterMarker()
    except Exception as e:
        return error(f"deleteAllAfterMarker failed: {e}")
    after = safe(lambda: timeline.count, 0) or 0
    if not did:
        return error(f"Fusion declined to delete after the marker (returned false); the timeline "
                     f"still holds {after} item(s).")
    if after >= count:
        return error(f"deleteAllAfterMarker reported success but the timeline still holds {after} "
                     f"item(s) of {count} - nothing was discarded.")
    return ok({"deleted_after_marker": True, "deleted": features,
               "deleted_timeline_entries": count - after, "discarded": doomed[:_PREVIEW_MAX],
               "marker_position": marker, "timeline_count": after,
               "note": "Those items and their geometry are gone. Undo in Fusion if unintended - the "
                       "API cannot restore them."})


def _attr_names(group, name):
    """(group, name, error_text) - the attribute's group/name pair, stripped and bounded."""
    g, n = (group or "").strip(), (name or "").strip()
    if not g or not n:
        return None, None, ("An attribute is named by 'attribute_group' plus 'attribute_name' - got "
                            f"attribute_group='{g}', attribute_name='{n}'.")
    for label, text in (("attribute_group", g), ("attribute_name", n)):
        if len(text) > _ATTR_MAX_CHARS:
            return None, None, (f"'{label}' is {len(text)} characters; this tool carries at most "
                                f"{_ATTR_MAX_CHARS}.")
        if text.startswith(_REGEX_PREFIX):
            return None, None, (f"'{label}' starts with '{_REGEX_PREFIX}', which the attribute "
                                "search reads as a regular expression instead of this literal name. "
                                "Name the attribute without that prefix.")
    return g, n, None


def _attr_target(feature):
    """((entity, timeline name), error_text) - the entity the named timeline item wraps. The kind
    refuses an ambiguous name and a timeline group, which carries no feature entity."""
    if not feature:
        return None, ("The attribute actions need 'feature' - the timeline item to tag (from "
                      "design_get(include=['timeline'])).")
    return _ATTR_TARGET.resolve(feature)


def _attributes_of(entity, label):
    """(Attributes, error_text). Fusion exposes no attributes on Timeline or TimelineObject - an
    attribute reached through the timeline lives on the ENTITY the item wraps, which is what this
    reads."""
    attrs = safe(lambda: entity.attributes)
    if attrs is None:
        return None, f"'{label}' carries no attributes collection, so it cannot hold an attribute."
    return attrs, None


def _design_matches(design, group, name):
    """How many attributes in the whole design carry this group/name, or None when the search is
    unreadable. Design.findAttributes hands back an AttributeVector - len()/[i], never
    .count/.item(i)."""
    found = safe(lambda: design.findAttributes(group, name))
    if found is None:
        return None
    return safe(lambda: len(found))


def _do_set_attribute(design, feature, group, name, value):
    if not isinstance(value, str):
        return error(f"'attribute_value' takes a string; got {type(value).__name__}.")
    if len(value) > _ATTR_MAX_CHARS:
        return error(f"'attribute_value' is {len(value)} characters; this tool carries at most "
                     f"{_ATTR_MAX_CHARS}. Store the bulk elsewhere and tag a reference to it.")
    group, name, gerr = _attr_names(group, name)
    if gerr:
        return error(gerr)
    resolved, ferr = _attr_target(feature)
    if ferr:
        return error(ferr)
    entity, label = resolved
    attrs, aerr = _attributes_of(entity, label)
    if aerr:
        return error(aerr)
    # add() on an existing group/name UPDATES that attribute in place rather than adding a second
    # one, so the value it is about to replace is readable only before the call.
    before = safe(lambda: attrs.itemByName(group, name))
    previous = safe(lambda: before.value) if before is not None else None
    try:
        attrs.add(group, name, value)
    except Exception as e:
        return error(f"Attaching attribute '{group}/{name}' to '{label}' failed: {e}")
    back = safe(lambda: attrs.itemByName(group, name))
    if back is None:
        return error(f"'{label}' reads no attribute '{group}/{name}' back after adding it - nothing "
                     "was attached.")
    now = safe(lambda: back.value)
    if now != value:
        return error(f"Attribute '{group}/{name}' on '{label}' reads '{short_ref(now)}' after setting it "
                     f"to '{short_ref(value)}' - the value did not take.")
    out = {"attribute_set": True, "feature": label, "attribute_group": group,
           "attribute_name": name, "value": now, "entity_type": type(entity).__name__,
           "note": ("The attribute is attached to the entity the timeline item wraps, not to the "
                    "timeline item. Remove it with action='delete_attribute'.")}
    if previous is not None:
        out["previous_value"] = previous
    matches = _design_matches(design, group, name)
    if matches is not None:
        out["design_matches"] = matches
    return ok(out)


def _do_delete_attribute(design, feature, group, name):
    group, name, gerr = _attr_names(group, name)
    if gerr:
        return error(gerr)
    resolved, ferr = _attr_target(feature)
    if ferr:
        return error(ferr)
    entity, label = resolved
    attrs, aerr = _attributes_of(entity, label)
    if aerr:
        return error(aerr)
    attr = safe(lambda: attrs.itemByName(group, name), _UNREADABLE)
    if attr is _UNREADABLE:
        # Sentinelled, not swallowed into None: None here is the ABSENT verdict, and a collection
        # that answered nothing at all has not delivered it.
        return error(f"Reading attribute '{group}/{name}' on '{label}' raised, so whether it is "
                     "there cannot be told and nothing was deleted.")
    if attr is None:
        return error(f"'{label}' carries no attribute '{group}/{name}', so nothing was deleted. "
                     "Attach one with action='set_attribute'.")
    # Read the value BEFORE the delete: reading .value on a deleted Attribute raises "An API Object
    # refers to a deleted Object", so a capture taken afterwards is lost.
    old = safe(lambda: attr.value)
    try:
        did = attr.deleteMe()
    except Exception as e:
        return error(f"Deleting attribute '{group}/{name}' from '{label}' failed: {e}")
    # After deleteMe, itemByName reads None and findAttributes reports the drop in the same
    # transaction, so the read-backs below are ground truth - as long as the read-back RAN, which
    # the sentinel is what distinguishes.
    back = safe(lambda: attrs.itemByName(group, name), _UNREADABLE)
    if back is _UNREADABLE:
        return error(f"Deleting attribute '{group}/{name}' from '{label}' returned {bool(did)} but "
                     "reading it back raised, so nothing confirms it is gone - the delete is "
                     "UNCONFIRMED. Run this same delete_attribute call again: a refusal naming "
                     f"'{group}/{name}' as absent is the attribute being gone.")
    if back is not None:
        return error(f"Deleting attribute '{group}/{name}' from '{label}' returned {bool(did)} but "
                     f"itemByName still returns it (value '{short_ref(safe(lambda: back.value))}') - it "
                     "was not deleted.")
    out = {"attribute_deleted": True, "feature": label, "attribute_group": group,
           "attribute_name": name, "deleted_value": old,
           "note": f"'{label}' no longer reads attribute '{group}/{name}' back."}
    if not did:
        out["note"] = (f"deleteMe returned false, but '{label}' no longer reads attribute "
                       f"'{group}/{name}' back - the attribute is gone.")
    matches = _design_matches(design, group, name)
    if matches is not None:
        # design-wide: other entities can carry the same tag, so this is a census, not a failure.
        out["design_matches"] = matches
    return ok(out)


def handler(action: str = "roll", feature: str = "", to: str = "before", end_feature: str = "",
            name: str = "", suppressed: bool = True,
            confirm_delete_after_marker: bool = False, attribute_group: str = "",
            attribute_name: str = "", attribute_value: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    values, verr = _inputs.resolve_inputs([_ACTION, _TO], {"action": action, "to": to})
    if verr:
        return verr
    action, to = values["action"], values["to"]

    design = _common.design()
    if not design:
        return error("No active design (open a document with design geometry).")
    timeline = safe(lambda: design.timeline)
    # Timeline defines __len__, so an EMPTY one reads falsy - test it against None, never for truth.
    if timeline is None:
        return error("This design has no timeline (a direct-modelling design keeps no history), so "
                     "there is no marker to move and nothing to group.")

    feature = (feature or "").strip()
    if action == "roll":
        return _do_roll(timeline, feature, to)
    if action == "suppress":
        return _do_suppress(design, timeline, feature, suppressed)
    if action == "group":
        return _do_group(timeline, feature, (end_feature or "").strip(), name)
    if action == "ungroup":
        return _do_ungroup(timeline, feature)
    if action == "set_attribute":
        return _do_set_attribute(design, feature, attribute_group, attribute_name, attribute_value)
    if action == "delete_attribute":
        return _do_delete_attribute(design, feature, attribute_group, attribute_name)
    return _do_delete_after_marker(timeline, bool(confirm_delete_after_marker))


TOOL_DESCRIPTION = (
    "Drive the parametric timeline. Items after the marker are not computed, so roll to='end' when "
    "done. Names come from design_get(include=['timeline'])."
)

tool = (
    Tool.create_simple(name="design_edit_timeline", description=TOOL_DESCRIPTION)
    .add_input_property(*_ACTION.as_property())
    .add_input_property(*_TO.as_property())
    .add_input_property("feature", {"type": "string",
            "description": "The roll/suppress/attribute target, or a group's first item."})
    .add_input_property("end_feature", {"type": "string",
            "description": "Last item of the group range."})
    .add_input_property("name", {"type": "string",
            "description": "The new group's name."})
    .add_input_property("suppressed", {"type": "boolean",
            "description": "False unsuppresses."})
    .add_input_property("confirm_delete_after_marker", {"type": "boolean",
            "description": "Without it the action previews and refuses."})
    .add_input_property("attribute_group", {"type": "string"})
    .add_input_property("attribute_name", {"type": "string"})
    .add_input_property("attribute_value", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_design_edit_timeline.py::TestVerificationPathsBite"
                      "::test_rollto_that_left_the_item_in_the_wrong_state_is_an_error"))


def register_tool():
    register(item)
