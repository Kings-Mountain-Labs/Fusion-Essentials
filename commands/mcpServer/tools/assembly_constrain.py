# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Mate two occurrences' geometry via Constrain Components - the relationship type is INFERRED from
the geometry, and a SET of relationships is solved together in ONE constraint. WRITES."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, timeline_health
from . import _common
from . import _inputs
from ._assembly_common import _MOVE_TOL_CM, _constraint_moves, _constraint_positions
# The joint tool's geometry resolver, so assembly_constrain snaps through the same
# '<occurrence>:<snap>' grammar every joint write surface offers.
from ._joint_inputs import _parse_snap, _resolve_snap_entity

app = adsk.core.Application.get()

# healthState on an assembly constraint: 2 = error, 1 = warning - the same pair assembly_get's
# relations slice publishes as healthy:false. The two DIVERGE on an UNREADABLE state: a READ treats
# it as healthy, while this CREATE refuses, since an unconfirmable mutation is an error.
_HS_ERROR, _HS_WARNING = 2, 1


def _newly_unhealthy(before_errors, before_warnings, before_total, design):
    """Timeline features that went unhealthy since the capture - error and warning deltas both
    count, over a walk bounded to `before_total`."""
    # That bound excludes the constraint's own fresh timeline entry, whose healthState RAISES
    # '1 : Unknown exception' right after the add - an error that rolls a script transaction back.
    errors, warnings, _total = timeline_health(design, limit=before_total)
    return ([n for n in errors if n not in before_errors]
            + [n for n in warnings if n not in before_warnings])


