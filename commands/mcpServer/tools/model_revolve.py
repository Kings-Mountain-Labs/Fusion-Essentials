# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: revolve a sketch profile about an axis into a solid.

  model_revolve -> spin a closed sketch profile around an axis to make a solid of revolution
                   (shafts, pistons, pulleys, bottles, anything turned). Choose the feature
                   operation, the angle (full 360 or partial), and symmetry. WRITES.
"""

import math

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component, root_body_advisory
from . import _common
from . import _geom
from . import _inputs
from . import _sketch_detail
from . import _assert

app = adsk.core.Application.get()

# profile_index may carry a profile HANDLE (entityToken from sketch_get) - resolved via ProfileRef.
_PROFILE = _inputs.ProfileRef("profile_index", scope_input="component")

_VEC_TO_KEY = {(1, 0, 0): "x", (0, 1, 0): "y", (0, 0, 1): "z"}

# face_entity hands an axis-defining FACE through: createInput's axis takes the entity that DEFINES
# the axis, and a face resolved to a direction vector would drop the axis POSITION - a cylinder at
# x=30 would revolve about the world axis through the origin.
_AXIS = _inputs.AxisRef("axis", face_entity=True, default="z")
_TARGET_BODIES = _inputs.BodyRefList("target_bodies", required=False)


def _in_context(ent, comp, design):
    """(entity, error) - the axis entity in a form the revolve's component can consume."""
    # A NATIVE entity (assemblyContext None) owned by ANOTHER component takes the whole script host
    # down, not a catchable raise; the same entity proxied into the occurrence that carries it is
    # accepted. The refusal wording completes the caller's "Could not resolve axis '<x>': ..."
    occ, err = _inputs.single_placement("it", ent, comp, design)
    if err:
        return None, err
    if occ is None:
        return ent, None
    proxy = safe(lambda: ent.createForAssemblyContext(occ))
    if proxy is None:
        path = safe(lambda: occ.fullPathName) or "its one occurrence"
        return None, (f"it could not be brought into the revolve's assembly context ({path}). Pass "
                      "a handle at geometry in the sketch's own component, or a world axis (x/y/z).")
    return proxy, None


def _cut_check_bodies(comp):
    """The solid bodies a cut/intersect revolve can act on: every solid directly in the feature's
    host component, resolved once so the same objects are re-read afterwards."""
    return [b for b in _common.iter_collection(safe(lambda: comp.bRepBodies))
            if safe(lambda b=b: b.isSolid)]


def _axis_entity(design, comp, sketch, axis):
    """(axis entity, label), or (None, error detail): world x/y/z, a handle, or 'line:<index>' for
    a line by position in the profile's own sketch."""
    a = (axis or "z").strip().lower()
    if a.startswith("line:"):
        try:
            idx = int(a.split(":", 1)[1])
        except Exception:
            return None, f"'{axis}' is not a valid line selector (want 'line:<index>')."
        lines = safe(lambda: sketch.sketchCurves.sketchLines)
        n = safe(lambda: lines.count, 0) if lines else 0
        if lines is None or not (0 <= idx < n):
            return None, f"line index {idx} out of range - sketch has {n} line(s)."
        return safe(lambda: lines.item(idx)), f"sketch {a}"

    tagged, err = _AXIS.resolve(axis)
    if err:
        return None, err
    kind, val = tagged
    if kind == "world":
        key = _VEC_TO_KEY.get(val)
        ent = _inputs.world_construction_axis(comp, key) if key else None
        return ent, f"{key}-axis"
    # The label is the entity's own name where it has one (a construction axis does), else its type
    # - neither a BRepEdge nor a BRepFace carries a name.
    ent, cerr = _in_context(val, comp, design)
    if cerr:
        return None, cerr
    name = safe(lambda: ent.name)
    return ent, (name if isinstance(name, str) and name else type(ent).__name__)


