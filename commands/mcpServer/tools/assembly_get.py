# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""RICH READ: assembly_get - the active assembly's kinematic state as clean JSON: each top-level
occurrence's world position, ground flags, and joints, plus a design-level joint list and health
rollup. Read-only.
"""

import math

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale
from ._assembly_detail import (_MEMBER_CAP, _all_occurrence_rows, _contact_analysis, _contact_rows,
                               _health, _health_fields, _joint_frame, _joint_origin_rows,
                               _limit_facts, _motion_axes, _occ_record, _relation_rows,
                               _unresolved_row, _value_now)
from . import _common
from . import _inputs
from . import _joints
from . import _relations

app = adsk.core.Application.get()

# JointMotion type enum value -> friendly name + degrees of freedom.
_MOTION = {
    0: ("rigid", 0),
    1: ("revolute", 1),
    2: ("slider", 1),
    3: ("cylindrical", 2),
    4: ("pin_slot", 2),
    5: ("planar", 3),
    6: ("ball", 3),
}

# The default cap on each bounded array, read by the handler signature below - which is what the
# schema stamps as each max_* input's structural default.
_MAX_OCCURRENCES_DEFAULT = 50
_MAX_JOINTS_DEFAULT = 100
_MAX_JOINT_ORIGINS_DEFAULT = 50
_MAX_RELATIONS_DEFAULT = 50
_MAX_CONTACTS_DEFAULT = 50
_MAX_ALL_OCCURRENCES_DEFAULT = 100

_SLICES = ("all_occurrences", "joint_origins", "relations", "contacts", "poses")

# What the default occurrence row withholds, and the reads that answer the same question narrowly.
_POSES_NOTE = ("Occurrence rows carry identity, ground flags, body_count and joints. "
               "include=['poses'] adds each one's world origin, x/y/z basis axes and bodies-only "
               "bbox; model_inspect(target='<occurrence>') measures ONE without listing the rest.")


# ONE joint row. The payload's 'frame' key is minted here and its value built in _assembly_detail -
# a frame publisher is classified by the tool module that mints the key (test_frame_disclosure.py).
def _joint_record(design, j, inv_k):
    mt = safe(lambda: j.jointMotion.jointType)
    friendly, dof = _MOTION.get(mt, ("?", None))
    rec = {"name": safe(lambda: j.name), "type": friendly, "dof": dof}
    rec.update(_health_fields(j))
    rec["occurrence_one"] = (safe(lambda: j.occurrenceOne.name)
                             if safe(lambda: j.occurrenceOne) else None)
    rec["occurrence_two"] = (safe(lambda: j.occurrenceTwo.name)
                             if safe(lambda: j.occurrenceTwo) else None)
    # Suppression is DISCLOSED, not folded into healthy - a suppressed joint is inert, not broken.
    # BOTH flags OR'd: Joint.isSuppressed keeps reading False when the suppression was set on the
    # TIMELINE item. read_flag, so two unreadable flags stay unstated.
    sup = (_common.read_flag(lambda: j.isSuppressed) or
           _common.read_flag(lambda: j.timelineObject.isSuppressed))
    if sup:
        rec["is_suppressed"] = True
    rot = _limit_facts(safe(lambda: j.jointMotion.rotationLimits), math.degrees)
    sld = _limit_facts(safe(lambda: j.jointMotion.slideLimits), lambda cm: cm * 10.0)
    if rot:
        rec["rotation_limits_deg"] = rot
    if sld:
        rec["slide_limits_mm"] = sld
    now = _value_now(j)
    if now:
        rec["value_now"] = now
    # The heading the motion itself reports for this joint's DOF - a separate read from the frame
    # below, whose z_axis states the direction a joint OFFSET drives along.
    rec.update(_motion_axes(j, friendly))
    frame = _joint_frame(design, j, inv_k)
    if frame:
        rec["frame"] = frame
    return rec


def _normalize_include(include):
    if include in (None, "", []):
        return []
    if isinstance(include, str):
        return [s.strip().lower() for s in include.split(",") if s.strip()]
    return [str(s).strip().lower() for s in include]


def handler(units: str = "mm", include=None, include_joints: bool = True,
            max_occurrences: int = _MAX_OCCURRENCES_DEFAULT,
            max_joints: int = _MAX_JOINTS_DEFAULT,
            max_joint_origins: int = _MAX_JOINT_ORIGINS_DEFAULT,
            max_relations: int = _MAX_RELATIONS_DEFAULT,
            max_contacts: int = _MAX_CONTACTS_DEFAULT,
            max_all_occurrences: int = _MAX_ALL_OCCURRENCES_DEFAULT) -> dict:
    """See TOOL_DESCRIPTION."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    inv_k = 1.0 / k

    inc = _normalize_include(include)
    bad = [s for s in inc if s not in _SLICES]
    if bad:
        return error(f"Unknown include {bad}. Valid: {', '.join(_SLICES)}.")

    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")
    root = design.rootComponent
    with_poses = "poses" in inc

    # The FULL joint walk: root AND every sub-component, joints AND asBuiltJoints. ALWAYS walked -
    # include_joints gates only what is EMITTED, so joint_count and broken_joints stay honest with
    # it false.
    joints = []
    occ_joints = {}
    for j in _joints.all_joints(design):
        rec = _joint_record(design, j, inv_k)
        joints.append(rec)
        for key in ("occurrence_one", "occurrence_two"):
            nm = rec.get(key)
            if nm:
                occ_joints.setdefault(nm, []).append(rec["name"])

    # Cap the JOINTS array reported to the caller; occ_joints (the cross-index) was built from the
    # FULL walk above, and broken_joints/health below reads the FULL 'joints' list, so capping here
    # only bounds the emitted array - it never hides a health problem.
    joint_total = len(joints)
    cap_j = max(1, int(max_joints))
    joints_out = joints[:cap_j]
    joints_truncated = joint_total > len(joints_out)

    occurrences = []
    grounded_names = []
    for occ in _common.iter_collection(safe(lambda: root.occurrences)):
        # An occurrence whose component will not read answers NOTHING else either (placement, bodies
        # and ground flags all raise), so it gets the flagged row rather than a full record built out
        # of swallowed reads - which would publish a grounded:false, body_count:0 part at the origin.
        is_broken, detail = _common.broken_reference(occ)
        if is_broken:
            occurrences.append(_unresolved_row({"name": safe(lambda o=occ: o.name) or "(unreadable name)",
                                                "parent_path": safe(lambda: root.name),
                                                "detail": detail}))
            continue
        rec = _occ_record(occ, inv_k, occ_joints, include_joints, with_pose=with_poses)
        if rec["grounded"]:
            grounded_names.append(rec["name"])
        occurrences.append(rec)

    # Cap the OCCURRENCES array reported to the caller; occurrence_count/grounded_occurrences below
    # stay computed from the FULL walk, so capping here only bounds the emitted array.
    occ_total = len(occurrences)
    cap_o = max(1, int(max_occurrences))
    occurrences_out = occurrences[:cap_o]
    occurrences_truncated = occ_total > len(occurrences_out)

    # Bodies directly in the ROOT component are NOT occurrences, so the loop above misses them - yet a
    # root body can't be jointed/grounded (it isn't an occurrence). Report it so the kinematic picture
    # isn't silently missing root-level geometry the user built.
    root_bodies = []
    for i, b in enumerate(_common.iter_collection(safe(lambda: root.bRepBodies))):
        root_bodies.append(safe(lambda b=b: b.name) or f"Body{i+1}")

    # HEALTH ROLLUP. `is False` / `.get("health_unknown")`, never truthiness: a row whose compute
    # state NEITHER the entity nor its timeline item answered carries no healthy key at all, and
    # reading that absence as broken raises a false alarm where the row makes no claim.
    broken_joints = [j["name"] for j in joints if j.get("healthy") is False]
    health_unknown_joints = [j["name"] for j in joints if j.get("health_unknown")]
    suppressed_joints = [j["name"] for j in joints if j.get("is_suppressed")]
    # Relation health is folded into the HEADLINE flag, not just the opt-in relations slice: a
    # failed assembly constraint reaches none of broken_joints/timeline_problems.
    broken_relations = []
    for kind in ("rigid_group", "motion_link", "constraint"):
        for rel, _owner in _relations.all_relations(design, kind):
            r_ok, r_msg = _health(rel)
            if r_ok is False:
                broken_relations.append({"kind": kind, "name": safe(lambda rel=rel: rel.name),
                                         "error": r_msg})
    timeline_problems = []
    for o in _common.iter_collection(safe(lambda: design.timeline)):
        healthy, msg = _health(o)
        if healthy is False:
            timeline_problems.append({"name": safe(lambda o=o: o.name), "error": msg})

    # A ROLLED-BACK marker means features after it (downstream joints included) are NOT in the current
    # model - they revert to home while still reading healthy, so the joint state below is INCOMPLETE.
    # markerPosition exposes exactly the state a non-restoring joint_edit once left behind; surface it.
    marker_pos, marker_count = _common.timeline_marker(design)
    rolled_back = bool(marker_pos is not None and marker_count and marker_pos < marker_count)

    # UNRESOLVED EXTERNAL REFERENCES are part of the headline verdict, not an opt-in slice. The
    # census runs once here and feeds both the verdict and the all_occurrences slice below.
    occ_walk = _common.occurrence_walk(design)
    unresolved_references = [{"name": b["name"], "parent_path": b["parent_path"],
                              "detail": b["detail"]} for b in occ_walk.broken]

    is_healthy = (not broken_joints and not timeline_problems and not rolled_back
                  and not broken_relations and not unresolved_references)

    # STALENESS RECONCILIATION: the per-joint healthState can LAG the timeline after an in-place
    # edit that has not been recomputed. When they disagree the timeline is authoritative.
    tl_problem_names = {p["name"] for p in timeline_problems}
    joints_broke_but_timeline_clean = bool(broken_joints) and not timeline_problems
    out = {
    "units": units,
    "is_healthy": is_healthy,
    "broken_joints": broken_joints,
    "broken_relations": broken_relations,
    "unresolved_references": unresolved_references,
    "suppressed_joints": suppressed_joints,
    "timeline_problems": timeline_problems,
    "timeline_rolled_back": rolled_back,
    "occurrence_count": occ_total,
    "grounded_occurrences": grounded_names,
    "joint_count": joint_total,
    "occurrences": occurrences_out,
    "occurrences_truncated": occurrences_truncated,
    "root_bodies": root_bodies,   # bodies directly in root (NOT jointable; promote to a component to joint)
    "joints": joints_out if include_joints else None,
    "joints_truncated": joints_truncated,
    "note": "Structured kinematic state. CHECK is_healthy FIRST - false means a joint, relation or "
    "feature FAILED TO COMPUTE, or the design holds an occurrence whose external reference does "
    "not resolve; broken_joints / broken_relations / timeline_problems / unresolved_references "
    "name them. Then reason about grounding and joint-wiring from these NUMBERS; pair with "
    "view_set(isolate).",
    }
    if not with_poses:
        out["note"] += " " + _POSES_NOTE
    if unresolved_references:
        # The reference's own source document/project/hub is NOT readable: occ.component and
        # occ.documentReference both raise and the ref is absent from Document.documentReferences,
        # so this names what CAN be read - the occurrence and its parent - and stops there.
        out["note"] += (
            f" {len(unresolved_references)} occurrence(s) hold an UNRESOLVED external reference "
            f"({', '.join(sorted({u['name'] for u in unresolved_references}))}): reading their "
            "component raises, so they carry no readable geometry, placement or joints and are "
            "excluded from every position/interference read. The API exposes no path from such an "
            "occurrence to its source file, project or hub - open the browser tree in Fusion and "
            "hover the flagged node for the reason.")

    # all_occurrences slice (opt-in): the same occurrence row for the NESTED instances too. The
    # 'occurrences' array above walks root.occurrences - top-level only - so a part sitting inside a
    # sub-assembly appears nowhere in it, and its position (drifted or not) is unreadable here.
    if "all_occurrences" in inc:
        cap_ao = max(1, int(max_all_occurrences))
        ao_rows, ao_walk = _all_occurrence_rows(occ_walk, inv_k, cap_ao, occ_joints, include_joints,
                                                with_poses)
        ao_total = ao_walk.total
        out["all_occurrences"] = ao_rows
        # null, never 0: a census nothing could be read from is UNKNOWN, and publishing 0 beside
        # all_occurrences_truncated:false is an active claim that the assembly is empty and nothing
        # was lost - measured on a 55-occurrence assembly whose allOccurrences raised.
        out["all_occurrence_count"] = ao_total
        out["occurrences_walk"] = ao_walk.method
        out["all_occurrences_truncated"] = bool(ao_total is not None and ao_total > len(ao_rows))
        if ao_total is None:
            out["note"] += (" all_occurrences could not be enumerated by EITHER walk "
                            "(root.allOccurrences and the component.occurrences fallback both "
                            "failed): all_occurrence_count is null - the census is UNKNOWN, not "
                            "zero, and the rows listed are not a complete set.")
        elif not ao_walk.complete:
            out["note"] += (" The occurrence walk did not complete, so all_occurrence_count is a "
                            "LOWER BOUND on what the design holds.")
        if ao_walk.method == _common.WALK_RECURSED:
            out["note"] += (" occurrences_walk='recursed': root.allOccurrences raised, so the census "
                            "was rebuilt from component.occurrences.")
        if out["all_occurrences_truncated"]:
            out["note"] += (f" all_occurrences was capped at {cap_ao} of {ao_total}; raise "
                            "max_all_occurrences to see the rest.")
    else:
        out["note"] += (" The 'occurrences' array is TOP-LEVEL only; include=['all_occurrences'] "
                        "repeats the same record for every occurrence in the design - nested children "
                        "included - each with its full_path.")

    # joint_origins slice (opt-in): each Joint Origin (WCS frame) as a referenceable, handle-bearing row.
    if "joint_origins" in inc:
        cap_jo = max(1, int(max_joint_origins))
        jo_rows, jo_total = _joint_origin_rows(design, inv_k, cap_jo)
        out["joint_origins"] = jo_rows
        out["joint_origin_count"] = jo_total
        out["joint_origins_truncated"] = jo_total > len(jo_rows)
        if out["joint_origins_truncated"]:
            out["note"] += (f" joint_origins was capped at {cap_jo} of {jo_total}; raise "
                            "max_joint_origins to see the rest.")
    else:
        out["note"] += (" include=['joint_origins'] lists each Joint Origin (a reusable WCS frame) - its "
                        "qualified name + a handle to reference it by (feed joint_create / joint_at_geometry "
                        "/ cam_edit_setup wcs), world position + frame axes, and which joints consume it.")

    # relations slice (opt-in): the maintained relationships that are NOT joints.
    if "relations" in inc:
        cap_r = max(1, int(max_relations))
        rel_rows, rel_totals = _relation_rows(design, cap_r)
        out["relations"] = rel_rows
        out["relation_counts"] = rel_totals
        # BOTH caps feed the flag: the per-kind list cap AND a rigid group's member preview, so
        # relations_truncated never reads false over a row that dropped members.
        list_capped = any(rel_totals[k] > len(rel_rows[k]) for k in rel_rows)
        members_capped = any(r.get("occurrences_truncated") for r in rel_rows["rigid_groups"])
        out["relations_truncated"] = list_capped or members_capped
        if list_capped:
            out["note"] += (f" relations lists were capped at {cap_r}; raise max_relations to see "
                            "the rest (relation_counts holds the true totals).")
        if members_capped:
            out["note"] += (f" A rigid group's members are previewed to {_MEMBER_CAP} - the rows "
                            "flagged occurrences_truncated carry their full count in "
                            "occurrence_count.")
    else:
        out["note"] += (" include=['relations'] lists the maintained relationships that are NOT joints - "
                        "rigid groups (members + suppressed), motion links (the two joints, their values "
                        "and reversed flag), and assembly constraints - each editable by name with "
                        "assembly_edit_relations.")

    # contacts slice (opt-in): the design's contact sets, plus the flags that make them act.
    if "contacts" in inc:
        cap_c = max(1, int(max_contacts))
        contact_rows, contact_total = _contact_rows(design, cap_c)
        # .get: a row whose member list could not be read carries no members_truncated at all.
        members_capped = any(r.get("members_truncated") for r in contact_rows)
        out["contact_analysis"] = _contact_analysis(design)
        out["contacts"] = contact_rows
        out["contact_count"] = contact_total
        out["contacts_truncated"] = contact_total > len(contact_rows) or members_capped
        if contact_total > len(contact_rows):
            out["note"] += (f" contacts was capped at {cap_c} of {contact_total}; raise max_contacts "
                            "to see the rest.")
        if members_capped:
            out["note"] += (f" A contact set's members are previewed to {_MEMBER_CAP} - the rows "
                            "flagged members_truncated carry their full count in member_count.")
        # A list of sets reads as "these are in force"; both flag states that make them do nothing
        # are disclosed beside it, in the same words assembly_edit_contacts uses.
        if contact_total and out["contact_analysis"]["enabled"] is False:
            out["note"] += (" NOTE: contact analysis is OFF for this design, so every contact set "
                            "listed is INERT and 'scope' reads all_bodies regardless; turn it on "
                            "with assembly_edit_contacts action='enable_analysis'.")
        elif (contact_total and out["contact_analysis"]["enabled"] is True
                and out["contact_analysis"]["scope"] == "all_bodies"):
            out["note"] += (" NOTE: contact analysis is ON but scoped to ALL bodies, so the contact "
                            "sets listed are IGNORED until assembly_edit_contacts "
                            "action='set_analysis_scope' with scope='contact_sets'.")
    else:
        out["note"] += (" include=['contacts'] lists the design's contact sets - members, member "
                        "count, suppressed - plus whether contact analysis is enabled and whether it "
                        "uses those sets or all bodies. Edit with assembly_edit_contacts.")

    if rolled_back:
        out["note"] += (f" WARNING: the timeline marker is at {marker_pos}/{marker_count} - features "
                        "AFTER it (downstream joints included) are ROLLED BACK and reverted to home, so "
                        "the joint state here is INCOMPLETE. Run design_recompute (or roll the marker to "
                        "the end) to restore the full model, then re-read.")
    if health_unknown_joints:
        # is_healthy is a verdict over the rows that HAVE one; a row that withheld its flag is not
        # counted broken, so the count it is silent about is said out loud here.
        out["note"] += (
            f" {len(health_unknown_joints)} joint(s) publish NO healthy flag "
            f"({', '.join(health_unknown_joints[:8])}): neither the joint nor its timeline item "
            "answered a compute state, so those rows carry health_unknown:true and is_healthy "
            "makes no claim about them.")
    if joints_broke_but_timeline_clean:
        out["health_may_be_stale"] = True
        out["note"] += (" WARNING: broken_joints is non-empty but the TIMELINE shows no errored feature - "
    "the joint health likely LAGS an uncommitted edit. Run design_recompute, then "
    "re-probe; the timeline (design_get) is authoritative.")
    if include_joints and joints_out:
        out["note"] += (" Each joint row carries value_now - the joint's CURRENT driven value read "
                        "off its motion (angle_deg / slide_mm), so it never has to be derived from "
                        "the parts' basis vectors - and frame, that joint's frame in WORLD "
                        "coordinates, whose z_axis is the direction a joint OFFSET drives along "
                        "(param_set on the joint's offset parameter, or joint_edit offset). Where "
                        "the joint's DOF has a heading, the row adds the vector its MOTION "
                        "reports - rotation_axis (revolute/cylindrical), the axis that DOF turns "
                        "about, and slide_direction (SLIDER ONLY - a cylindrical motion exposes no "
                        "slide direction), the direction it slides along - a separate read from "
                        "frame.z_axis. SPACE: rotation_axis read "
                        "WORLD on a top-level joint (measured); slide_direction's space is "
                        "UNMEASURED, as is either heading on a joint reached through a nested "
                        "instance. No OTHER joint kind carries a heading here: a pin_slot / "
                        "planar / ball row states none because none is read, not because a read "
                        "failed. On the kinds that do, an absent key is a read that answered "
                        "nothing.")
    if root_bodies:
        out["note"] += (" NOTE: root_bodies lists geometry directly in the root component - these are "
                        "NOT occurrences and can't be jointed/grounded; promote one to a component "
                        "(model_create_component) to make it part of the kinematics.")
    if occurrences_truncated:
        out["note"] += (f" occurrences was capped at {cap_o} of {occ_total}; raise max_occurrences to "
                        "see the rest.")
    if joints_truncated:
        out["note"] += (f" joints was capped at {cap_j} of {joint_total}; raise max_joints to see the rest.")
    return ok(out)


TOOL_DESCRIPTION = (
    "Read the active assembly's kinematic state: per top-level occurrence, identity, ground flags, "
    "body count and joints, plus the design's joint list. Check is_healthy first; the note names "
    "the include= slices this call omitted."
)

tool = (
    Tool.create_simple(name="assembly_get", description=TOOL_DESCRIPTION)
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("include", {"type": "array",
            "items": {"type": "string", "enum": list(_SLICES)},
            "description": "Omit for the light kinematic state."})
    .add_input_property("include_joints", {"type": "boolean"})
    .add_input_property("max_occurrences", {"type": "integer"})
    .add_input_property("max_joints", {"type": "integer"})
    .add_input_property("max_joint_origins", {"type": "integer"})
    .add_input_property("max_relations", {"type": "integer"})
    .add_input_property("max_contacts", {"type": "integer"})
    .add_input_property("max_all_occurrences", {"type": "integer"})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
