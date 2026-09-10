# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that creates a RULED surface off an edge chain - the tapered extension behind a
draft-check face, a mold-parting transition, or a flared rim. WRITES. The new sheet is a SEPARATE
body from the one the edges came from, which the created feature's own `bodies` collection also
holds - so the result is the difference, not that collection (see _added_bodies).
"""

import math

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale, target_component
from . import _common
from . import _inputs
from . import _assert
from ._surface_common import _curve_host_component, _open_profile_from_curves, _solid_verdict

app = adsk.core.Application.get()

# ruled_type -> adsk.fusion.RuledSurfaceTypes member. The behaviour of each is MEASURED at angle 0 off
# a planar parent face, with the seed edge on its rim: TANGENT continued the parent face's own plane
# past the rim; NORMAL stood perpendicular to the parent; DIRECTION swept along the supplied entity.
_TYPES = {
    "tangent": "TangentRuledSurfaceType",
    "normal": "NormalRuledSurfaceType",
    "direction": "DirectionRuledSurfaceType",
}

_EDGES = _inputs.EdgeLoopRef("edges", closed=False, required=True)
_TYPE = _inputs.Choice("ruled_type", ["tangent", "normal", "direction"], default="tangent")
_DISTANCE = _inputs.Distance("distance", required=True)
# The direction entity is handed to createInput ITSELF, so it must be an ENTITY: a face handle would
# only yield a direction vector, which the call cannot consume - entity_only refuses one up front.
_DIRECTION = _inputs.AxisRef("direction", entity_only=True)


def _added_bodies(bodies, before_keys):
    """The members of `bodies` that were NOT in the component before the mutation, keyed by
    ``_common.native_identity`` and falling back to the body's bare NAME."""
    # RuledSurfaceFeature.bodies also holds the body the edges came from, so the feature's result
    # set is not the result. The two sides come from different collections, each minting its own
    # wrapper: a proxy's entityToken differs from its native's, but the identity and name do not.
    return [b for b in bodies
            if (_common.native_identity(b) or safe(lambda b=b: b.name)) not in before_keys]


def _resolve_direction(raw, host):
    """(direction entity, label, error) for the createInput direction argument: a world axis becomes
    that component's origin ConstructionAxis, an edge/sketch-line handle passes through as itself.
    Neither a BRepEdge nor a SketchLine carries a `name`, so the label is the entity's TYPE."""
    tagged, aerr = _DIRECTION.resolve(raw)
    if aerr:
        return None, None, aerr
    kind, value = tagged
    if kind == "world":
        key = raw.strip().lower()
        axis = _inputs.world_construction_axis(host, key)
        if axis is None:
            return None, None, (f"Could not read the {key}-axis off the component that owns the "
                                "edges, so there is no direction entity to sweep along.")
        return axis, f"{key}-axis", None
    return value, type(value).__name__, None


def _feature_readback(feature, want_type, type_key, dist_cm, ang_deg, k):
    """(fields, unverified, error) read off the CREATED RuledSurfaceFeature - ruledSurfaceType as
    the enum int, distance and angle as ModelParameters carrying CM and RADIANS. A value that
    cannot be read is named in `unverified` rather than assumed good."""
    fields, unverified = {}, []
    got_type = safe(lambda: feature.ruledSurfaceType)
    if got_type is None:
        unverified.append("ruled_type")
    elif got_type != want_type:
        return fields, unverified, (
            f"The ruled surface was created but its type reads back {got_type}, not the requested "
            f"'{type_key}' ({want_type}) - the surface does not leave the edge the way it was asked to.")

    got_dist = safe(lambda: feature.distance.value)
    if isinstance(got_dist, float):
        if abs(got_dist - dist_cm) > 1e-6:
            return fields, unverified, (
                f"The ruled surface was created but its distance reads back {round(got_dist / k, 6)}, "
                f"not the requested {round(dist_cm / k, 6)}.")
        fields["distance"] = round(got_dist / k, 6)
    else:
        unverified.append("distance")

    got_ang = safe(lambda: feature.angle.value)
    if isinstance(got_ang, float):
        deg = math.degrees(got_ang)
        if abs(deg - ang_deg) > 1e-6:
            return fields, unverified, (
                f"The ruled surface was created but its angle reads back {round(deg, 6)} deg, not "
                f"the requested {round(ang_deg, 6)}.")
        fields["angle_deg"] = round(deg, 6)
    else:
        unverified.append("angle_deg")
    return fields, unverified, ""