def handler(sketch_name: str = "", profile_index=0, axis: str = "z",
            angle_deg: float = 360.0, operation: str = "new", symmetric: bool = False,
            second_angle_deg: float = 0.0, component: str = "", target_bodies=None) -> dict:
    """See TOOL_DESCRIPTION."""
    op_key = (operation or "new").strip().lower()
    if op_key not in _common.OPERATIONS:
        return error(f"Unknown operation '{operation}'. Use: new, join, cut, intersect.")
    try:
        ang = float(angle_deg)
    except Exception:
        return error("angle_deg must be a number (degrees).")
    if ang == 0:
        return error("Provide a non-zero 'angle_deg' to revolve (e.g. 360 for a full revolve).")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    sketch, requested, ambiguous = _sketch_detail.scoped_or_recent_sketch(
        design, sketch_name, component)
    if ambiguous:
        return error(ambiguous)
    if not sketch:
        if requested:
            names = _common.all_sketch_names(design)
            avail = f" Available: {', '.join(names)}." if names else ""
            return error(f"No sketch named '{requested}'.{avail} Use sketch_get or sketch_create.")
        return error("No sketch to revolve. Create one and draw a closed profile first.")

    profiles = safe(lambda: sketch.profiles)
    pcount = safe(lambda: profiles.count, 0) if profiles else 0
    # HANDLE path: a profile entityToken from sketch_get (a ProfileRef) targets the exact region -
    # the robust pick on a multi-profile / on-face sketch, where a blind index is ambiguous.
    if _inputs.is_handle(profile_index):
        profile, perr = _PROFILE.resolve(profile_index, component)
        if perr:
            return error(perr)
        idx = "handle"
    else:
        # The index path reads sketch.profiles directly, never through ProfileRef, so a deferred
        # sketch's pcount and item(idx) are both the pre-deferral ones.
        stale = _inputs.deferred_sketch_refusal("profile_index", sketch)
        if stale:
            return error(stale)
        if pcount == 0:
            return error(f"Sketch '{safe(lambda: sketch.name)}' has no closed profile to revolve.")
        try:
            idx = int(profile_index)
        except Exception:
            idx = 0
        if idx < 0 or idx >= pcount:
            return error(f"profile_index {idx} out of range - sketch has {pcount} profile(s).")
        profile = profiles.item(idx)

    # Host the feature on the sketch's OWNING component (see profile_host_component) and take origin
    # axes from that same component - a revolve input mixes contexts otherwise.
    host = _inputs.profile_host_component(profile, sketch, comp)
    axis_entity, axis_label = _axis_entity(design, host, sketch, axis)
    if not axis_entity:
        return error(f"Could not resolve axis '{axis}': {axis_label or 'use x | y | z, a straight-edge/sketch handle, or line:<index>.'}")

    scoped_bodies = None
    if target_bodies not in (None, "", []):
        if op_key not in ("cut", "intersect"):
            return error("'target_bodies' only applies to cut/intersect operations.")
        scoped_bodies, berr = _TARGET_BODIES.resolve(target_bodies)
        if berr:
            return error(berr)

    op = getattr(adsk.fusion.FeatureOperations, _common.OPERATIONS[op_key])
    try:
        rev_input = host.features.revolveFeatures.createInput(profile, axis_entity, op)
    except Exception as e:
        return error(f"Could not start revolve: {e}. (The axis must not pass through the profile "
    "in a way that self-intersects.)")

    angle_val = adsk.core.ValueInput.createByReal(math.radians(ang))
    try:
        second = float(second_angle_deg or 0.0)
        if second and not symmetric:
            # asymmetric two-sided revolve: 'ang' one way, 'second' the other.
            second_val = adsk.core.ValueInput.createByReal(math.radians(second))
            if not rev_input.setTwoSideAngleExtent(angle_val, second_val):
                return error(f"Fusion refused a two-sided revolve extent ({angle_deg} / "
                             f"{second_angle_deg} deg), so nothing was revolved.")
        else:
            if not rev_input.setAngleExtent(bool(symmetric), angle_val):
                return error(f"Fusion refused a {'symmetric ' if symmetric else ''}revolve extent "
                             f"of {angle_deg} deg, so nothing was revolved.")
    except Exception as e:
        return error(f"Could not set revolve angle: {e}")

    if scoped_bodies is not None:
        try:
            rev_input.participantBodies = list(scoped_bodies)
        except Exception as e:
            return error(f"Could not set target_bodies before revolve: {e}")

    # cut/intersect MATERIAL evidence: the volumes the operation must move, sampled BEFORE the add.
    # A revolve reports a healthy feature for a profile that sweeps through empty air, so the feature
    # object alone cannot say material changed - only this before/after pair can.
    check_bodies = ((list(scoped_bodies) if scoped_bodies is not None else _cut_check_bodies(host))
                    if op_key in ("cut", "intersect") else [])
    vol_before = _geom.volumes(check_bodies)
    count_hosts = ([host] if check_bodies and scoped_bodies is None else
                   [safe(lambda b=b: b.parentComponent) for b in check_bodies])
    body_counts_before = [(c, _common.body_count(c)) for c in count_hosts if c is not None]
    # A join is told "grew a body" from "made a second one" by the host's body NAMES before the add.
    bodies_before = _common.component_body_names(host) if op_key == "join" else None

    try:
        feature = host.features.revolveFeatures.add(rev_input)
    except Exception as e:
        # No coplanarity claim: the API projects an axis that is not in the profile's plane ONTO that
        # plane, so out-of-plane is not itself a cause. A profile CROSSING the axis is.
        return error(f"Revolve failed: {e}. (A 'cut'/'intersect' needs existing geometry to act on. "
    "An axis outside the profile's plane is projected onto it, so that is not the cause; a profile "
    "that CROSSES the axis is refused.)")
    if not feature:
        return error(_common.no_feature_error(design, "Revolve"))

    volume_delta_cm3 = None
    body_count_changed = False
    if check_bodies:
        delta, readable = _geom.volume_delta(check_bodies, vol_before)
        count_changes = [(before, _common.body_count(c)) for c, before in body_counts_before]
        body_count_changed = any(before is not None and after is not None and before != after
                                 for before, after in count_changes)
        # A body whose volume read BEFORE and reads unreadable now was consumed whole - a real effect
        # that contributes no delta, so it must not be counted as "nothing moved".
        consumed = [b for b in check_bodies
                    if vol_before.get(id(b)) is not None and _geom.signed_volume(b) is None]
        if readable and not body_count_changed:
            volume_delta_cm3 = round(delta, 6)
        if (readable and not consumed and not body_count_changed
                and abs(delta) < _common.NO_VOLUME_CHANGE_CM3):
            where = safe(lambda: host.name) or "the host component"
            return error(f"Revolve reported success but this {op_key} changed nothing - every solid "
                         f"body in '{where}' measures the volume it had before and none was "
                         "consumed, so the revolved shape does not overlap any of them. Check that "
                         "the profile and axis put the swept solid inside the target body (an "
                         "'intersect' whose target lies entirely INSIDE the swept solid also reads "
                         "this way). " + _common.failed_effect_remedy(design, feature))

    body_names = [f["name"] for f in _common.body_facts(_common.result_bodies(feature))]

    note = "Profile revolved into a solid. Pair with view_screenshot (iso) to view it."
    if scoped_bodies is not None:
        note += (" 'scoped_to_bodies' lists the configured participant bodies; Fusion does not "
                 "expose a readback for this input.")
    elif op_key in ("cut", "intersect"):
        note += " With no target_bodies, Fusion considers every intersected body."
    if body_count_changed:
        note += (" The body count changed, so volume_delta_cm3 is omitted; held-body volumes do "
                 "not describe the resulting body set.")
    if op_key == "new":
        adv = root_body_advisory(design, host)
        if adv:
            note += " " + adv
    join_clause = _common.join_new_body_clause(op_key, bodies_before, body_names)
    if join_clause:
        note += " " + join_clause

    payload = {
        "revolved": True,
        "feature": safe(lambda: feature.name),
        "operation": op_key,
        "sketch": safe(lambda: sketch.name),
        "component": safe(lambda: feature.parentComponent.name),
        "profile_index": idx,
        "axis": axis_label,
        "angle_deg": round(ang, 6),
        "second_angle_deg": round(float(second_angle_deg or 0.0), 6),
        "symmetric": bool(symmetric),
        "result_bodies": body_names,
        "note": note,
    }
    if scoped_bodies is not None:
        payload["scoped_to_bodies"] = [_inputs.qualified_body_name(b) for b in scoped_bodies]
    # Absent, never null: a null would read as "no material moved".
    if volume_delta_cm3 is not None:
        payload["volume_delta_cm3"] = volume_delta_cm3
    return ok(payload)


