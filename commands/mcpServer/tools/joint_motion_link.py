# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Links two existing joints' motion with a ratio (the Motion Link command) so driving one moves the
other proportionally - a gear pair, belt/chain drive, or coupled rotation. WRITES (adds a MotionLink
feature).
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from ._joints import (find_joint, all_joints, motion_link_dof, link_ratio_values,
                      link_ratio_mismatch)

app = adsk.core.Application.get()


def _joint_names(design):
    """Every joint name in the design (the full walk), for a resolve-failure error message - so the
    list matches what find_joint can actually resolve (a sub-component or as-built joint included),
    not just root joints."""
    return [nm for nm in (safe(lambda j=j: j.name) for j in all_joints(design)) if nm]


def _is_slider(joint):
    """True when the joint's landed motion is a slider - the pair the travel-direction trap lives
    on."""
    return type(safe(lambda: joint.jointMotion)).__name__ == "SliderJointMotion"


# Which way a linked slider pair TRAVELS is not computable from the two slideDirectionVectors and
# the reversed flag: each joint's occurrence ordering sets which part its slide value moves, and
# that sense has no reliable read - so the payload teaches the check instead.
_SLIDER_PAIR_NOTE = (
    " MIRROR OR TRANSLATE: the ratio sign alone does not determine whether this slider pair "
    "mirrors about the center or travels together - it also depends on each slide direction and "
    "each joint's occurrence ordering. Prove the motion: joint_drive one member a small distance "
    "and read its 'moved', then drive back to 0.")


