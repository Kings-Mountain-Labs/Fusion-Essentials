# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""DRIVES a revolute/slider/cylindrical joint to a commanded angle and/or distance (the API's Drive
Joints command), moving the mechanism along that joint's DOF - e.g. swing a revolute to 30 deg, extend
a slider 50 mm. Rigid has no value; ball/planar/pin-slot aren't drivable this way (pose those with
assembly_move). WRITES (mutates part poses); the pose is TRANSIENT until assembly_capture_position
(action='capture') writes it into the timeline - a recompute resets an uncaptured pose.
"""

import math

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe, scale
from . import _common
from . import _geom
from . import _inputs
from . import _write_guard
from .design_move_occurrence import _corner
from ._joints import (DRIVES_ANGLE, DRIVES_ANY, DRIVES_SLIDE, find_joint as _find_joint,
                      current_joint_type as _current_joint_type,
                      motion_link_record as _motion_link_record)

# The bands a member's placement change must EXCEED to count as motion: the placement record
# carries 3 decimals of a millimetre and 4 of a direction component, and a basis quantized that way
# places two orientations under ~0.008 deg apart on the same reading.
_MOVE_BAND_MM = 1e-3
_MOVE_BAND_DEG = 0.01

# rotationValue lands on a 0.1 deg grid, so a stored angle sits at most half a step from the
# command; a read-back inside that half-step landed the value. Measured by measure_api.py row
# joint-revolute-value-tenth-degree-grid; the slide band stays at 1e-3 mm.
_ANGLE_GRID_DEG = 0.1
_ANGLE_BAND_DEG = 0.05

# A command halfway between two grid multiples lands half a step away whichever multiple it takes,
# and half a step is not a binary fraction, so the subtraction can leave a residue just above the
# band. The slack keeps the closest landing the grid allows from reading as a failed drive.
_BAND_SLACK_DEG = 1e-9


def _over_angle_band(diff_deg):
    """Whether an angle difference is further from zero than the half-grid-step band."""
    return abs(diff_deg) - _ANGLE_BAND_DEG > _BAND_SLACK_DEG


# (document identity, joint ENTITY TOKEN) driven this add-in session, keyed by token so a
# delete+recreate clears the block while a rename does not. The refusal it arms, and the crash that
# refusal exists for, are at the point of use in handler().
_driven_this_session = set()


def _carry_driven_entries(old_key, new_key):
    """Re-key this document's driven-joint entries when its document key changes."""
    # Without this the crash guard fails OPEN, the direction that kills Fusion: entries under
    # old_key stop matching, so a joint driven before the save reads as never driven and the
    # both-members refusal does not fire.
    for entry in [e for e in _driven_this_session if e[0] == old_key]:
        _driven_this_session.discard(entry)
        _driven_this_session.add((new_key,) + tuple(entry[1:]))


_write_guard.on_key_renamed(_carry_driven_entries)


def _reg_key(doc_id, joint):
    """Registry key for a driven joint: (doc identity, entityToken), falling back to the joint NAME
    when no token is readable."""
    token = safe(lambda: joint.entityToken)
    return (doc_id, token if token else (safe(lambda: joint.name) or ""))


# What stands in for the document half of a registry key when no document reads at all. Not a
# document either, so it matches no real one.
_NO_DOCUMENT = "<no document>"


def _doc_key():
    """The document half of _reg_key - what tells one document's driven joints from another's."""
    # A NAME cannot serve: every never-saved document answers 'Untitled', so a name key merges two
    # documents' driven-joint sets, and a joint's entityToken is document-local. Merged, the
    # registry refuses a safe drive because a DIFFERENT document's joint was driven.
    key = _write_guard.document_key()
    return _NO_DOCUMENT if key is None else key


def _occ_positively_plain(occ):
    """True only when this occurrence is POSITIVELY confirmed native - no referenced component up
    its assembly-context chain. Any unreadable value is False, keeping the crash guard ON."""
    if occ is None:
        return False
    ref = safe(lambda: occ.isReferencedComponent)
    if ref is None or ref:
        return False
    ctx = safe(lambda: occ.assemblyContext)
    depth = 0
    while ctx is not None and depth < 32:
        r = safe(lambda c=ctx: c.isReferencedComponent)
        if r is None or r:
            return False
        ctx = safe(lambda c=ctx: c.assemblyContext)
        depth += 1
    return True


def _pair_is_plain(j1, j2):
    """Both linked joints wholly native - no xref anywhere in either joint's two occurrences. Only
    such a pair skips the second-member refusal, since every observed crash was xref-context;
    unprovable is False, so the guard stays on."""
    for j in (j1, j2):
        if j is None:
            return False
        if not (_occ_positively_plain(safe(lambda jj=j: jj.occurrenceOne))
                and _occ_positively_plain(safe(lambda jj=j: jj.occurrenceTwo))):
            return False
    return True


