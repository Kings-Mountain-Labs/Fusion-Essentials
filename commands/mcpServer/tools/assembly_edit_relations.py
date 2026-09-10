# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lifecycle for the three assembly relations - rigid group, motion link, assembly constraint:
suppress/unsuppress, delete, and reverse or re-value a motion link. Editing a rigid group's
MEMBERSHIP after creation raises on Fusion 2705.0.87, so set_occurrences refuses up front and
names the delete-and-recreate path instead.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, timeline_health
from . import _common
from . import _inputs
from . import _joints
from . import _relations

# Which actions each relation kind supports. Only a motion link carries a direction and a pair of
# coupled values; all three carry isSuppressed and deleteMe. set_occurrences stays in the rigid
# group's set so the caller meets the refusal below rather than an unknown verb.
_KIND_ACTIONS = {
    "rigid_group": ("suppress", "unsuppress", "delete", "set_occurrences"),
    "motion_link": ("suppress", "unsuppress", "delete", "reverse", "set_values"),
    "constraint": ("suppress", "unsuppress", "delete"),
}
_ACTIONS = ("suppress", "unsuppress", "delete", "set_occurrences", "reverse", "set_values")

_KIND = _inputs.Choice("kind", list(_relations.KINDS), required=True)
_ACTION = _inputs.Choice("action", list(_ACTIONS), required=True)
_OCCURRENCES = _inputs.OccurrenceRefList("occurrences")


def _flag(obj, prop):
    """A boolean flag read off `obj`, or None when it is unknown - `_common.read_flag` is the ONE
    unreadable-flag read; this only names the property."""
    return _common.read_flag(lambda: getattr(obj, prop))

# Measured on Fusion 2705.0.87: RigidGroup.setOccurrences on a group created in the same session
# raises "3 : Cannot be edited before rolling back", and the members read back unchanged - so
# post-creation membership editing is unusable on this build and the tool refuses it up front.
_SET_OCCURRENCES_REFUSAL = (
    "action='set_occurrences' is refused: this Fusion build (2705.0.87) will not edit a rigid "
    "group's members after it is created - setOccurrences raises '3 : Cannot be edited before "
    "rolling back' and the members read back unchanged. To change the members: "
    "assembly_edit_relations(kind='rigid_group', name=..., action='delete'), then "
    "assembly_rigid_group with the occurrences you want. Nothing was changed."
)

# What suppression/deletion OBSERVABLY did, per kind - the flag this call read back, plus the tool
# that re-creates the kind. What a suppressed relation does to the parts it relates is not measured.
_CREATE_TOOL = {"rigid_group": "assembly_rigid_group", "motion_link": "joint_motion_link",
                "constraint": "assembly_constrain"}


def _health_delta(before, design):
    """Timeline features newly in error since `before` - a suppress/delete can break a downstream
    feature that consumed what this relation held."""
    after, warnings, _total = timeline_health(design)
    return [n for n in after if n not in before], warnings


def _suppress_note(kind, state):
    """What the suppress call OBSERVED, per kind - the flag read back, and where to look for the
    effect. What suppression does to the members/coupling is not measured, so it is not claimed."""
    if kind == "motion_link":
        return (f"isSuppressed now reads {state} on the motion link, which couples two joints' "
                "motion. Drive one member (joint_drive) and read the partner back to see what "
                "changed.")
    if kind == "constraint":
        return (f"isSuppressed now reads {state} on the assembly constraint - the flag is what this "
                "call confirmed; what suppression does to its geometric relationships is not "
                "measured here.")
    return (f"isSuppressed now reads {state} on the rigid group; it stays in the timeline until "
            "action='delete'. Re-read positions with assembly_get to see the effect.")


def _do_suppress(design, obj, kind, name, suppressed):
    label = _relations.kind_label(kind)
    was = _flag(obj, "isSuppressed")
    errors_before, _w, _t = timeline_health(design)
    try:
        obj.isSuppressed = bool(suppressed)
    except Exception as e:
        return error(f"Could not set isSuppressed on {label} '{name}': {e}")
    now = _flag(obj, "isSuppressed")
    if now is None:
        return error(f"isSuppressed cannot be read on {label} '{name}' after setting it to "
                     f"{bool(suppressed)}, so the change is UNCONFIRMED. Re-read the relation with "
                     "assembly_get(include=['relations']).")
    if now != bool(suppressed):
        return error(f"Setting isSuppressed={bool(suppressed)} on {label} '{name}' did not take - "
                     f"it reads {now}.")
    new_errors, warnings = _health_delta(errors_before, design)
    out = {"kind": kind, "name": name, "is_suppressed": now,
           # null, not False, when the prior flag could not be read - an unreadable state is not "off".
           "was_suppressed": was,
           "note": _suppress_note(kind, now)}
    if new_errors:
        out["timeline_errors_after"] = new_errors
        out["note"] = (f"The change left {len(new_errors)} feature(s) in error: "
                       + ", ".join(new_errors) + ". Set the opposite action to restore it.")
    elif warnings:
        out["timeline_warnings"] = warnings
    return ok(out)