def handler(occurrence_one: str = "", occurrence_two: str = "",
            snap_one: str = "", snap_two: str = "", relationships=None,
            offset: float = 0.0, angle_deg: float = 0.0,
            flipped: bool = False, units: str = "mm") -> dict:
    """Constrain two occurrences' geometry: 'relationships' is a list of {snap_one, snap_two,
    flip?, offset?, angle_deg?} geometry pairs added to ONE constraint and solved together;
    snap_one/snap_two is the single-pair shorthand, and no snaps takes the Fusion selection. The
    relationship type is INFERRED from the geometry. WRITES."""
    design = _common.design()
    if not design:
        return error("No active design with components.")

    # Normalize inputs into a list of relationship specs: {snap_one, snap_two, flip, offset, angle}.
    specs = []
    if relationships:
        if not isinstance(relationships, (list, tuple)):
            return error("'relationships' must be a list of {snap_one, snap_two, flip?, offset?}.")
        for i, r in enumerate(relationships):
            if not isinstance(r, dict) or not r.get("snap_one") or not r.get("snap_two"):
                return error(f"relationships[{i}] needs both 'snap_one' and 'snap_two'.")
            specs.append({"snap_one": r["snap_one"], "snap_two": r["snap_two"],
        "flip": bool(r.get("flip", False)),
        "offset": float(r.get("offset", 0.0)),
        "angle_deg": float(r.get("angle_deg", 0.0))})
    elif (snap_one or "").strip() or (snap_two or "").strip():
        specs.append({"snap_one": snap_one, "snap_two": snap_two, "flip": bool(flipped),
        "offset": float(offset or 0.0), "angle_deg": float(angle_deg or 0.0)})

    k = _common.scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Valid: mm, cm, in.")

    try:
        cin = design.rootComponent.assemblyConstraints.createInput()
        rels = cin.geometricRelationships
        names = set()
        # label -> occurrence for the parts this constraint locates. A label whose occurrence could
        # not be re-resolved maps to None and is reported as unmeasured, never as "did not move".
        # The label is the fullPathName, falling back to the caller's string when nothing resolved.
        targets, labels = {}, {}

        if specs:
            # Autonomous snap path - resolve every pair and add it to the SAME constraint input.
            for i, sp in enumerate(specs):
                occ1, sn1 = _parse_snap(sp["snap_one"])
                occ2, sn2 = _parse_snap(sp["snap_two"])
                if not (occ1 and sn1):
                    return error(f"relationships[{i}].snap_one '{sp['snap_one']}' is not a valid "
    "'<occurrence>:<snap>' (snap = center/top/bottom/left/right/front/"
    "back/cylinder/origin).")
                if not (occ2 and sn2):
                    return error(f"relationships[{i}].snap_two '{sp['snap_two']}' is not a valid "
    "'<occurrence>:<snap>'.")
                e1, _k1, err1 = _resolve_snap_entity(design, occ1, sn1)
                if not e1:
                    return error(err1 or f"Could not resolve '{sp['snap_one']}'.")
                e2, _k2, err2 = _resolve_snap_entity(design, occ2, sn2)
                if not e2:
                    return error(err2 or f"Could not resolve '{sp['snap_two']}'.")
                if sp["angle_deg"]:
                    val = adsk.core.ValueInput.createByString(f"{sp['angle_deg']} deg")
                else:
                    val = adsk.core.ValueInput.createByReal(sp["offset"] * k)
                rels.add(e1, e2, sp["flip"], val)
                # _resolve_snap_entity hands back the ENTITY only, so the occurrence whose position is
                # sampled is resolved through the same shared refuse-ambiguity resolver it used
                # internally (_inputs._resolve_occurrence) - not a second matcher.
                for nm in (occ1, occ2):
                    if nm in labels:
                        continue
                    occ = _inputs._resolve_occurrence(nm, nm)[0]
                    lbl = nm
                    if occ is not None:
                        lbl = safe(lambda occ=occ: occ.fullPathName) or safe(lambda occ=occ: occ.name) or nm
                    labels[nm] = lbl
                    names.add(lbl)
                    targets[lbl] = occ
        else:
            # Selection path (no snaps): geometry from the user's current Fusion selection.
            o1, e1 = _inputs._resolve_occurrence(occurrence_one, occurrence_one)
            o2, e2 = _inputs._resolve_occurrence(occurrence_two, occurrence_two)
            if not o1 or not o2:
                return error(e1 if not o1 else e2)
            sel = safe(lambda: app.userInterface.activeSelections)
            sel_count = safe(lambda: sel.count, 0) if sel else 0
            if sel_count < 2:
                return error("Provide 'relationships' or 'snap_one'/'snap_two' ('<occurrence>:"
                              "<snap>') for autonomous geometry, OR select ONE entity on each "
                              "occurrence in Fusion first then call again. "
                              f"(Got {sel_count} selected; need 2.)")
            e1 = safe(lambda: sel.item(0).entity)
            e2 = safe(lambda: sel.item(1).entity)
            if not e1 or not e2:
                return error("Could not read the two selected entities. Re-select and try again.")
            val = (adsk.core.ValueInput.createByString(f"{float(angle_deg)} deg") if angle_deg
                   else adsk.core.ValueInput.createByReal(float(offset or 0.0) * k))
            rels.add(e1, e2, bool(flipped), val)
            for o in (o1, o2):
                lbl = safe(lambda o=o: o.fullPathName) or safe(lambda o=o: o.name)
                names.add(lbl)
                targets[lbl] = o

        if rels.count == 0:
            return error("No relationships to constrain. Provide 'relationships' or snap_one/snap_two.")
        # Sampled BEFORE the add: the add recomputes the assembly, which is both how a part gets
        # located (the point of the tool) and how an EXISTING joint/motion link can break - neither is
        # reportable without a pre-mutation reading of positions and timeline health.
        before_pos = _constraint_positions(targets)
        errors_before, warnings_before, total_before = timeline_health(design)
        constraint = design.rootComponent.assemblyConstraints.add(cin)
    except Exception as e:
        return error(f"Assembly constraint failed: {e}")
    if not constraint:
        return error("Assembly constraint creation returned nothing.")
    name_read = safe(lambda: constraint.name)
    cname = name_read or "the created constraint"
    # The undo names the constraint only when its name was actually read - quoting a placeholder as
    # the 'name' argument would hand the caller a call that resolves nothing.
    undo = ("It REMAINS in the design - remove it with assembly_edit_relations(kind='constraint', "
            + (f"name='{name_read}', action='delete')." if name_read else
               "action='delete') once assembly_get(include=['relations']) names it."))

    # A constraint can be ADDED yet fail to SOLVE (over-constrained/unsatisfiable) - the same platform
    # behavior joint_at_geometry guards.
    hs = safe(lambda: constraint.healthState)
    if hs is None:
        return error(f"Constraint '{cname}' was created but its healthState cannot be read, so "
                     "whether it SOLVED is UNCONFIRMED - nothing here says the parts are located. "
                     f"Read it back with assembly_get(include=['relations']). {undo}")
    if hs in (_HS_ERROR, _HS_WARNING):
        msg = safe(lambda: constraint.errorOrWarningMessage) or ""
        state = "FAILED to solve" if hs == _HS_ERROR else "reports a compute WARNING"
        return error((f"Constraint '{cname}' was created but {state}. " + msg).strip()
                     + f" {undo} Relax or remove one of its relationships.")

    # The add solved, but it can still have broken what the parts already carried. The
    # constraint's OWN timeline entry carries its name, so that name is filtered out as well as the
    # index bounded - the bound alone assumes the entry landed after the pre-add count.
    damaged = [n for n in _newly_unhealthy(errors_before, warnings_before, total_before, design)
               if n != name_read]
    if damaged:
        return error(f"Constraint '{cname}' solved, but adding it left {len(damaged)} existing "
                     f"timeline feature(s) unhealthy: {', '.join(damaged)}. "
                     f"{undo} Deleting it does not restore them automatically - check them with "
                     "assembly_get afterwards.")

    # The COUNT the constraint reports, never the request: an unreadable count publishes null (with
    # the submitted number beside it), so no caller reads the ask back as a measurement.
    count = _common.counted(lambda: constraint.geometricRelationships.count)
    submitted = len(specs) or 1
    # A constraint holding FEWER relationships than were submitted did not land the request: the
    # missing pairs constrain nothing. An unreadable count (None) is not a shortfall and stays the
    # null disclosure below.
    if count is not None and count < submitted:
        return error(f"Constraint '{cname}' was created but holds only {count} of the {submitted} "
                     f"relationship(s) submitted - the missing one(s) constrain nothing, so the "
                     f"parts are not located the way this call describes. "
                     f"{undo} Then re-submit the relationships that must solve together.")
    moves, measured = _constraint_moves(before_pos, targets)
    note = "Components constrained with the relationship set (type inferred from geometry)."
    if moves:
        note += (" Repositioned: "
                 + "; ".join(f"{m['occurrence']} by {m['distance_mm']} mm" for m in moves) + ".")
    elif measured:
        note += (f" NO target occurrence moved - every sampled world transform reads within "
                 f"{round(_MOVE_TOL_CM * 10.0, 3)} mm of its pre-add position, so the constraint "
                 "solved without repositioning a part.")
    else:
        note += (" 'moved' is null - no target occurrence's transform could be read on both sides of "
                 "the add, so whether any part moved is UNKNOWN here (it is not a 'no'). Read the "
                 "positions with assembly_get.")
    if count is None:
        note += (f" 'relationship_count' is null - it could not be read off the constraint; "
                 f"{submitted} relationship(s) were submitted.")
    elif count > submitted:
        # A count ABOVE the request is a surplus, not a shortfall - nothing the caller asked for is
        # missing, so it is disclosed rather than refused.
        note += f" 'relationship_count' reads {count} for the {submitted} relationship(s) submitted."
    return ok({"created": True, "constraint": name_read,
        "relationship_count": count,
        "relationships_submitted": submitted,
        "occurrences": sorted(n for n in names if n),
        "moved": moves if measured else None,
        "note": note})


