# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: sweep a sketch profile along a path into a solid (or surface).

  model_sweep -> drive a cross-section profile along a path (model edges or a path sketch) to make a
                 3D body: pipes, handrails, cables, moulding, extrusions that follow a curve. Solid by
                 default; an open-curve profile makes a SURFACE (no end caps). WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component, root_body_advisory, build_path
from . import _common
from . import _geom
from . import _inputs
from . import _outputs
from . import _sketch_detail

app = adsk.core.Application.get()

# scope_input: the {sketch, profile_index} form addresses a sketch BY NAME, and Fusion numbers
# sketches per component from 1, so a name two components carry is refused, naming 'component'.
_PROFILE = _inputs.ProfileRef("profile", required=True, scope_input="component")
_TARGET_BODIES = _inputs.BodyRefList("target_bodies", required=False)

# orientation keyword -> adsk.fusion.SweepOrientationTypes attribute.
_ORIENTATIONS = {
    "perpendicular": "PerpendicularOrientationType",
    "parallel": "ParallelOrientationType",
}

RETURNS = [
    _outputs.ReturnsValue("result_bodies", "the names of the bodies the sweep created/modified"),
    _outputs.ReturnsValue("is_solid", "whether the sweep produced a SOLID (vs an open surface)"),
    _outputs.ReturnsValue("path_curves", "how many curves the built path actually holds"),
]


def _sketch_for_open(design, profile_raw, component):
    """(sketch or None, refusal or None) for an OPEN profile - only a {sketch, profile_index}
    selector names a sketch, resolved through the same scoped walk the closed path uses."""
    if not isinstance(profile_raw, dict):
        return None, None
    nm = profile_raw.get("sketch", profile_raw.get("sketch_name", ""))
    sk, _requested, refusal = _sketch_detail.scoped_or_recent_sketch(design, nm, component)
    return sk, refusal


def _path_sketch_curves(host, path_raw):
    """How many SWEEPABLE curves the sketch a 'sketch:<name>' path names carries - None for any
    other path form or an unreadable sketch. Construction geometry is not part of any path."""
    if not (isinstance(path_raw, str) and path_raw.strip().lower().startswith("sketch:")):
        return None
    sk, _ = _common.target_sketch(host, path_raw.split(":", 1)[1].strip())
    if sk is None:
        return None
    curves = safe(lambda: sk.sketchCurves)
    if _common.counted(lambda: curves.count) is None:
        return None
    return sum(1 for c in _common.iter_collection(curves)
               if not bool(safe(lambda c=c: c.isConstruction, False)))


def _cut_check_bodies(comp):
    """The solid bodies an UNSCOPED cut/intersect sweep can act on: every solid directly in the
    feature's host component, resolved once so the same objects are re-read afterwards."""
    return [b for b in _common.iter_collection(safe(lambda: comp.bRepBodies))
            if safe(lambda b=b: b.isSolid)]


def _resolve_profile(design, comp, profile_raw, as_surface, component=""):
    """(profile_arg, want_solid, open_profile, host, error) - an open profile forces want_solid
    False, and `host` is the profile's OWNING component, since handing another component's native
    profile to features.createInput raises 'InternalValidationError : bSet'."""
    if profile_raw in (None, "", []):
        return None, None, None, None, ("'profile' is required (a profile handle from sketch_get, or a "
                                        "{sketch, profile_index} selector).")
    prof, err = _PROFILE.resolve(profile_raw, component)
    if prof is not None:
        host = _inputs.profile_host_component(prof, None, comp)
        return prof, (not bool(as_surface)), False, host, None
    # No closed profile. If the selector names a sketch with open curves, build an OPEN profile on the
    # sketch's OWNING component (the same host the feature is built on).
    sk, sk_refusal = _sketch_for_open(design, profile_raw, component)
    if sk_refusal:
        return None, None, None, None, sk_refusal
    if sk is not None:
        host = _common.safe(lambda: sk.parentComponent) or comp
        open_prof, operr = _common.open_profile_from_sketch(
            host, sk, "for a surface sweep",
            no_curves_error=(f"Profile sketch '{safe(lambda: sk.name)}' has no curves to sweep. "
                             "Draw an open path (a line/arc/spline) or a closed region first."))
        if open_prof is not None:
            return open_prof, False, True, host, None
        return None, None, None, None, operr or err
    return None, None, None, None, err