def ruled_handler(edges=None, ruled_type="tangent", distance=None, units="mm",
                  angle_deg=0.0, direction="") -> dict:
    """Sweep a ruled surface off an edge chain."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    type_key, terr = _TYPE.resolve(ruled_type)
    if terr:
        return error(terr)
    dist_cm, derr = _DISTANCE.resolve_scaled(distance, k)
    if derr:
        return error(derr)
    try:
        ang = float(angle_deg or 0.0)
    except (TypeError, ValueError):
        return error(f"'angle_deg' must be a number of degrees, got '{angle_deg}'.")

    # The direction entity and the type are ONE choice: a direction handed to createInput with
    # TangentRuledSurfaceType is IGNORED - the type still reads back tangent and the surface is
    # identical to the one built without it.
    wants_direction = direction not in (None, "", [])
    if type_key == "direction" and not wants_direction:
        return error("ruled_type='direction' needs a 'direction' entity - Fusion refuses to build the "
                     "input without one (measured: \"3 : invalid argument direction\").")
    if type_key != "direction" and wants_direction:
        return error(f"'direction' was given with ruled_type='{type_key}', which sweeps off the face "
                     f"the edge bounds - the direction is ignored, not applied. Pass "
                     f"ruled_type='direction' to sweep along '{direction}', or drop 'direction'.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    resolved, eerr = _EDGES.resolve(edges)
    if eerr:
        return error(eerr)
    coll, meta = resolved
    ents = meta["entities"]
    if not ents:
        return error("'edges' resolved to no edges. Pass find_geometry edge handles.")
    # Build the profile AND the feature on the component that OWNS the edges - a profile-consuming
    # feature created on the active component raises bSet when the source is owned elsewhere.
    host = _curve_host_component(ents, target_component(design))
    profile, perr = _open_profile_from_curves(host, ents)
    if perr:
        return error(perr)

    dir_entity, dir_label = None, None
    if wants_direction:
        dir_entity, dir_label, aerr = _resolve_direction(direction, host)
        if aerr:
            return error(aerr)

    want_type = safe(lambda: getattr(adsk.fusion.RuledSurfaceTypes, _TYPES[type_key]))
    if want_type is None:
        return error(f"RuledSurfaceTypes.{_TYPES[type_key]} is not available on this Fusion version.")
    # createByReal takes CENTIMETRES for the distance and RADIANS for the angle.
    dist_val = adsk.core.ValueInput.createByReal(dist_cm)
    angle_val = adsk.core.ValueInput.createByReal(math.radians(ang))

    ruled = host.features.ruledSurfaceFeatures
    # The census in identity form, resolved ONCE before the mutation: the feature's own result set
    # holds the parent body too, and a Features.*.add() that returns nothing leaves the model as
    # the only evidence. A count cannot say WHICH body is new.
    before_keys = {(_common.native_identity(b) or safe(lambda b=b: b.name))
                   for b in _common.iter_collection(safe(lambda: host.bRepBodies))}
    try:
        # MEASURED arity: the 4-argument form builds tangent/normal; DirectionRuledSurfaceType takes
        # the direction entity as a REQUIRED 5th argument.
        if dir_entity is not None:
            ruled_input = ruled.createInput(profile, dist_val, angle_val, want_type, dir_entity)
        else:
            ruled_input = ruled.createInput(profile, dist_val, angle_val, want_type)
        if not ruled_input:
            return error("Fusion built no ruled-surface input from those edges, so nothing was "
                         "created. Confirm the handles still resolve with find_geometry.")
        feature = ruled.add(ruled_input)
    except Exception as e:
        return error(f"Ruled surface failed: {e}. The measured working shape is an edge chain on ONE "
                     "body whose edges bound a face - tangent and normal are both measured off that "
                     "face, so an edge with no adjacent face has nothing to leave from.")

    if not feature:
        landed = _added_bodies(list(_common.iter_collection(safe(lambda: host.bRepBodies))),
                               before_keys)
        if not (_common.direct_feature_absence(design, feature) and landed):
            return error(_common.no_feature_error(design, "Ruled surface"))
        # Direct mode: no feature to name or read a type off - report what the model shows instead.
        facts = _common.body_facts(landed)
        direct_solid = _solid_verdict([f["is_solid"] for f in facts])
        payload = {
            "created": True,
            "ruled_type": type_key,
            "distance": round(dist_cm / k, 6),
            "angle_deg": round(ang, 6),
            "units": units,
            "edge_count": len(ents),
            "result_bodies": [f["name"] for f in facts],
            "is_solid": direct_solid,
            "bodies_added": len(landed),
            "unverified": (["ruled_type", "distance", "angle_deg"]
                           + (["is_solid"] if direct_solid is None else [])),
            "note": _common.DIRECT_FEATURE_NOTE,
        }
        if dir_label:
            payload["direction"] = dir_label
        return ok(payload)

    added = _added_bodies(_common.result_bodies(feature), before_keys)
    if not added:
        # Worded off what this branch actually saw - the feature's body set may be empty as well as
        # parent-only, and the error must not assert a shape it did not observe.
        where = safe(lambda: host.name) or "the component the edges belong to"
        return error("The ruled surface feature was created but added nothing: every body it reports "
                     f"was already in '{where}' before the call. "
                     + _common.failed_effect_remedy(design, feature))
    fields, unverified, rerr = _feature_readback(feature, want_type, type_key, dist_cm, ang, k)
    if rerr:
        return error(rerr + " " + _common.failed_effect_remedy(design, feature))

    # is_solid is read off the NEW body only - the parent in feature.bodies would otherwise decide
    # the verdict. _solid_verdict, not any(): each flag is True/False/None, and any() would fold an
    # unread flag into this payload's "an open sheet".
    facts = _common.body_facts(added)
    names = [f["name"] for f in facts]
    any_solid = _solid_verdict([f["is_solid"] for f in facts])
    if any_solid is None:
        unverified = unverified + ["is_solid"]

    payload = {
        "created": True,
        "feature": safe(lambda: feature.name),
        "ruled_type": type_key,
        "distance": round(dist_cm / k, 6),
        "angle_deg": round(ang, 6),
        "units": units,
        "edge_count": len(ents),
        "result_bodies": names,
        "is_solid": any_solid,     # read back off the body, not assumed (expected false for a sheet)
    }
    if dir_label:
        payload["direction"] = dir_label
    payload.update(fields)
    if unverified:
        payload["unverified"] = unverified
    if any_solid is True:
        payload["note"] = ("The result reads back SOLID (isSolid=true), not the open sheet a ruled "
                           "surface makes - inspect it before building on it.")
    elif any_solid is False:
        payload["note"] = ("New open surface body (isSolid=false), separate from the body the edges "
                           "came from. Join it with model_stitch, or thicken it with surface_thicken."
                           + (" Not read back off the feature: " + ", ".join(unverified) + "."
                              if unverified else ""))
    else:
        payload["note"] = ("The ruled surface was created, but no result body's isSolid flag could "
                           "be read back, so whether it is the open sheet a ruled surface makes or "
                           "a SOLID is UNVERIFIED - check it with model_inspect."
                           + " Not read back off the feature: " + ", ".join(unverified) + ".")
    return ok(payload)


_DESC = (
"Create a ruled surface off an edge chain."
)

surface_create_ruled_tool = (
    Tool.create_simple(name="surface_create_ruled", description=_DESC)
    .add_input_property(*_EDGES.as_property())
    .add_input_property(*_TYPE.as_property())
    .add_input_property(*_DISTANCE.as_property())
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("angle_deg", {"type": "number",
        "description": "Tilt off the ruled direction (default 0)."})
    .add_input_property(*_DIRECTION.as_property())
    .add_required_input("edges")
    .add_required_input("distance")
    .strict_schema()
)
surface_create_ruled_item = Item.create_tool_item(
    tool=surface_create_ruled_tool, write="write", handler=ruled_handler, run_on_main_thread=True,
    postconditions=[_assert.FeatureHealthy()],
    verification=Verification(
        kind="inline", rung="geometry",
        evidence_test="tests/unit/test_surface_create_ruled.py::TestResultBody"
                      "::test_a_result_set_holding_only_the_parent_is_an_error"))


def register_tool():
    register(surface_create_ruled_item)