def _current_value_text(jm, jtype):
    """The joint's current driven value as display text ('12.5 mm' / '30.0 deg'), or None when no
    DOF of this kind answered - a caller renders that None through _value_clause."""
    parts = []
    if jtype in DRIVES_ANGLE:
        rv = safe(lambda: jm.rotationValue)
        if rv is not None:
            parts.append(f"{round(math.degrees(rv), 4)} deg")
    if jtype in DRIVES_SLIDE:
        sv = safe(lambda: jm.slideValue)
        if sv is not None:
            parts.append(f"{round(sv * 10.0, 4)} mm")
    return ", ".join(parts) or None


def _value_clause(name, jm, jtype, verb="reads"):
    """One joint's driven value as a clause - "'JawR' reads 12.5 mm" - or the sentence saying the
    value did not read. The ONE home for that negative, so no wire string states a reading for a DOF
    that answered nothing."""
    text = _current_value_text(jm, jtype)
    return f"'{name}' {verb} {text}" if text else f"the current value of '{name}' did not read"


# joint type -> (its driven-value property, its bounding JointLimits property, a display formatter).
# Only the ONE-DOF types: a cylindrical joint drives two values, so which one a motion link couples
# is not established by the joint's type alone, and every scaled claim below is withheld for it.
_ONE_DOF = {
    "revolute": ("rotationValue", "rotationLimits", lambda v: f"{round(math.degrees(v), 4)} deg"),
    "slider": ("slideValue", "slideLimits", lambda v: f"{round(v * 10.0, 4)} mm"),
}


def _link_couples(link):
    """Whether a motion_link_record describes a link that TRANSMITS motion, as a TRI-STATE: False
    when it names no partner or reads SUPPRESSED or BROKEN, True when a partner is named and both
    states read clean, None when a state could not be read at all."""
    if link["linked"] is not True:
        return False if link["linked"] is False else None
    if link["suppressed"] is True or link["broken"] is True:
        return False
    if link["suppressed"] is None or link["broken"] is None:
        return None
    return True


def _link_state_text(link):
    """What a link's OWN state read, as a VERB clause the wire strings slot behind 'the link' or
    'which': the flagged states when any is set, else the ones that gave no answer, else '' for a
    link that read clean on both."""
    flagged = ((["SUPPRESSED"] if link["suppressed"] is True else [])
               + (["carrying a compute failure"] if link["broken"] is True else []))
    if flagged:
        return "reads " + " and ".join(flagged)
    unread = ((["suppression"] if link["suppressed"] is None else [])
              + (["compute state"] if link["broken"] is None else []))
    if not unread:
        return ""
    if len(unread) == 2:
        return "answered neither its suppression nor its compute state"
    return f"did not answer its {unread[0]}"


def _enabled_bounds(limits, fmt):
    """['min X', 'max Y'] for the ENABLED bounds of one JointLimits, rendered by fmt. A DISABLED
    bound is left out - it constrains no value. Empty when the object is absent or nothing reads."""
    if limits is None:
        return []
    out = []
    if bool(safe(lambda: limits.isMinimumValueEnabled, False)):
        lo = safe(lambda: limits.minimumValue)
        if lo is not None:
            out.append(f"min {fmt(lo)}")
    if bool(safe(lambda: limits.isMaximumValueEnabled, False)):
        hi = safe(lambda: limits.maximumValue)
        if hi is not None:
            out.append(f"max {fmt(hi)}")
    return out


def _limits_text(jm, jtype):
    """One joint's ENABLED limits as display text ('min 0.0 deg, max 120.0 deg'), or None when none
    are enabled or nothing read."""
    parts = []
    if jtype in DRIVES_ANGLE:
        parts += _enabled_bounds(safe(lambda: jm.rotationLimits),
                                 lambda v: f"{round(math.degrees(v), 4)} deg")
    if jtype in DRIVES_SLIDE:
        parts += _enabled_bounds(safe(lambda: jm.slideLimits),
                                 lambda v: f"{round(v * 10.0, 4)} mm")
    return ", ".join(parts) or None


def _ground_lock_census(design):
    """The DESIGN-WIDE ground_to_parent census as (locked fullPathNames, unanswered, total,
    complete). `total` is None when neither walk enumerated; `complete` False makes every count a
    LOWER BOUND; `unanswered` counts the rows whose flag gave no answer."""
    # ground_to_parent can be set on a NESTED instance, so a root-only collection read would leave
    # a nested lock invisible.
    walk = _common.occurrence_walk(design)
    total = walk.total
    if total is None:
        return [], 0, None, False
    locked, unanswered = [], 0
    for o in list(walk.occurrences) + list(walk.broken_occurrences):
        flag = _common.read_flag(lambda o=o: o.isGroundToParent)
        if flag is None:
            unanswered += 1
        elif flag:
            locked.append(safe(lambda o=o: o.fullPathName)
                          or safe(lambda o=o: o.name) or "(unnamed occurrence)")
    return locked, unanswered, total, walk.complete


