# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: thicken faces into a SOLID wall - the surface->solid bridge. WRITES; the wall
is gated on the isSolid of the bodies owning the CREATED faces, and on its own landed thickness.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale, target_component
from . import _common
from . import _inputs
from . import _assert
from ._surface_common import _any_solid, _created_bodies, _landed_length

app = adsk.core.Application.get()

_THICKEN_OPS = ("new", "new_body", "join", "cut")
_THICKEN_TYPES = {
"sharp": "SharpThickenType",
"rounded": "RoundedThickenType",
}

_THICKEN_FACES = _inputs.GeometryHandleList("faces", require="face", required=True)


def handler(faces=None, thickness: float = 0.0, units: str = "mm",
            symmetric: bool = False, chaining: bool = True, operation: str = "new",
            thicken_type: str = "") -> dict:
    """Thicken faces into a SOLID wall - the surface->solid bridge."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    if thickness == 0:
        return error("Provide a non-zero 'thickness' to thicken.")
    op_key = (operation or "new").strip().lower()
    if op_key not in _THICKEN_OPS:
        return error(f"Unknown operation '{operation}'. Thicken supports: new, join, cut.")
    tt_key = (thicken_type or "").strip().lower()
    if tt_key and tt_key not in _THICKEN_TYPES:
        return error(f"Unknown thicken_type '{thicken_type}'. Use: sharp, rounded.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    face_ents, ferr = _THICKEN_FACES.resolve(faces)
    if ferr:
        return error(ferr)
    coll = adsk.core.ObjectCollection.create()
    for f in face_ents:
        coll.add(f)

    thick_val = adsk.core.ValueInput.createByReal(float(thickness) * k)
    op = getattr(adsk.fusion.FeatureOperations, _common.OPERATIONS[op_key])
    # For operation='join': the solids standing BEFORE the add. A join that fused lands its created
    # faces on one of THESE bodies; a join whose sheet touches no solid silently mints a NEW
    # free-floating body. The key is the identity PAIR - a bare token is document-local.
    join_host = (_common.census_host(safe(lambda: face_ents[0].body), comp)
                 if op_key == "join" and face_ents else None)
    before_keys = ({k for k in (_common.native_identity(b) for b in
                                _common.iter_collection(safe(lambda: join_host.bRepBodies)))
                    if k} if join_host is not None else set())
    try:
        thk_input = comp.features.thickenFeatures.createInput(coll, thick_val, bool(symmetric),
                                                              op, bool(chaining))
        # thickenType has no createInput slot; a fresh input's thickenType reads 0 (measured), so
        # an omitted value writes nothing.
        if tt_key:
            tt = safe(lambda: getattr(adsk.fusion.ThickenTypes, _THICKEN_TYPES[tt_key]))
            terr = _common.set_verified(thk_input, "thickenType", tt,
                                        f"thicken_type={tt_key}", "ThickenFeatureInput")
            if terr:
                return error(terr)
        feature = comp.features.thickenFeatures.add(thk_input)
    except Exception as e:
        return error(f"Thicken failed: {e}.")
    if not feature:
        return error(_common.no_feature_error(design, "Thicken"))

    # Gate on the bodies owning the faces the feature CREATED, not feature.bodies - the latter also
    # lists a pre-existing source solid (see _created_bodies), which would call a failed thicken
    # beside a solid 'closed'. Fall back to the coarse read only if the faces are unreadable.
    created, _face_count, readable = _created_bodies(feature)
    if readable and created:
        bodies = created
    else:
        bodies = _common.result_bodies(feature)
    names = [safe(lambda b=b: b.name) for b in bodies]
    any_solid = _any_solid(bodies)
    # An EMPTY result set is a failure, not a quiet success: with nothing to read isSolid off, the
    # solid gate below has nothing to judge and the payload would claim a wall it cannot show.
    if not bodies:
        return error("Thicken reported success but the feature owns no result body - no wall was "
                     "created, so there is nothing to read isSolid back off. "
                     + _common.failed_effect_remedy(design, feature))
    if any_solid is False:
        return error("Thicken reported success but no CREATED body reads isSolid=true - the wall "
                     "did not close into a solid. The feature remains in the timeline; inspect it "
                     "with model_inspect or remove it with design_delete_feature.")
    # The solid gate above proves a solid wall LANDED; it says nothing about how thick it is, which
    # is why the wall's own thickness parameter is read back here.
    landed, rerr = _landed_length(lambda: feature.thickness.value, float(thickness) * k, k,
                                  "wall", "thickness")
    if rerr:
        return error(rerr + " " + _common.failed_effect_remedy(design, feature))
    # The note states what the created body ACTUALLY read back - a hardcoded "isSolid=true" beside
    # an is_solid the payload could not read is the contradiction this wording exists to prevent.
    unverified = []
    if any_solid is True:
        note = ("Faces thickened into a wall reading back isSolid=true - a SOLID. The "
                "surface->solid bridge.")
    else:
        note = ("Faces thickened, but no created body's isSolid flag could be read back, so "
                "whether the wall closed into a SOLID is UNVERIFIED.")
        unverified.append("is_solid")
    payload = {
        "thickened": True,
        "feature": safe(lambda: feature.name),
        "operation": op_key,
        "result_bodies": names,
        "is_solid": any_solid,       # read off the CREATED bodies -> a failed closure errors above
        "thickness": round(float(thickness), 6),
        "units": units,
        "symmetric": bool(symmetric),
        "note": note,
    }
    if landed is None:
        unverified.append("thickness")
        payload["note"] += " Not read back off the feature: thickness."
    else:
        payload["thickness"] = landed
    if unverified:
        payload["unverified"] = unverified
    if tt_key:
        payload["thicken_type"] = tt_key
    # join no-fuse disclosure: a created body whose identity was NOT among the pre-add solids is a
    # NEW free-floating body, so operation='join' merged nothing. A body whose identity cannot be
    # read answers None, which is never in the census either, so it lands in `loose` and is warned.
    if op_key == "join" and readable and created:
        loose = [safe(lambda b=b: b.name) for b in created
                 if _common.native_identity(b) not in before_keys]
        if loose:
            payload["fused"] = False
            payload["disjoint_join"] = True
            payload["note"] += (" WARNING: operation='join' fused NOTHING - the wall landed as a "
                                f"NEW free-floating body ({', '.join(n for n in loose if n)}) "
                                "because the sheet touches no existing solid. Move it into contact "
                                "(model_move) and thicken again, or pass operation='new' when a "
                                "separate body is intended.")
    return ok(payload)


TOOL_DESCRIPTION = (
"Thicken faces into a solid wall."
)
tool = (
    Tool.create_simple(name="surface_thicken", description=TOOL_DESCRIPTION)
    .add_input_property("faces", _THICKEN_FACES.schema())
    .add_input_property("thickness", {"type": "number", "description": "In 'units'; non-zero."})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("symmetric", {"type": "boolean", "description": "Thicken both sides."})
    .add_input_property(*_inputs.boolean_op(options=("new", "join", "cut"), default="new").as_property())
    .add_input_property("chaining", {"type": "boolean", "description": "Select the connected face set."})
    .add_input_property(*_inputs.Choice("thicken_type", ["sharp", "rounded"],
        description="Corner treatment of the thickened wall.").as_property())
    .add_required_input("faces")
    .add_required_input("thickness")
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler,
                             run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy()],
                             verification=Verification(
                                 kind="inline", rung="geometry",
                                 evidence_test="tests/unit/test_surface_thicken.py"
                                               "::TestOffsetThickenKind"
                                               "::test_thicken_gate_names_the_created_body"))


def register_tool():
    register(item)