def handler(joint_one: str = "", joint_two: str = "", ratio: float = 1.0) -> dict:
    """See TOOL_DESCRIPTION."""
    j1name, j2name = (joint_one or "").strip(), (joint_two or "").strip()
    if not j1name or not j2name:
        return error("Provide 'joint_one' and 'joint_two' - the two joints to link.")
    if j1name == j2name:
        return error("joint_one and joint_two must be different joints.")
    design = _common.design()
    if not design:
        return error("No active design.")
    root = safe(lambda: design.rootComponent)
    j1, amb1 = find_joint(design, j1name)
    j2, amb2 = find_joint(design, j2name)
    if amb1 or amb2:
        return error(amb1 or amb2)
    if not j1 or not j2:
        missing = j1name if not j1 else j2name
        names = _joint_names(design)
        return error(f"No joint named '{missing}'. Joints: {', '.join(n for n in names if n) or '(none)'}.")

    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return error(f"ratio must be a number (got {ratio!r}).")
    if r == 0:
        return error("ratio must be non-zero (a 0 ratio links no motion).")
    reversed_link = r < 0

    # setMotionData couples ONE JointMotionTypes DOF per joint, resolved BEFORE creating anything so
    # an unlinkable joint fails without leaving a stray link to roll back.
    m1, err1 = motion_link_dof(j1)
    m2, err2 = motion_link_dof(j2)
    for jname, mdof, merr in ((j1name, m1, err1), (j2name, m2, err2)):
        if mdof is None:
            return error(f"Joint '{jname}' {merr}. Link two joints that permit motion "
                         "(revolute/slider/cylindrical).")

    try:
        mls = root.motionLinks
        # createInput takes the TWO joints directly, NOT an ObjectCollection.
        inp = mls.createInput(j1, j2)
        ml = mls.add(inp)
    except Exception as e:
        return error(f"Could not create the motion link: {e}. (Two joints already coupled through the "
                     "same kinematic chain cannot be linked - the platform refuses them here.)")
    if not ml:
        return error("Motion link creation returned nothing - check that both joints permit motion "
    "(revolute/slider/cylindrical); a rigid joint cannot be linked.")

    # Apply the ratio AFTER add via setMotionData, the shared codec turning the caller's
    # display-unit ratio into the native pair. If this fails the link still exists at the API's
    # default 1:1, which the branch below reports.
    value_one, value_two, ratio_facts = link_ratio_values(m1, m2, r)
    ratio_error = None
    try:
        # setMotionData wants a JointMotionTypes DOF per joint (from motion_link_dof), NOT the joint's
        # JointTypes value that jointMotion.jointType returns - passing that raises BAD_JOINT_DOF.
        v1 = adsk.core.ValueInput.createByReal(value_one)
        v2 = adsk.core.ValueInput.createByReal(value_two)
        ok_set = ml.setMotionData(m1, v1, m2, v2, reversed_link)
        if not ok_set:
            ratio_error = "setMotionData returned False"
    except Exception as e:
        ratio_error = str(e)

    if ratio_error:
        # The link was added but the ratio could not be applied, leaving a compute-failed feature;
        # roll it back rather than leave a broken 1:1 link nobody asked for.
        link_name = safe(lambda: ml.name)
        try:
            rolled_back = bool(ml.deleteMe())
        except Exception:
            rolled_back = False
        msg = ("Created the link but could not apply the ratio: the platform will not couple "
               f"these two joints' motion. (Fusion: {ratio_error})")
        if not rolled_back:
            # The rollback is itself a mutation whose result must be read: a declined deleteMe
            # leaves exactly the link this branch exists to prevent.
            msg += (" The rollback FAILED as well: the motion link "
                    + (f"'{link_name}' " if link_name else "(its name could not be read) ")
                    + "REMAINS in the design at the platform's DEFAULT 1:1 ratio - it couples these "
                      "two joints even though the requested ratio was never applied. Delete it by "
                      "name with assembly_edit_relations(kind='motion_link', name=..., "
                      "action='delete'); assembly_get(include=['relations']) names it.")
        return error(msg)

    # The link's OWN parameters in Fusion's native cm/rad - what 'value_one'/'value_two' publish
    # everywhere on this surface. The pair that was SENT is named in 'interpreted' instead.
    read_one = safe(lambda: ml.valueOne.value)
    read_two = safe(lambda: ml.valueTwo.value)
    mismatch = link_ratio_mismatch(value_two, read_one, read_two)
    if mismatch:
        # setMotionData answered True and the link holds a DIFFERENT coupling. It is not rolled
        # back: it computed, and a deleteMe on a pair that read is a mutation this cannot justify.
        link_name = safe(lambda: ml.name)
        return error(
            f"setMotionData reported success but {mismatch}. The motion link "
            + (f"'{link_name}' " if link_name else "(its name could not be read) ")
            + "REMAINS in the design holding the pair its parameters read. "
              "Re-value it with assembly_edit_relations(kind='motion_link', name=..., "
              "action='set_values'), or remove it with action='delete'; "
              "assembly_get(include=['relations']) names it.")

    out = {
                "linked": True,
    "motion_link": safe(lambda: ml.name),
    "joint_one": safe(lambda: j1.name),
    "joint_two": safe(lambda: j2.name),
    "ratio": r,
    "value_one": read_one,
    "value_two": read_two,
    "ratio_applied": True,
    "reversed": reversed_link,
    "note": ("Joints linked - value_one/value_two are the link's own parameters READ BACK after "
        "the set. Whether the link moves the partner is not claimed here: joint_drive ONE member "
        "and its receipt answers whether the link couples, then read the partner back with "
        "assembly_get - joint_drive REFUSES the second member of an xref pair."),
    }
    if read_one is None or read_two is None:
        # The pair did not read as TWO numbers, which is no evidence either way. The silent side
        # stays null rather than echoing what was sent.
        out["note"] += (" The link's valueOne/valueTwo did not both read back here, so the "
                        "published pair is incomplete and the coupling this call applied is "
                        "UNCONFIRMED - assembly_get(include=['relations']) reads the pair off "
                        "the link.")
    # The codec's three keys, published identically by the re-value path (assembly_edit_relations
    # set_values), so one ratio reads the same whichever writer applied it.
    out.update(ratio_facts)

    # Two sliders: teach the travel-direction check. Never a computed verdict - see the measured
    # fact at _SLIDER_PAIR_NOTE.
    if _is_slider(j1) and _is_slider(j2):
        out["note"] += _SLIDER_PAIR_NOTE
    return ok(out)


TOOL_DESCRIPTION = (
    "Link two existing joints' motion with a ratio (the Motion Link command): driving one drives "
    "the other proportionally."
)

motion_link_tool = (
    Tool.create_simple(name="joint_motion_link", description=TOOL_DESCRIPTION)
    .add_input_property("joint_one", {"type": "string"})
    .add_input_property("joint_two", {"type": "string"})
    .add_input_property("ratio", {"type": "number",
            "description": "joint_two motion per unit of joint_one, each in its own display unit "
                           "(deg or mm)."})
    .strict_schema()
)
motion_link_item = Item.create_tool_item(
    tool=motion_link_tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_joint_motion_link.py::TestValueReadBack"
                      "::test_a_link_left_holding_a_different_coupling_is_an_error"))


def register_tool():
    register(motion_link_item)