def _limit_refusal(limits, value, fmt):
    """The refusal when 'value' (native rad / cm) lies STRICTLY beyond an enabled limit, else
    None; fmt renders a native value in display units."""
    # Fusion IGNORES an out-of-range drive rather than clamping - the value simply stays where it
    # was - so the only honest receipt is a refusal BEFORE the assignment.
    if limits is None:
        return None
    lo_on = bool(safe(lambda: limits.isMinimumValueEnabled, False))
    hi_on = bool(safe(lambda: limits.isMaximumValueEnabled, False))
    lo = safe(lambda: limits.minimumValue)
    hi = safe(lambda: limits.maximumValue)
    if lo_on and lo is not None and value < lo - 1e-9:
        return f"{fmt(value)} is below the enabled minimum {fmt(lo)}"
    if hi_on and hi is not None and value > hi + 1e-9:
        return f"{fmt(value)} is above the enabled maximum {fmt(hi)}"
    return None


def _partner_limit_cause(link, partner_joint, jtype, jm, rad, cm):
    """The clause reporting that the link's RECORDED ratio puts the partner beyond an enabled bound
    of its own, or None when the reads do not establish that."""
    # ARITHMETIC ON READ VALUES, not an observed coupling: implied = the partner's current value
    # plus this joint's commanded change, scaled by value_partner / value_self and signed by
    # isReversed. Any unread term withholds the clause; every number used is published.
    if partner_joint is None or _link_couples(link) is not True:
        return None
    spec, p_spec = _ONE_DOF.get(jtype), _ONE_DOF.get(_current_joint_type(partner_joint))
    if spec is None or p_spec is None:
        return None
    p_jm = safe(lambda: partner_joint.jointMotion)
    if p_jm is None:
        return None
    v_self, v_partner, rev = link["value_self"], link["value_partner"], link["reversed"]
    if not v_self or v_partner is None or rev is None:
        return None
    commanded = rad if jtype == "revolute" else cm
    now = _common.measured(lambda: getattr(jm, spec[0]))
    p_now = _common.measured(lambda: getattr(p_jm, p_spec[0]))
    if commanded is None or now is None or p_now is None:
        return None
    implied = p_now + (commanded - now) * (v_partner / v_self) * (-1.0 if rev else 1.0)
    beyond = _limit_refusal(safe(lambda: getattr(p_jm, p_spec[1])), implied, p_spec[2])
    if not beyond:
        return None
    return (f"'{link['partner']}' is coupled to it by motion link "
            f"'{link['link'] or '(unnamed link)'}', whose recorded values are {v_self} : "
            f"{v_partner} in Fusion's internal units (radians / cm)"
            f"{', reversed' if rev else ''}. Applying that RATIO to this command implies a value "
            f"for '{link['partner']}' its own enabled limits exclude - {beyond}. That is "
            f"arithmetic on the values the link records, not a coupling this receipt observed - "
            f"what the link did to '{link['partner']}' was not read here. Read "
            f"'{link['partner']}' back with assembly_get to check it; if that limit is the bound "
            f"in the way, widen it with joint_edit or command a value the ratio keeps inside it.")


def _placement(occ):
    """One member's placement sample in MILLIMETRES, through the shared occurrence-placement record:
    'origin' is its transform2 translation, x_axis/y_axis/z_axis that transform's rotation basis.
    None when the joint carries no occurrence on that side; an empty dict when nothing read."""
    if occ is None:
        return None
    return _geom.occ_world_frame(occ, 10.0)      # cm -> mm


def _delta_mm(before, after):
    """[dx, dy, dz] in mm between two placement samples, or None when either origin is unreadable -
    so an unread placement is never published as a zero move."""
    a, b = (before or {}).get("origin"), (after or {}).get("origin")
    if a is None or b is None:
        return None
    return [round(q - p, 4) for p, q in zip(a, b)]


def _unit(v):
    """A basis axis rescaled to length 1, or None when it has no length."""
    # The published axes are ROUNDED to 4 decimals, leaving them slightly off unit length, and the
    # angle read below turns that missing length into rotation that never happened.
    n = math.sqrt(sum(c * c for c in v))
    return [c / n for c in v] if n else None


def _delta_deg(before, after):
    """The angle in DEGREES between two samples' orientations, or None when either is missing a
    basis axis. Magnitude only - a sense needs a rotation axis these samples do not establish."""
    keys = ("x_axis", "y_axis", "z_axis")
    if not before or not after or any(k not in before or k not in after for k in keys):
        return None
    trace = 0.0
    for k in keys:
        p, q = _unit(before[k]), _unit(after[k])
        if p is None or q is None:
            return None
        trace += sum(a * b for a, b in zip(p, q))
    return round(math.degrees(math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0)))), 4)


