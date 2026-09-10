# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: stamp a closed sketch profile - or a sketch TEXT - onto the faces of a body.

  model_emboss -> part marking, nameplates, logos, ribs and recesses on a face; the SIGN of 'depth'
                  raises or engraves. createInput takes PLAIN PYTHON LISTS for both collection
                  arguments - an ObjectCollection in either position raises TypeError. WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _assert
from . import _geom
from . import _inputs
from . import _outputs
from . import _sketch_detail


# What this tool RETURNS (declared once; drives the PRODUCES: prose + the assert-present contract test).
RETURNS = [
    _outputs.ReturnsName("feature", of="feature", consumers=["design_delete_feature"],
                         absent_when="no_timeline_feature"),
]

# allow_text: createInput's profiles array takes Profile AND SketchText objects, so a nameplate's
# text - which carries no Profile of its own - is stamped as itself. scope_input names this tool's
# 'component' input, the remedy when two components carry one sketch name.
_PROFILES = _inputs.ProfileRefList("profiles", required=True, allow_text=True,
    scope_input="component")
_FACES = _inputs.GeometryHandleList("faces", require="face", required=True)
# EmbossFeatureInput carries NO operation property and createInput takes no operation argument, so
# the SIGN of depth is the whole raise-vs-engrave surface: a positive depth ADDS material. The
# volume-direction gate below refuses any call whose effect disagrees with the sign asked for.
_DEPTH = _inputs.Distance("depth", allow_zero=False, required=True)

app = adsk.core.Application.get()