def _do_delete(design, obj, kind, name):
    label = _relations.kind_label(kind)
    errors_before, _w, _t = timeline_health(design)
    try:
        did = obj.deleteMe()
    except Exception as e:
        return error(f"Deleting {label} '{name}' failed: {e}")
    if not did:
        return error(f"Fusion declined to delete {label} '{name}' (deleteMe returned false) - it is "
                     "still in the design.")
    # Re-list the kind: a deleted relation must be GONE from the collection, not merely reported so.
    # relation_names drops an unreadable name, so `remaining` is the READABLE count of this kind.
    remaining = _relations.relation_names(design, kind)
    if any((n or "").lower() == name.lower() for n in remaining):
        return error(f"deleteMe reported success but {label} '{name}' is still listed - it was not "
                     "deleted.")
    new_errors, warnings = _health_delta(errors_before, design)
    out = {"deleted": True, "kind": kind, "name": name, "remaining": len(remaining),
           "note": (f"The {label} is no longer listed (re-read to confirm); 'remaining' counts the "
                    f"readable {label}s left. Undo in Fusion if unintended - the API cannot restore "
                    f"it. Re-create one with {_CREATE_TOOL[kind]}.")}
    if new_errors:
        out["timeline_errors_after"] = new_errors
        out["note"] += (f" WARNING: {len(new_errors)} feature(s) are now in error: "
                        + ", ".join(new_errors) + ".")
    elif warnings:
        out["timeline_warnings"] = warnings
    return ok(out)


def _do_reverse(ml, name):
    was = _flag(ml, "isReversed")
    if was is None:
        return error(f"Motion link '{name}' does not report isReversed, so there is no direction to "
                     "flip.")
    want = not was
    try:
        ml.isReversed = want
    except Exception as e:
        return error(f"Could not set isReversed on motion link '{name}': {e}")
    now = _flag(ml, "isReversed")
    if now is None:
        return error(f"isReversed cannot be read on motion link '{name}' after setting it to "
                     f"{want}, so the flip is UNCONFIRMED. Re-read the link with "
                     "assembly_get(include=['relations']).")
    if now != want:
        return error(f"Setting isReversed={want} on motion link '{name}' did not take - it reads "
                     f"{now}.")
    return ok({"kind": "motion_link", "name": name, "reversed": now,
               "was_reversed": was,
               "note": "The linked joints now move in the opposite sense relative to each other. "
                       "Drive ONE member (joint_drive) and read the partner back."})