def _moved_rows(members):
    """(rows, readable) over [(occurrence, before-sample, before-corner)]: one row per member whose
    placement changed by MORE than its band, farthest first, with 'geometry_moved_mm' (its own body
    corner's travel) when that corner read on both sides. `readable` says at least one placement
    read at both ends - what tells 'nothing moved' apart from 'the move could not be measured'."""
    rows, readable = [], False
    for occ, before, corner_before in members:
        after = _placement(occ)
        dmm, ddeg = _delta_mm(before, after), _delta_deg(before, after)
        if dmm is None and ddeg is None:
            continue
        readable = True
        span = math.sqrt(sum(c * c for c in dmm)) if dmm is not None else 0.0
        turn = ddeg if ddeg is not None else 0.0
        if span <= _MOVE_BAND_MM and turn <= _MOVE_BAND_DEG:
            continue
        row = {"occurrence": (safe(lambda o=occ: o.fullPathName)
                              or safe(lambda o=occ: o.name))}
        if dmm is not None:
            row["delta_mm"] = dmm
        if ddeg is not None:
            row["delta_deg"] = ddeg
        corner_after = _corner(occ) if corner_before is not None else None
        if corner_after is not None:
            row["geometry_moved_mm"] = round(
                max(abs(a - b) for a, b in zip(corner_before, corner_after)) * 10.0, 4)
        rows.append((span, turn, row))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [r[2] for r in rows], readable


def _off_grid_deg(angle):
    """True when a commanded angle is not a multiple of the grid rotationValue stores on."""
    steps = float(angle) / _ANGLE_GRID_DEG
    return abs(steps - round(steps)) > 1e-6


def _value_move(before, after, unit):
    """What the joint's OWN driven value did across this drive, as (moved, clause): True when the
    published value CHANGED, False when it did not, None when the pre-drive value did not read.
    The two are compared at the 4 decimals the receipt publishes them at."""
    if before is None or after is None:
        return None, "has no readable pre-drive value here, so whether it moved is not known"
    delta = round(after - before, 4)
    if delta == 0.0:
        return False, f"did not move at all (before and after both read {before} {unit})"
    return True, f"moved from {before} {unit} to {after} {unit} (a change of {delta} {unit})"


