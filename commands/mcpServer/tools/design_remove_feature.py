# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Creates a Remove FEATURE (RemoveFeatures) taking ONE body or component occurrence out of the
design as a timeline step - a feature that can be suppressed or deleted like any other, unlike
design_delete_occurrence / design_delete_feature which erase the item outright. The item's
disappearance is judged by re-scanning the collection it lived in. WRITES.
"""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _inputs
from . import _assert

# RemoveFeatures.add takes ONE item, "a single body (solid or surface) or component occurrence", so
# both kinds are singular, neither is schema-required, and kind="brep" refuses a mesh body with its
# redirect instead of letting it reach add().
_BODY = _inputs.BodyRef("body", kind="brep", required=False)
_OCCURRENCE = _inputs.OccurrenceRef("occurrence", required=False)

# removeFeatures.add() in a DIRECT-modelling design raises "3 : RemoveFeature is not supported in
# Direct Modeling.", so the guard refuses before the mutation.
_MODE_GUARD = _inputs.ModeGuard(
    _inputs.MODE_PARAMETRIC,
    why="A Remove FEATURE is a parametric timeline step - removeFeatures.add() raises "
        "'RemoveFeature is not supported in Direct Modeling.' in direct mode.",
    fix_hint="Switch to parametric mode first (design_set_mode), or erase the item outright with "
             "design_delete_occurrence.")


def _host_component(design, target, kind):
    """Which component's features.removeFeatures the Remove feature is added to: a body's own
    parentComponent, or the component CONTAINING an occurrence (else the root)."""
    if kind == "body":
        return safe(lambda: target.parentComponent)
    parent = safe(lambda: target.assemblyContext)
    if parent is not None:
        return safe(lambda: parent.component)
    return safe(lambda: design.rootComponent)


def _body_census(host, name):
    """How many bodies named `name` the host component's bRepBodies holds RIGHT NOW - the collection
    re-walk is the survivor signal - or None when the collection itself cannot be read. None is not
    "gone" but "the census declined to answer", which may never be published as a verdict."""
    coll = safe(lambda: host.bRepBodies)
    n = safe(lambda: coll.count) if coll is not None else None
    if n is None:
        return None
    return sum(1 for b in _common.iter_collection(coll) if safe(lambda b=b: b.name) == name)


def _occurrence_census(design, path):
    """How many occurrences currently carry `path` as their fullPathName, or None when the walk
    cannot be read. A COUNT, not a lookup: a bare name repeats across sub-assemblies and two siblings
    can even wear one path, so what the before/after diff needs is how many carry it."""
    # root.allOccurrences RAISES on a design holding an unresolved external reference, so the shared
    # walk's component.occurrences fallback is what keeps this census answerable there.
    walk = _common.occurrence_walk(design)
    if not walk.readable:
        return None
    return safe(lambda: sum(1 for o in walk.occurrences if safe(lambda o=o: o.fullPathName) == path))


def _census(design, host, kind, label):
    return _body_census(host, label) if kind == "body" else _occurrence_census(design, label)


def handler(body: str = "", occurrence: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    good_mode, mode_err = _MODE_GUARD.check(design)
    if not good_mode:
        return mode_err

    b_raw = body.strip() if isinstance(body, str) else body
    o_raw = occurrence.strip() if isinstance(occurrence, str) else occurrence
    if b_raw and o_raw:
        return error(f"Provide EITHER 'body' ('{b_raw}') OR 'occurrence' ('{o_raw}') - one Remove "
                     "feature takes one item. Call the tool twice to remove two things.")
    if not b_raw and not o_raw:
        return error("Provide 'body' (a find_geometry handle or a body name) or 'occurrence' (a "
                     "handle or fullPathName from design_get(include=['tree'], tree_handles=true)) "
                     "- the item to remove.")

    if b_raw:
        target, terr = _BODY.resolve(b_raw)
        if terr:
            return error(terr)
        kind = "body"
    else:
        target, terr = _OCCURRENCE.resolve(o_raw)
        if terr:
            return error(terr)
        kind = "occurrence"

    # The census keys on this label, so an unreadable one means the removal could never be judged.
    label = safe(lambda: target.fullPathName) if kind == "occurrence" else safe(lambda: target.name)
    if not label:
        key = "fullPathName" if kind == "occurrence" else "name"
        return error(f"Could not read the {kind}'s {key}, which is what the removal is verified "
                     "against - nothing was changed.")
    host = _host_component(design, target, kind)
    if host is None:
        return error(f"Could not read the component that owns the {kind} to remove - nothing was "
                     "changed.")
    host_name = safe(lambda: host.name)

    feats = safe(lambda: host.features.removeFeatures)
    if feats is None:
        return error(f"Component '{host_name}' exposes no removeFeatures collection - the Remove "
                     "feature is unavailable here.")

    # The census must SEE the target BEFORE the mutation, or its absence afterwards proves nothing.
    # An unreadable census (None) and a target the census cannot see (0) are different failures -
    # reporting "not visible" for a collection that never answered would name the wrong cause.
    n_before = _census(design, host, kind, label)
    if n_before is None:
        collection = ("the root component's allOccurrences" if kind == "occurrence"
                      else f"component '{host_name}' bRepBodies")
        return error(f"Refusing to remove: {collection} - the collection this tool re-scans to "
                     "confirm a removal - could not be read, so the effect could not be verified. "
                     "Nothing was changed.")
    if n_before == 0:
        return error(f"Refusing to remove: the {kind} '{label}' is not visible in the collection this "
                     "tool re-scans to confirm a removal, so the effect could not be verified. Re-read "
                     "the target with design_get(include=['tree'], tree_handles=true) / find_geometry "
                     "and retry.")

    err_before, _, _ = _common.timeline_health(design)
    try:
        feat = feats.add(target)
    except Exception as e:
        return error(f"Remove failed (removeFeatures.add raised): {e}")

    n_after = _census(design, host, kind, label)
    if n_after is None:
        return error(f"removeFeatures.add ran for '{label}', but the collection re-scan that "
                     "confirms it could not be read - the removal may or may not have taken. Check "
                     "with design_get(include=['tree']) before acting on this result.")
    if n_after != n_before - 1:
        return error(f"Remove reported success but '{label}' is still present in "
                     f"'{host_name}' ({n_before} before, {n_after} after) - treat the removal as "
                     "failed.")

    feature_name = safe(lambda: feat.name) if feat else None
    # itemByName over the SAME collection the feature was added to - the feature's own read-back,
    # separate from the item's disappearance.
    feature_found = bool(feature_name and safe(lambda: feats.itemByName(feature_name)) is not None)
    err_after, warn_after, _ = _common.timeline_health(design)

    out = {
        "removed": label,
        "target_kind": kind,
        "component": host_name,
        "feature": feature_name,
        "feature_read_back": feature_found,
        "note": "Remove is a TIMELINE feature - suppress it (design_edit_timeline) or delete it "
        "(design_delete_feature) to bring the item back. Pair with design_get(include=['tree']) to "
        "confirm the assembly.",
    }
    if feature_name is None:
        out["feature_warning"] = (
            "removeFeatures.add returned no feature object, so the Remove feature could not be named "
            f"- but the {kind} is gone from its collection, which is the measured effect. Find the "
            "feature in design_get(include=['timeline']) to suppress or delete it.")
    elif not feature_found:
        out["feature_warning"] = (
            f"The Remove feature reports the name '{feature_name}' but does not resolve back out of "
            "its own collection - name it from design_get(include=['timeline']) instead.")
    # Only the features that errored BETWEEN the two reads belong to this remove - reporting the
    # whole after-list would blame it for errors that were already there.
    new_errors = sorted(set(err_after) - set(err_before))
    if new_errors:
        out["timeline_warning"] = (
            f"These timeline features errored after the remove: {', '.join(new_errors)}. The "
            "removal stands; delete the Remove feature to undo it.")
    elif warn_after:
        out["timeline_warnings"] = warn_after
    return ok(out)


TOOL_DESCRIPTION = (
"Remove ONE body or occurrence as a timeline Remove FEATURE."
)

tool = (
    Tool.create_simple(name="design_remove_feature", description=TOOL_DESCRIPTION)
    .add_input_property(*_BODY.as_property())
    .add_input_property(*_OCCURRENCE.as_property())
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    postconditions=[_assert.FeatureHealthy()],
    # Beside the kernel's health gate: the handler re-scans the collection the item lived in and
    # refuses a target that is still there, which is the removal itself read back.
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_design_remove_feature.py::TestRemoveBody"
                      "::test_a_survivor_after_reported_success_is_an_error"))


def register_tool():
    register(item)