def handler(profile=None, path=None, operation: str = "new", orientation: str = "perpendicular",
            as_surface: bool = False, target_bodies=None, component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    op_key = (operation or "new").strip().lower()
    if op_key not in _common.OPERATIONS:
        return error(f"Unknown operation '{operation}'. Use: new, join, cut, intersect.")
    orient_key = (orientation or "perpendicular").strip().lower()
    if orient_key not in _ORIENTATIONS:
        return error(f"Unknown orientation '{orientation}'. Use: perpendicular, parallel.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    profile_arg, want_solid, open_profile, host, perr = _resolve_profile(
        design, comp, profile, as_surface, component)
    if perr:
        return error(perr)

    # Build the path AND the feature on the profile's OWNING component (host) - a profile-consuming
    # feature created on the active component raises bSet when the profile is owned elsewhere.
    sweep_path, path_label, patherr = build_path(host, path)
    if patherr:
        return error(patherr)
    # What the built Path HOLDS, beside what the request named: the profile is driven over these
    # curves and no others, so a chain that stopped short sweeps a stub of the intended run.
    path_curves = _common.counted(lambda: sweep_path.count)
    sketch_curves = _path_sketch_curves(host, path)

    op = getattr(adsk.fusion.FeatureOperations, _common.OPERATIONS[op_key])
    try:
        sweep_input = host.features.sweepFeatures.createInput(profile_arg, sweep_path, op)
    except Exception as e:
        return error(f"Could not start sweep: {e}. (The path must geometrically connect and the "
                     "profile should sit on/near the path start.)")

    try:
        sweep_input.isSolid = bool(want_solid) # False -> open surface, no end caps
        sweep_input.orientation = getattr(adsk.fusion.SweepOrientationTypes, _ORIENTATIONS[orient_key])
    except Exception as e:
        return error(f"Could not configure the sweep: {e}")

    # target_bodies: scope a cut/intersect to specific bodies so it can't bleed through others.
    scoped_to = None
    bodies_ents = None
    if target_bodies not in (None, "", []):
        if op_key == "new":
            return error("'target_bodies' only applies to cut/join/intersect (a 'new' body has no "
                         "participants). Remove it, or change the operation.")
        bodies_ents, berr = _TARGET_BODIES.resolve(target_bodies)
        if berr:
            return error(berr)
        try:
            sweep_input.participantBodies = list(bodies_ents)
            scoped_to = [safe(lambda b=b: b.name) for b in bodies_ents]
        except Exception as e:
            return error(f"Could not scope to target_bodies: {e}")

    # cut/intersect MATERIAL evidence: the volumes the operation must move, sampled BEFORE the add.
    # A sweep along a path that misses the body reports a healthy feature and result bodies just the
    # same, so only this before/after pair can say material actually changed.
    check_bodies = []
    if op_key in ("cut", "intersect"):
        check_bodies = list(bodies_ents) if bodies_ents else _cut_check_bodies(host)
    vol_before = _geom.volumes(check_bodies)
    # A join is told "grew a body" from "made a second one" by the host's body NAMES before the add.
    bodies_before = _common.component_body_names(host) if op_key == "join" else None

    try:
        feature = host.features.sweepFeatures.add(sweep_input)
    except Exception as e:
        return error(f"Sweep failed: {e}. (A 'cut'/'intersect' needs existing geometry to act on; "
                     "the profile and path must form a valid sweep.)")
    if not feature:
        return error(_common.no_feature_error(design, "Sweep"))

    body_names = [f["name"] for f in _common.body_facts(_common.result_bodies(feature))]

    # An operation that reports success but produced no body is a silent no-op - fail it honestly.
    if op_key == "new" and not body_names:
        return error("Sweep reported success but created no body. Check that the profile sits on the "
                     "path and the path forms a valid, connected sweep.")

    volume_delta_cm3 = None
    if check_bodies:
        delta, readable = _geom.volume_delta(check_bodies, vol_before)
        # A body whose volume read BEFORE and reads unreadable now was consumed whole - a real effect
        # that contributes no delta, so it must not be counted as "nothing moved".
        consumed = [b for b in check_bodies
                    if vol_before.get(id(b)) is not None and _geom.signed_volume(b) is None]
        if readable:
            volume_delta_cm3 = round(delta, 6)
        if readable and not consumed and abs(delta) < _common.NO_VOLUME_CHANGE_CM3:
            if scoped_to:
                # SCOPED: only a participant body can be affected, and every one of them measures
                # what it did before - nothing landed anywhere, so the feature is safe to remove.
                named = ", ".join(n for n in scoped_to if n) or "the scoped bodies"
                rolled = bool(safe(lambda: feature.deleteMe(), False))
                return error(f"Sweep reported success but this {op_key} changed nothing - "
                             f"{named} measure the volumes they had before and none was consumed, so "
                             "the profile does not sweep through any of them. A cut/intersect can "
                             "only affect bodies named in 'target_bodies' - check the path runs "
                             "through them. "
                             + ("The sweep feature was rolled back." if rolled else
                                "Remove the empty feature with design_delete_feature."))
            where = safe(lambda: host.name) or "the host component"
            return error(f"Sweep reported success but this {op_key} changed nothing - every solid "
                         f"body in '{where}' measures the volume it had before and none was "
                         "consumed, so the swept profile does not overlap any of them. Check the "
                         "path runs through the target body (an 'intersect' whose target lies "
                         "entirely INSIDE the swept solid also reads this way). "
                         + _common.failed_effect_remedy(design, feature))

    # SOLID/SURFACE verdict read BACK off the feature - never assumed from the request.
    is_solid = safe(lambda: feature.isSolid)
    if open_profile or is_solid is False:
        note = ("Swept into a SURFACE (no end caps) - pair with model_stitch to close several "
                "surfaces into a solid.")
    else:
        note = "Profile swept into a solid along the path. Pair with view_screenshot (iso) to view it."
    if op_key == "new":
        adv = root_body_advisory(design, host)
        if adv:
            note += " " + adv
    join_clause = _common.join_new_body_clause(op_key, bodies_before, body_names)
    if join_clause:
        note += " " + join_clause
    if path_curves is not None and sketch_curves is not None and path_curves < sketch_curves:
        note += (f" WARNING: the path chained {path_curves} of the sketch's {sketch_curves} curves, so "
                 "the sweep covers only that run - chaining follows tangent continuity and a sharp "
                 "corner stops it. Make the junction tangent, or sweep each run separately.")

    payload = {
        "swept": True,
        "feature": safe(lambda: feature.name),
        "operation": op_key,
        "component": safe(lambda: feature.parentComponent.name),
        "path": path_label,
        "path_curves": path_curves,
        "orientation": orient_key,
        "as_surface": bool(open_profile or is_solid is False),
        "open_profile": bool(open_profile),
        "is_solid": is_solid,
        "scoped_to_bodies": scoped_to,
        "result_bodies": body_names,
        "note": note,
    }
    # Only a 'sketch:<name>' path has a source curve count; a null would read as an unreadable sketch.
    if sketch_curves is not None:
        payload["path_sketch_curves"] = sketch_curves
    # Absent, never null: a null would read as "no material moved".
    if volume_delta_cm3 is not None:
        payload["volume_delta_cm3"] = volume_delta_cm3
    return ok(payload)


TOOL_DESCRIPTION = (
"Sweep a profile along a path.\n"
+ _outputs.produces_block(RETURNS)
)

sweep_tool = (
    Tool.create_simple(name="model_sweep", description=TOOL_DESCRIPTION)
    .add_input_property("profile", {"type": ["string", "object"]})
    .add_input_property("path", {"type": ["string", "array"], "items": {"type": "string"},
            "description": "An edge 'handle' (chains across TANGENT connections only; the 'path' "
                           "count is the truth), or 'sketch:<name>'."})
    .add_input_property(*_inputs.boolean_op(default="new").as_property())
    .add_input_property("orientation", {"type": "string", "enum": ["perpendicular", "parallel"]})
    .add_input_property("as_surface", {"type": "boolean"})
    .add_input_property("target_bodies", _TARGET_BODIES.schema())
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .strict_schema()
)
sweep_item = Item.create_tool_item(
    tool=sweep_tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="geometry",
        evidence_test="tests/unit/test_model_sweep.py::TestCutMovesMaterial"
                      "::test_unscoped_cut_that_moves_no_volume_is_an_error"))


def register_tool():
    register(sweep_item)