def handler(joint_name: str = "", angle_deg=None, distance=None, units: str = "mm") -> dict:
    """See TOOL_DESCRIPTION."""
    if angle_deg is None and distance is None:
        return error("Provide 'angle_deg' (revolute/cylindrical) and/or 'distance' (slider/cylindrical) "
                     "to drive the joint to.")
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")

    design = _common.design()
    if not design:
        return error("No active design with components.")
    joint, ambiguous = _find_joint(design, joint_name)
    if ambiguous:
        return error(ambiguous)
    if not joint:
        return error(f"No joint named '{joint_name}'. Use assembly_get or design_get(include=['timeline']) to list "
                     "joint names.")

    jtype = _current_joint_type(joint)
    if jtype not in DRIVES_ANY:
        return error(f"Joint '{joint_name}' is {jtype or 'an unknown type'} - only revolute, slider, and "
                     "cylindrical joints can be driven by value. (rigid has no value; for a ball joint "
                     "pose the part with assembly_move.)")

    # Validate the caller gave the value(s) the joint actually has.
    if angle_deg is not None and jtype not in DRIVES_ANGLE:
        return error(f"Joint '{joint_name}' is a slider - it has no rotation. Use 'distance', not 'angle_deg'.")
    if distance is not None and jtype not in DRIVES_SLIDE:
        return error(f"Joint '{joint_name}' is a revolute - it has no slide. Use 'angle_deg', not 'distance'.")

    jm = safe(lambda: joint.jointMotion)
    if jm is None:
        return error(f"Could not read the motion of joint '{joint_name}'.")

    # Driving BOTH members of a linked pair in an xref assembly KILLED Fusion 2705.1.4, so a pair
    # not provably plain whose partner was already driven refuses BEFORE mutating (a SUPPRESSED or
    # BROKEN link arms none). Re-probe next major: drive both members in a scratch xref assembly.
    doc_id = _doc_key()
    resolved_name = safe(lambda: joint.name) or joint_name
    link = _motion_link_record(joint)
    partner = link["partner"]
    couples = _link_couples(link)
    # A partner name SEVERAL joints carry resolves to None here (find_joint refuses it), so the
    # already-driven check below simply has no partner to key on - it does not block this drive.
    partner_joint = (_find_joint(design, partner)[0] if partner else None)
    partner_driven = bool(partner_joint and _reg_key(doc_id, partner_joint) in _driven_this_session)
    plain_pair = _pair_is_plain(joint, partner_joint) if partner_joint else True
    if partner_driven and not plain_pair and couples is not False:
        # What was READ: the link's two states and this joint's current value. Whether the
        # partner's drive moved THIS joint is a comparison no read here makes.
        moved_claim = (f"The link reads neither suppressed nor compute-failed, and "
                       f"{_value_clause(resolved_name, jm, jtype, 'now reads')} - whether the "
                       f"partner's drive moved it is not read here. Read it back with assembly_get "
                       f"rather than re-driving it. ")
        if couples is None:
            moved_claim = (f"The link {_link_state_text(link)}, so whether it moved "
                           f"'{resolved_name}' is not known here - "
                           f"{_value_clause(resolved_name, jm, jtype, 'now reads')}; read it back "
                           f"with assembly_get. The refusal stands on that unread state, not on a "
                           f"coupling that was observed. ")
        # _pair_is_plain SHORT-CIRCUITS at the first occurrence that is not positively native, so
        # the refusal claims only the reading it has - the pair did not read as wholly native -
        # with all four outcomes that produce it, rather than an xref context no read established.
        return error(
            f"Refused: '{resolved_name}' is motion-linked to '{partner}', already driven this "
            f"session, and the pair did NOT read as wholly native - an occurrence of one joint "
            f"reads as a REFERENCED component, sits under one, did not answer "
            f"isReferencedComponent, or did not read as an occurrence at all. Driving BOTH members "
            f"of a linked pair has killed the Fusion process. "
            + moved_claim + f"Rebuilding '{partner}' (a new token) clears this refusal.")

    applied = {}
    # Out-of-range commands are REFUSED before ANY assignment, since Fusion IGNORES a beyond-limit
    # drive; checking BOTH values first keeps a cylindrical drive from half-applying.
    refusals = []
    rad = cm = None
    if angle_deg is not None:
        rad = math.radians(float(angle_deg))
        r = _limit_refusal(safe(lambda: jm.rotationLimits), rad,
                           lambda v: f"{round(math.degrees(v), 4)} deg")
        if r:
            refusals.append("angle " + r)
    if distance is not None:
        cm = float(distance) * k
        r = _limit_refusal(safe(lambda: jm.slideLimits), cm,
                           lambda v: f"{round(v * 10.0, 4)} mm")
        if r:
            refusals.append("slide " + r)
    if refusals:
        return error(
            f"Refused: the command lies beyond the enabled joint limits of '{resolved_name}' - "
            + "; ".join(refusals) + ". Fusion IGNORES an out-of-range drive (the value stays "
            "where it was), so nothing would move. Command a value inside the limits (a command "
            "exactly AT a bound lands on it), or widen them with joint_edit.")
    # The PRE-drive values, for the two read-back verdicts below: only a before-value tells a
    # read-back matching the command modulo 360 from a mechanism that already sat there, and a
    # mismatch is reported WITH the move the value made.
    rv_before = safe(lambda: jm.rotationValue) if jtype in DRIVES_ANGLE else None
    sv_before = safe(lambda: jm.slideValue) if jtype in DRIVES_SLIDE else None
    # The pre-drive placement of BOTH members, plus the joint's motion vector per commanded DOF:
    # re-sampling those placements after the drive is what says WHICH member was displaced.
    occ_one, occ_two = safe(lambda: joint.occurrenceOne), safe(lambda: joint.occurrenceTwo)
    before_one, before_two = _placement(occ_one), _placement(occ_two)
    # The placement is the transform's CLAIM; each member's body corner is the EVIDENCE.
    corner_one, corner_two = _corner(occ_one), _corner(occ_two)
    directions = {}
    if cm is not None:
        slide_dir = _geom.axis_vec(safe(lambda: jm.slideDirectionVector))
        if slide_dir is not None:
            directions["slide_direction"] = slide_dir
    if rad is not None:
        rot_axis = _geom.axis_vec(safe(lambda: jm.rotationAxisVector))
        if rot_axis is not None:
            directions["rotation_axis"] = rot_axis
    try:
        if rad is not None:
            jm.rotationValue = rad
            applied["angle_deg"] = round(float(angle_deg), 6)
        if cm is not None:
            jm.slideValue = cm
            applied["distance"] = round(float(distance), 6)
    except Exception as e:
        # A cylindrical drive can land its rotation and then fail on the slide: the earlier
        # assignment was ACCEPTED, so the session guard registers the attempt rather than failing
        # open on exactly the partial-drive sequence it exists for.
        if applied:
            _driven_this_session.add(_reg_key(doc_id, joint))
            return error(f"Could not drive joint '{joint_name}': {e}. The assignments made before "
                         f"the failure ({applied}) were accepted; no value was read back here, so "
                         "where the mechanism stands now is not known from this receipt. Read the "
                         "pose back with assembly_get.")
        return error(f"Could not drive joint '{joint_name}': {e}")

    # Read the values back off the joint so the caller sees what actually took (the joint may clamp).
    read_back = {}
    if jtype in DRIVES_ANGLE:
        rv = safe(lambda: jm.rotationValue)
        if rv is not None:
            acc = round(math.degrees(rv), 4)
            read_back["angle_deg"] = acc
            # A revolute keeps the full turns it was commanded rather than normalizing, and no view
            # of the mechanism tells such an angle from its mod-360 form, so publish both.
            norm = round(acc % 360.0, 4)
            if abs(acc - norm) > 1e-9:
                read_back["angle_deg_normalized"] = norm
    if jtype in DRIVES_SLIDE:
        sv = safe(lambda: jm.slideValue)
        if sv is not None:
            read_back["distance_mm"] = round(sv * 10.0, 4)   # cm -> mm

    result = {
        "driven": True,
        "joint": safe(lambda: joint.name),
        "joint_type": jtype,
        "applied": applied,
        "value_now": read_back,
        "units": units,
        "note": "Joint driven (the Drive Joints command). This pose is TRANSIENT: a recompute "
                "resets it unless captured, so call assembly_capture_position (action='capture') "
                "to write it into the timeline as a Position marker; driving again after a capture "
                "arms a NEW pending snapshot. There is no parameter for a slide/rotation VALUE - "
                "the 'offset' param moves the frame Z instead.",
    }
    # value_now verify gate: a drive the mechanism silently ignored reads back its pre-drive value.
    # Limits cannot explain a mismatch here - an out-of-range command was already refused above.
    mismatched = []
    # What each MISSED value did anyway, from its own before/after pair: the tri-states decide the
    # verdict's wording, the clauses publish the numbers behind it.
    moves, move_clauses = [], []
    angle_landed = slide_landed = None      # per-value outcome, for the PARTIAL diagnosis below
    if "angle_deg" in applied and "angle_deg" in read_back:
        angle_landed = True
        if _over_angle_band(read_back["angle_deg"] - applied["angle_deg"]):
            # 720 and 0 are the SAME physical pose: a command the read-back matches modulo 360 is
            # an equivalent pose, not a failed drive - the stored value kept a full-turn count the
            # command did not. Only a mismatch that survives the mod-360 test is a genuine no-take.
            d = abs(read_back["angle_deg"] - applied["angle_deg"]) % 360.0
            if not _over_angle_band(min(d, 360.0 - d)):
                before_deg = round(math.degrees(rv_before), 4) if rv_before is not None else None
                acc_txt = (f"value_now reads {read_back['angle_deg']} deg"
                           + (f" (= {read_back['angle_deg_normalized']} deg normalized)"
                              if "angle_deg_normalized" in read_back else ""))
                if (before_deg is not None
                        and _over_angle_band(read_back["angle_deg"] - before_deg)):
                    # The value CHANGED: the drive moved the mechanism and landed pose-equivalent
                    # to the command. A real move, not a no-op - say so instead of claiming the
                    # pose was already there.
                    result["note"] += (
                        f" NOTE: the drive moved the mechanism to the commanded pose; {acc_txt} - "
                        "the stored angle kept a full-turn count the command did not.")
                elif before_deg is None:
                    result["equivalent_pose"] = True
                    result["note"] += (
                        f" NOTE: {acc_txt}, pose-equivalent to the command (modulo 360 deg); the "
                        "pre-drive value was unreadable, so whether the mechanism moved is not "
                        "known from this receipt.")
                else:
                    result["equivalent_pose"] = True
                    result["note"] += (
                        f" NOTE: the commanded angle equals the current pose modulo 360 deg - "
                        f"{acc_txt}, so the physical pose already matches the command and nothing "
                        "moved. A whole number of turns is an equivalent pose: command an angle "
                        "that is not a multiple of 360 deg to move the mechanism.")
            else:
                angle_landed = False
                mismatched.append(
                    f"angle {read_back['angle_deg']} deg vs commanded {applied['angle_deg']} deg "
                    f"(a residual of "
                    f"{round(read_back['angle_deg'] - applied['angle_deg'], 4)} deg)")
                moved, clause = _value_move(
                    round(math.degrees(rv_before), 4) if rv_before is not None else None,
                    read_back["angle_deg"], "deg")
                moves.append(moved)
                move_clauses.append(f"the angle {clause}")
    if "distance" in applied and "distance_mm" in read_back:
        slide_landed = True
        exp_mm = round(float(applied["distance"]) * k * 10.0, 4)
        if abs(read_back["distance_mm"] - exp_mm) > 1e-3:
            slide_landed = False
            mismatched.append(
                f"slide {read_back['distance_mm']} mm vs commanded {exp_mm} mm "
                f"(a residual of {round(read_back['distance_mm'] - exp_mm, 4)} mm)")
            moved, clause = _value_move(
                round(sv_before * 10.0, 4) if sv_before is not None else None,
                read_back["distance_mm"], "mm")
            moves.append(moved)
            move_clauses.append(f"the slide {clause}")
    if mismatched:
        # A detected no-take is a FAILED drive - isError, never ok (a success whose effect
        # did not land is the cardinal sin). The guard still registers the attempt: the
        # assignment was accepted, so fail toward refusal for the xref pair crash guard.
        _driven_this_session.add(_reg_key(doc_id, joint))
        landed_bits = []
        if angle_landed:
            landed_bits.append(f"angle landed at {read_back['angle_deg']} deg")
        if slide_landed:
            landed_bits.append(f"slide landed at {read_back['distance_mm']} mm")
        moved_at_all = any(m is True for m in moves)
        if landed_bits:
            # One value read back AT the command, so whatever held the other did not hold this one
            # and no frozen-chain diagnosis is elected. Nothing here reads the motion-link PARTNER.
            return error(
                f"PARTIAL drive of '{resolved_name}': " + ", ".join(landed_bits) + "; "
                + "; ".join(mismatched) + " did NOT land the commanded value - "
                + "; ".join(move_clauses) + ". Read the pose back with assembly_get.")
        # A total no-take publishes OBSERVATIONS and elects a cause only where the reads prove one:
        # a parent-locked occurrence EXISTING is no proof it held this drive.
        locked, unanswered, census, census_whole = _ground_lock_census(design)
        if census is None:
            seen = ["the design's occurrence census did not read, so no ground_to_parent state "
                    "was seen"]
        else:
            # The scope word follows the walk: a census that stopped early asked only the rows it
            # reached, so a design-wide negative would cover a subtree nothing looked in. The count
            # is a lower bound for the same reason.
            if locked:
                seen = [f"ground_to_parent is SET on {_common.named_with_remainder(locked)}"]
            elif census_whole:
                seen = ["no occurrence in the design reads ground_to_parent set"]
            else:
                seen = ["no occurrence the walk reached reads ground_to_parent set"]
            if unanswered:
                seen.append(f"{unanswered} of {census} occurrences did not answer "
                            "ground_to_parent")
            if not census_whole:
                seen.append("the occurrence walk did not run to the end (a collection would not "
                            "enumerate, or a depth/node cap was hit), so part of the design was "
                            "not scanned and these ground_to_parent readings cover only the rows "
                            "it reached")
        if link["linked"] is True:
            state = _link_state_text(link)
            seen.append(f"'{resolved_name}' is motion-linked to '{partner}' by "
                        f"'{link['link'] or '(unnamed link)'}'"
                        + (f", which {state}" if state else ""))
            p_jm = safe(lambda: partner_joint.jointMotion) if partner_joint else None
            if p_jm is not None:
                p_type = _current_joint_type(partner_joint)
                p_limits = _limits_text(p_jm, p_type)
                seen.append(_value_clause(partner, p_jm, p_type) + ", "
                            + (f"enabled limits {p_limits}" if p_limits
                               else "with no enabled limits"))
        elif link["linked"] is False:
            seen.append(f"'{resolved_name}' is in no motion link")
        else:
            seen.append(f"the motion-link membership of '{resolved_name}' did not read")
        cause = _partner_limit_cause(link, partner_joint, jtype, jm, rad, cm)
        if cause:
            verdict = " " + cause
        elif link["linked"] is False and locked and not moved_at_all:
            # A frozen chain is the only shape this candidate describes, so a value that MOVED
            # across the drive takes it off the table - the observation still stands in `seen`.
            verdict = (" With no motion link on this joint, a parent-locked member is the CANDIDATE "
                       "cause - release it with assembly_ground(ground_to_parent=false) and re-drive "
                       "to test it.")
        else:
            verdict = (" These observations do not single out a cause. Read the mechanism with "
                       "assembly_get (per-occurrence ground_to_parent, and the joint limits of every "
                       "joint in the chain), then re-drive.")
        # The headline states which of the three this joint's OWN before/after pair shows. 'DID NOT
        # TAKE' is reserved for a value that never moved: on one that moved it would read as a
        # frozen chain its own two reads contradict.
        if moved_at_all:
            headline = (f"Drive of '{resolved_name}' MOVED the joint but did NOT land the command - "
                        f"value_now reads " + "; ".join(mismatched) + "; "
                        + "; ".join(move_clauses) + ".")
        elif any(m is None for m in moves):
            headline = (f"Drive of '{resolved_name}' did NOT land the command - value_now reads "
                        + "; ".join(mismatched) + "; " + "; ".join(move_clauses) + ".")
        else:
            headline = (f"Drive of '{resolved_name}' DID NOT TAKE - value_now reads "
                        + "; ".join(mismatched) + "; " + "; ".join(move_clauses) + ".")
        return error(headline + " Observed: " + "; ".join(seen) + "." + verdict)
    if (applied.get("angle_deg") is not None and "angle_deg" in read_back
            and _off_grid_deg(applied["angle_deg"])):
        result["note"] += (f" NOTE: {applied['angle_deg']} deg is off the {_ANGLE_GRID_DEG} deg "
                           f"grid rotationValue stores on; value_now reads "
                           f"{read_back['angle_deg']} deg.")
    # WHICH member the drive displaced, from the placement samples taken either side of it. This is
    # an observation, never a prediction: the rows name the occurrence that moved and its measured
    # change, and a drive after which neither placement changed says exactly that.
    rows, placement_readable = _moved_rows(((occ_one, before_one, corner_one),
                                            (occ_two, before_two, corner_two)))
    # A drive with no angle commanded TRANSLATES its moved member whole, so a placement that moved
    # over a body corner that stayed is a claim the geometry contradicts - an error, not a pose.
    if rad is None:
        for row in rows:
            carried = row.get("geometry_moved_mm")
            if carried is not None and carried <= _MOVE_BAND_MM:
                _driven_this_session.add(_reg_key(doc_id, joint))
                return error(
                    f"Drive of '{resolved_name}' moved the placement of '{row['occurrence']}' by "
                    f"{row.get('delta_mm')} mm but its body geometry did not move "
                    f"({carried} mm) - the transform is a claim, the body corner is the evidence. "
                    "Read the pose back with assembly_get.")
    if rows:
        result["moved"] = rows[0]
        if len(rows) > 1:
            result["also_moved"] = rows[1]
        result["note"] += (" 'moved' names the member whose placement changed across this drive: "
                           "delta_mm is how far its origin moved (mm), delta_deg the angle between "
                           "its before and after orientation (a magnitude, no sense), "
                           "geometry_moved_mm how far its own body corner travelled.")
    elif placement_readable:
        result["moved"] = None
        result["note"] += (f" 'moved' is null - neither member's placement changed by more than "
                           f"{_MOVE_BAND_MM} mm or {_MOVE_BAND_DEG} deg across this drive.")
    else:
        result["note"] += (" No 'moved' key: neither member's placement could be read, so which "
                           "part this drive displaced is not reported.")
    if directions:
        result.update(directions)
        result["note"] += (" The joint's own motion vector, read before the drive: "
                           + ", ".join(sorted(directions)) + ".")
    if partner:
        result["motion_link_partner"] = partner
        # The link's own STATE, beside the partner name - not keyed 'motion_link', which
        # joint_motion_link already publishes as the created link's NAME.
        result["motion_link_state"] = {k: link[k] for k in
                                       ("link", "suppressed", "broken", "value_self",
                                        "value_partner", "reversed")}
        link_ref = f"'{link['link'] or '(unnamed link)'}'"
        if couples is True:
            result["note"] += (f" NOTE: '{resolved_name}' is motion-linked to '{partner}' by "
                               f"{link_ref}, which reads neither suppressed nor compute-failed - the "
                               "link couples the two joints, so read the partner back with "
                               "assembly_get rather than driving it. In xref assemblies, drive/edit "
                               "cycles on a linked pair have killed the Fusion process.")
        elif couples is False:
            result["note"] += (f" NOTE: '{resolved_name}' is motion-linked to '{partner}' by "
                               f"{link_ref}, which {_link_state_text(link)} - this receipt makes NO "
                               "claim that the partner moved with it, and the second-member refusal "
                               f"is not armed for this pair. Read '{partner}' back with assembly_get "
                               "to see where it stands.")
        else:
            result["note"] += (f" NOTE: '{resolved_name}' is motion-linked to '{partner}' by "
                               f"{link_ref}, which {_link_state_text(link)} - whether the link moved "
                               "the partner is not known from this receipt. Read "
                               f"'{partner}' back with assembly_get, and do not drive it: the "
                               "second-member refusal stays armed on that unread state.")
        if partner_driven and couples is False:
            result["note"] += (" Both members have now been driven this session; nothing refused "
                               "the second, because the link does not read as one that couples.")
        elif partner_driven and plain_pair:
            result["note"] += (" Both members have now been driven; allowed in this plain (non-xref) "
                               "assembly, but avoid it in an xref assembly.")
    _driven_this_session.add(_reg_key(doc_id, joint))
    return ok(result)


TOOL_DESCRIPTION = (
    "Drive a joint to a value - the pose is TRANSIENT until "
    "assembly_capture_position(action='capture') keeps it."
)

tool = (
    Tool.create_simple(name="joint_drive", description=TOOL_DESCRIPTION)
    .add_input_property("joint_name", {"type": "string", "description": "From assembly_get."})
    .add_input_property("angle_deg", {"type": "number",
                                      "description": "Revolute / cylindrical."})
    .add_input_property("distance", {"type": "number",
                                     "description": "In 'units'; slider / cylindrical."})
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="geometry",
        evidence_test="tests/unit/test_joint_drive.py::TestBodyCornerEvidence"
                      "::test_a_slide_whose_placement_moved_but_body_stayed_is_an_error"))


def register_tool():
    register(item)