def handler(profiles=None, faces=None, depth: float = 0.0, units: str = "mm",
            component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    scale_factor, uerr = _inputs.UNITS.resolve(units)
    if uerr:
        return error(uerr)
    depth_cm, derr = _DEPTH.resolve_scaled(depth, scale_factor)
    if derr:
        return error(derr)

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    prof_ents, perr = _PROFILES.resolve(profiles, component)
    if perr:
        return error(perr)
    face_ents, ferr = _FACES.resolve(faces)
    if ferr:
        return error(ferr)

    bodies = _geom.owning_bodies(face_ents)
    if not bodies:
        return error("'faces' resolved to face(s) with no readable owning body - cannot emboss.")
    # EmbossFeatureInput.inputFaces (API doc): several input faces must all be on the SAME body.
    # The names are read BEFORE the mutation: a post-mutation proxy can stop answering .name, and
    # the payload must not publish a null for a body it resolved.
    body_names = [safe(lambda b=b: b.name) for b in bodies]
    if len(bodies) > 1:
        listed = ", ".join(str(n) for n in body_names)
        return error(f"'faces' spans {len(bodies)} bodies ({listed}) - an emboss stamps the faces of "
                     "ONE body. Pass faces from a single body, one call per body.")

    # A profile-consuming createInput runs on the component that OWNS the profile's sketch: handing
    # another component's profile to features.createInput raises 'InternalValidationError : bSet'.
    host = _inputs.profile_host_component(prof_ents[0], None, comp)
    # So the faces must live in that same component. Stamping across components needs the input's
    # creationOccurrence, which nothing here sets - refuse by name rather than emboss the wrong
    # component. same_component compares by token: component wrappers are never identity-stable.
    face_comp = safe(lambda: bodies[0].parentComponent)
    together = _common.same_component(face_comp, host)
    if together is False:
        fname = safe(lambda: face_comp.name) or "unreadable"
        hname = safe(lambda: host.name) or "unreadable"
        return error(f"'faces' sit on a body in component '{fname}', but 'profiles' belong to "
                     f"component '{hname}' - an emboss is built on the profile's component, so both "
                     "must be the same one. Sketch the profile on the target body's component.")
    if together is None:
        # Not the refusal above: that one states the components DIFFER, which nothing here read.
        # Proceeding is not the safer half either - a cross-component createInput raises
        # 'InternalValidationError : bSet' from inside the API with nothing naming the cause.
        fname = safe(lambda: face_comp.name) or "unreadable"
        hname = safe(lambda: host.name) or "unreadable"
        return error(f"Whether the 'faces' body (component '{fname}') and the 'profiles' sketch "
                     f"(component '{hname}') belong to the SAME component could not be read, and an "
                     "emboss across two components is refused by Fusion at createInput. Pass faces "
                     "and profiles from one component - find_geometry with 'target' set to that "
                     "component names both.")

    # Pre-mutation sample: the volume the emboss must move, and in which direction.
    vol_before = _geom.volumes(bodies)

    mode = "raise" if depth_cm > 0 else "engrave"
    depth_val = adsk.core.ValueInput.createByReal(depth_cm)

    # PLAIN LISTS, not ObjectCollections - MEASURED: an ObjectCollection in either position raises
    # TypeError "argument 2/3 of type 'std::vector< adsk::core::Ptr< ... > > const &'".
    try:
        emboss_input = host.features.embossFeatures.createInput(
            list(prof_ents), list(face_ents), depth_val)
    except Exception as e:
        return error(f"Could not start the emboss: {e}")
    if not emboss_input:
        return error("EmbossFeatures.createInput returned nothing, so no emboss was attempted. "
                     "Re-check that the profiles sit over the target face(s).")

    try:
        feature = host.features.embossFeatures.add(emboss_input)
    except Exception as e:
        return error(f"Emboss failed: {e}")

    # A falsy add() carries no information in a DIRECT design; the volume verdict below needs no
    # feature object, so fall through to it there. In a parametric design it stays an honest error.
    direct_no_feature = _common.direct_feature_absence(design, feature)
    if not feature and not direct_no_feature:
        return error(_common.no_feature_error(design, "Emboss"))

    # A feature can be ADDED yet fail to compute; report that as failure, not a false ok.
    if safe(lambda: feature.healthState) == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:
        msg = safe(lambda: feature.errorOrWarningMessage) or "no detail"
        return error(f"Emboss was created but failed to compute: {msg}. Try a smaller depth, or move "
                     "the profile fully onto the target face(s). "
                     + _common.failed_effect_remedy(design, feature))

    # Post-mutation verdict. The volume delta is the ONLY evidence of which way the material went -
    # a feature object does not carry that - so an unreadable volume is a failure, not a success
    # with a caveat: this gate IS the tool's verdict.
    delta_total, any_readable = _geom.volume_delta(bodies, vol_before)
    if not any_readable:
        return error("Emboss raised no error, but the affected body's volume could not be read back "
                     "afterwards - whether the profile was raised or engraved is UNVERIFIED, so it "
                     "is reported as a failure. Re-read the body with model_inspect.")
    if abs(delta_total) < _common.NO_VOLUME_CHANGE_CM3:
        return error("Emboss reported success but the body's volume is unchanged - nothing was "
                     "raised or engraved. " + _common.failed_effect_remedy(design, feature))
    if (delta_total > 0) != (depth_cm > 0):
        moved = "removed" if delta_total < 0 else "added"
        return error(f"Emboss went the wrong way: depth {depth} {units} asked to {mode}, but the "
                     f"body {moved} {abs(round(delta_total, 6))} cm3 of material. "
                     + _common.failed_effect_remedy(design, feature))

    payload = {
        "embossed": True,
        "mode": mode,
        "profiles_requested": len(prof_ents),
        "faces_requested": len(face_ents),
        "body": body_names[0],
        "depth": round(float(depth), 6),
        "units": units,
        "note": "Profile stamped onto the face(s). 'mode' ECHOES the sign of the depth requested; "
                "the call is refused when the body's measured volume moves the other way, so the "
                "mode reported here is also the direction the material actually went.",
        "volume_delta_cm3": round(delta_total, 6),
    }
    # Direct mode: no feature object, so no name - publish the flag RETURNS declares the omission
    # against. The other keys survive the missing feature because none is read off it: mode, depth,
    # units and the two counts ECHO the request; body and volume_delta_cm3 are READ off the model.
    if direct_no_feature:
        payload["no_timeline_feature"] = True
        payload["note"] += " " + _common.DIRECT_FEATURE_NOTE
    else:
        payload["feature"] = safe(lambda: feature.name)
    # EmbossFeature.depth is a ModelParameter (not the ValueInput handed in), so the depth the
    # feature actually holds is read off its .value, in internal cm.
    applied_cm = safe(lambda: feature.depth.value) if feature else None
    if isinstance(applied_cm, (int, float)) and not isinstance(applied_cm, bool) and scale_factor:
        payload["applied_depth"] = round(applied_cm / scale_factor, 6)
    return ok(payload)


TOOL_DESCRIPTION = (
    "Stamp sketch profiles or text onto faces; a negative 'depth' engraves.\n"
    + _outputs.produces_block(RETURNS)
)

emboss_tool = (
    Tool.create_simple(name="model_emboss", description=TOOL_DESCRIPTION)
    .add_input_property(_PROFILES.name, _PROFILES.schema())
    .add_input_property(_FACES.name, _FACES.schema())
    .add_input_property(_DEPTH.name, _DEPTH.schema())
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .strict_schema()
)
emboss_item = Item.create_tool_item(tool=emboss_tool, write="write", handler=handler,
                                    run_on_main_thread=True,
                                    postconditions=[_assert.FeatureHealthy()],
                                    verification=Verification(
                                        kind="inline", rung="geometry",
                                        evidence_test="tests/unit/test_model_emboss.py::TestHonesty"
                                        "::test_unchanged_volume_reports_error_not_ok"))


def register_tool():
    register(emboss_item)