TOOL_DESCRIPTION = (
"Revolve a sketch profile about an axis; sketch one half - the profile must not cross the axis."
)

revolve_tool = (
    Tool.create_simple(name="model_revolve", description=TOOL_DESCRIPTION)
    .add_input_property("sketch_name", {"type": "string"})
    .add_input_property("profile_index", {"type": ["integer", "string"],
            "description": "An index (default 0), or a profile 'handle'."})
    .add_input_property("axis", {"type": "string",
            "description": _AXIS.schema()["description"] + " Or 'line:<i>' for a line in the "
            "profile's sketch."})
    .add_input_property("angle_deg", {"type": "number"})
    .add_input_property("second_angle_deg", {"type": "number"})
    .add_input_property("target_bodies", _TARGET_BODIES.schema())
    .add_input_property(*_inputs.boolean_op(default="new").as_property())
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .add_input_property("symmetric", {"type": "boolean"})
    .strict_schema()
)
revolve_item = Item.create_tool_item(tool=revolve_tool, write="write", handler=handler, run_on_main_thread=True,
                                     postconditions=[_assert.FeatureHealthy()],
                                     verification=Verification(
                                         kind="inline", rung="geometry",
                                         evidence_test="tests/unit/test_model_revolve.py"
                                         "::TestCutMovesMaterial"
                                         "::test_cut_that_moves_no_volume_is_an_error"))


def register_tool():
    register(revolve_item)