TOOL_DESCRIPTION = (
    "Constrain occurrences' geometry: the relationship (flush / coincident / concentric / angle) is "
    "INFERRED, and a SET solves together."
)
tool = (
    Tool.create_simple(name="assembly_constrain", description=TOOL_DESCRIPTION)
    .add_input_property("relationships", {"type": "array",
            "description": "{snap_one, snap_two, flip?, offset?, angle_deg?} pairs added to ONE constraint.",
            "items": {"type": "object"}})
    .add_input_property("snap_one", {"type": "string", "description": "One relationship: '<occurrence>:<snap>'."})
    .add_input_property("snap_two", {"type": "string", "description": "The second '<occurrence>:<snap>'."})
    .add_input_property("occurrence_one", {"type": "string"})
    .add_input_property("occurrence_two", {"type": "string"})
    .add_input_property("offset", {"type": "number", "description": "In 'units'."})
    .add_input_property("angle_deg", {"type": "number", "description": "Overrides 'offset' - an angle constraint."})
    .add_input_property("flipped", {"type": "boolean", "description": "true rests mating faces ON each other."})
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler,
    run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_assembly_constrain.py::TestConstraintMovedVerdict"
                      "::test_a_repositioned_part_is_published_with_its_distance"))


def register_tool():
    register(item)