def _do_set_values(ml, name, ratio):
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return error(f"'ratio' must be a number (got {ratio!r}).")
    if r == 0:
        return error("'ratio' must be non-zero (a 0 ratio links no motion).")
    # setMotionData re-states WHICH degrees of freedom are coupled, so the link's existing
    # motionOne/motionTwo are read back and passed through - picking a DOF here would silently
    # re-couple a different pair.
    m1 = safe(lambda: ml.motionOne)
    m2 = safe(lambda: ml.motionTwo)
    if m1 is None or m2 is None:
        return error(f"Motion link '{name}' does not report both coupled motions (motionOne/"
                     "motionTwo), so its values cannot be re-set without guessing which degrees of "
                     "freedom it links.")
    # setMotionData carries isReversed, so the SIGN of ratio SETS the direction outright: a positive
    # ratio clears an existing reversal. was_reversed reports what that overwrote.
    was_rev = _flag(ml, "isReversed")
    reversed_link = r < 0
    # 'ratio' is stated in each DOF's DISPLAY unit; the shared codec (the same one joint_motion_link
    # creates through) turns it into the native pair the API takes, so a re-value and a create of one
    # coupling send the same two numbers.
    value_one, value_two, ratio_facts = _joints.link_ratio_values(m1, m2, r)
    v1 = adsk.core.ValueInput.createByReal(value_one)
    v2 = adsk.core.ValueInput.createByReal(value_two)
    try:
        did = ml.setMotionData(m1, v1, m2, v2, reversed_link)
    except Exception as e:
        return error(f"setMotionData on motion link '{name}' failed: {e}. (The platform refuses a "
                     "coupling it cannot solve; the link is unchanged.)")
    if not did:
        return error(f"Fusion declined to re-value motion link '{name}' (setMotionData returned "
                     "false) - its ratio is unchanged.")
    one = safe(lambda: ml.valueOne.value)
    two = safe(lambda: ml.valueTwo.value)
    now_rev = _flag(ml, "isReversed")
    if one is None or two is None:
        return error(f"setMotionData reported success on '{name}' but its valueOne/valueTwo "
                     "parameters cannot be read back, so nothing confirms the new ratio.")
    # value_one is 1, so the coupled pair's own ratio IS value_two - the NATIVE number this ratio
    # converts to, which is what the read-back has to express. The comparison is the codec's own
    # gate (_joints.link_ratio_mismatch), so the create path judges its read-back the same way.
    mismatch = _joints.link_ratio_mismatch(value_two, one, two)
    if mismatch:
        return error(f"setMotionData reported success on '{name}' but {mismatch}.")
    if now_rev is None:
        return error(f"setMotionData reported success on '{name}' and its parameters read "
                     f"{one}:{two}, but isReversed cannot be read back, so the direction the SIGN "
                     "of ratio sets is UNCONFIRMED. Re-read the link with "
                     "assembly_get(include=['relations']).")
    if now_rev != reversed_link:
        return error(f"setMotionData reported success on '{name}' but it reads isReversed="
                     f"{now_rev}, not {reversed_link} - the direction did not take.")
    out = {"kind": "motion_link", "name": name, "ratio": r, "value_one": one, "value_two": two,
           "reversed": now_rev,
           "was_reversed": was_rev,
           "note": "Coupling re-valued - 'interpreted' states how the ratio was read, and "
                   "value_one/value_two are the link's own parameters READ BACK after the set. The "
                   "SIGN of ratio SETS the direction, so a positive ratio CLEARS an existing "
                   "reversal (was_reversed reports what it overwrote); action='reverse' flips the "
                   "direction without re-valuing. Drive ONE member (joint_drive) and read the "
                   "partner back."}
    # The codec's three keys, published identically by the create path (joint_motion_link), so one
    # ratio reads the same whichever writer applied it.
    out.update(ratio_facts)
    return ok(out)


def handler(kind: str = "", name: str = "", action: str = "", occurrences=None,
            include_children: bool = False, ratio=None) -> dict:
    """See TOOL_DESCRIPTION."""
    values, verr = _inputs.resolve_inputs([_KIND, _ACTION], {"kind": kind, "action": action})
    if verr:
        return verr
    kind, action = values["kind"], values["action"]

    allowed = _KIND_ACTIONS[kind]
    if action not in allowed:
        return error(f"action='{action}' does not apply to a {_relations.kind_label(kind)} - it "
                     f"supports: {', '.join(allowed)}.")
    # Refused BEFORE anything is resolved or rolled: the platform refuses the edit itself, so
    # resolving the group and its occurrences first would only add failure modes ahead of the
    # teaching. Kept in the action set so the agent is taught, not met with an unknown verb.
    if action == "set_occurrences":
        return error(_SET_OCCURRENCES_REFUSAL)

    design = _common.design()
    if not design:
        return error("No active design with components.")

    obj, _comp, rerr = _relations.find_relation(design, kind, name)
    if rerr:
        return error(rerr)
    nm = safe(lambda: obj.name) or (name or "").strip()

    if action in ("suppress", "unsuppress"):
        return _do_suppress(design, obj, kind, nm, action == "suppress")
    if action == "delete":
        return _do_delete(design, obj, kind, nm)
    if action == "reverse":
        return _do_reverse(obj, nm)
    return _do_set_values(obj, nm, ratio)


TOOL_DESCRIPTION = (
    "Edit or remove an existing assembly relation; create one with assembly_rigid_group / "
    "joint_motion_link / assembly_constrain."
)

tool = (
    Tool.create_simple(name="assembly_edit_relations", description=TOOL_DESCRIPTION)
    .add_input_property(*_KIND.as_property())
    .add_input_property("name", {"type": "string"})
    .add_input_property(*_ACTION.as_property())
    .add_input_property(*_OCCURRENCES.as_property())
    .add_input_property("include_children", {"type": "boolean"})
    .add_input_property("ratio", {"type": "number",
            "description": "set_values: joint_two per ONE unit of joint_one, in each joint's DISPLAY unit (deg or mm); the SIGN sets direction."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_assembly_edit_relations.py::TestDelete"
                      "::test_a_survivor_after_a_true_delete_is_an_error"))


def register_tool():
    register(item)
