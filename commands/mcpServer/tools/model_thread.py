# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: add a Thread feature to EXISTING cylindrical faces - external on a shaft or
boss, internal in a bore.
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
from . import _threads

app = adsk.core.Application.get()


RETURNS = [
    _outputs.ReturnsName("feature", of="feature", consumers=["design_delete_feature"]),
]

_FACES = _inputs.GeometryHandleList("faces", require="cylinder_face", required=True)
_LENGTH = _inputs.Distance("length", allow_zero=False, allow_negative=False,
    description="Thread only this much, from the 'location' end.")
_OFFSET = _inputs.Distance("offset", allow_zero=True, allow_negative=False)
_LOCATION = _inputs.Choice("location", ("high", "low"),
    description="'high' is where the axis points.")

_LOCATION_ATTRS = {"high": "HighEndThreadLocation", "low": "LowEndThreadLocation"}


def _face_is_internal(face):
    """True when `face` is a bore wall, False when it is a shaft/boss outer wall, None when the
    normal or the cylinder origin cannot be read. A cylinder's surface normal is purely radial, so
    dotting it with the face-point-to-axis-origin vector cancels the axial term and leaves the
    radial sign alone: a normal pointing AT the axis has the material outside it, which is a bore."""
    pt = safe(lambda: face.pointOnFace)
    # getNormalAtPoint returns the FACE's OUT-OF-MATERIAL normal and already accounts for
    # isParamReversed: a bore wall's sample points AT the axis, a shaft wall's away from it, so the
    # sign below needs no correction.
    nrm = _geom.evaluator_normal_at(face, pt)
    origin = safe(lambda: face.geometry.origin)
    if pt is None or nrm is None or origin is None:
        return None
    coords = [safe(lambda: origin.x), safe(lambda: origin.y), safe(lambda: origin.z),
              safe(lambda: pt.x), safe(lambda: pt.y), safe(lambda: pt.z)]
    if any(not isinstance(c, (int, float)) for c in coords):
        return None
    ox, oy, oz, px, py, pz = [float(c) for c in coords]
    dot = nrm[0] * (ox - px) + nrm[1] * (oy - py) + nrm[2] * (oz - pz)
    if abs(dot) < 1e-9:
        return None
    return dot > 0


def _sides(face_ents):
    """([internal-or-external per face], unreadable_indexes) for the resolved faces."""
    sides, unreadable = [], []
    for i, f in enumerate(face_ents):
        s = _face_is_internal(f)
        if s is None:
            unreadable.append(i)
        sides.append(s)
    return sides, unreadable


def _note(modeled):
    """The result note - what the caller still has to check, which differs by thread kind. Fusion
    accepts a designation that does not fit the cylinder (M30x3.5 on a 20 mm shaft computed with a
    clean health state), so the fit is the caller's to confirm."""
    if modeled:
        return ("Modeled thread cut into the existing cylinder(s). The cut is verified by volume, "
                "which catches a designation too large for a shaft or too small for a bore; a "
                "designation too large for a BORE also removes material and passes that check, so "
                "confirm the call-out against the bore diameter.")
    return ("Cosmetic thread added to the existing cylinder(s) - the call-out is recorded and the "
            "geometry stays a plain cylinder; pass modeled=true when the helix itself must be in "
            "the model. Fusion does not check a cosmetic call-out against the cylinder diameter.")


def handler(faces=None, designation: str = "", modeled: bool = False, left_handed: bool = False,
            length=None, offset=None, location: str = "", thread_type: str = "",
            thread_class: str = "", units: str = "mm") -> dict:
    """See TOOL_DESCRIPTION."""
    scale_factor, uerr = _inputs.UNITS.resolve(units)
    if uerr:
        return error(uerr)
    designation = (designation or "").strip()
    if not designation:
        return error("Provide 'designation' - the thread call-out, e.g. 'M8x1.25' or '1/4-20 UNC'.")

    length_cm, lerr = _LENGTH.resolve_scaled(length, scale_factor)
    if lerr:
        return error(lerr)
    offset_cm, oerr = _OFFSET.resolve_scaled(offset, scale_factor)
    if oerr:
        return error(oerr)
    if offset_cm is not None and length_cm is None:
        return error("'offset' positions a partial thread, so it needs 'length' too; without "
                     "'length' the thread runs the whole cylinder and the offset is ignored.")
    if location and length_cm is None:
        return error("'location' picks which end a partial thread is measured from, so it needs "
                     "'length' too.")
    # 'high' when omitted, which is NOT a schema default: a supplied location without 'length' is
    # refused above, so sending 'high' is not the same call as omitting it.
    loc_key, cerr = _LOCATION.resolve(location or "high")
    if cerr:
        return error(cerr)

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)
    if not comp:
        return error("No target component.")

    face_ents, ferr = _FACES.resolve(faces)
    if ferr:
        return error(ferr)

    sides, unreadable = _sides(face_ents)
    if unreadable:
        return error(f"Could not read the outward normal of face(s) {unreadable} (0-based), so "
                     "whether they are bores or shafts is unknown. Re-run find_geometry for fresh "
                     "handles and pass faces whose 'normal' it reports.")
    if len(set(sides)) > 1:
        internal_at = [i for i, s in enumerate(sides) if s]
        return error(f"One Thread feature cannot mix internal and external faces: face(s) "
                     f"{internal_at} (0-based) are bores and the rest are shafts. Thread each side "
                     "in its own call.")
    internal = bool(sides[0])

    thread_info, carried_by, terr = _threads.resolve_thread_info(
        comp, designation, internal=internal, thread_type=thread_type,
        thread_class=thread_class)
    if terr:
        return error(terr)

    threads = safe(lambda: comp.features.threadFeatures)
    if threads is None:
        return error("This component does not support thread features.")

    coll = adsk.core.ObjectCollection.create()
    for f in face_ents:
        coll.add(f)

    bodies = _geom.owning_bodies(face_ents) if modeled else []
    if modeled and not bodies:
        return error("'faces' resolved to face(s) with no readable owning body, so a modeled "
                     "thread's cut cannot be verified. Re-run find_geometry for fresh handles.")
    vol_before = _geom.volumes(bodies)

    try:
        tin = threads.createInput(coll, thread_info)
    except Exception as e:
        return error(f"Thread input could not be built for '{designation}': {e}")
    if not tin:
        return error(f"threadFeatures.createInput returned nothing for '{designation}'.")

    try:
        tin.isModeled = bool(modeled)
        tin.isRightHanded = not bool(left_handed)
        if length_cm is not None:
            # isFullLength defaults to true, which leaves the partial-thread trio unused.
            tin.isFullLength = False
            tin.threadLength = adsk.core.ValueInput.createByReal(length_cm)
            tin.threadOffset = adsk.core.ValueInput.createByReal(offset_cm or 0.0)
            loc_enum = getattr(adsk.fusion.ThreadLocations, _LOCATION_ATTRS[loc_key], None)
            if loc_enum is None:
                return error(f"ThreadLocations.{_LOCATION_ATTRS[loc_key]} is not available on this "
                             "Fusion version.")
            tin.threadLocation = loc_enum
    except Exception as e:
        return error(f"Could not apply the thread settings: {e}")

    try:
        feature = threads.add(tin)
    except Exception as e:
        return error(f"Thread failed for '{designation}': {e}")
    if not feature:
        return error(_common.no_feature_error(design, "Thread"))

    if safe(lambda: feature.healthState) == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:
        msg = safe(lambda: feature.errorOrWarningMessage) or "no detail"
        return error(f"The thread '{safe(lambda: feature.name)}' was created but failed to "
                     f"compute: {msg}. " + _common.failed_effect_remedy(design, feature))

    info = safe(lambda: feature.threadInfo)
    got = safe(lambda: info.threadDesignation) if info is not None else None
    if got is not None and got != designation:
        return error(f"The thread was created but carries designation '{got}', not the requested "
                     f"'{designation}'. Remove it with design_delete_feature (feature "
                     f"'{safe(lambda: feature.name)}').")
    # A second witness that costs one read: the platform validates the ThreadInfo's internal flag
    # against the face at add() and refuses a mismatch ("input face's externality is different from
    # what's in the ThreadInfo"), so by the time a feature exists this flag agrees with the face.
    got_internal = safe(lambda: info.isInternal) if info is not None else None
    if isinstance(got_internal, bool) and got_internal != internal:
        return error(f"The thread was created as an {'internal' if got_internal else 'external'} "
                     f"thread, but the face(s) are "
                     f"{'bores' if internal else 'shafts'}. Remove it with design_delete_feature "
                     f"(feature '{safe(lambda: feature.name)}').")

    payload = {
        "threaded": True,
        "feature": safe(lambda: feature.name),
        "faces": len(face_ents),
        "designation": got or designation,
        "internal": internal,
        # Read off the FEATURE, never defaulted to the request: falling back to what was asked for
        # turns an unreadable flag into a confirmation of itself, which is the one thing a
        # read-back may not do. None means unreadable, and the note below names it.
        "modeled": safe(lambda: bool(feature.isModeled)),
        "right_handed": safe(lambda: bool(feature.isRightHanded)),
        "thread_type": safe(lambda: info.threadType) if info is not None else None,
        "thread_type_alternatives": carried_by if len(carried_by) > 1 else None,
        "thread_class": safe(lambda: info.threadClass) if info is not None else None,
        "note": _note(modeled),
    }
    unread = [k for k in ("modeled", "right_handed") if payload[k] is None]
    if unread:
        payload["note"] += (" " + " and ".join(unread) + " could NOT be read back off the feature, "
                            "so " + ("they are" if len(unread) > 1 else "it is") + " reported as "
                            "null rather than as the value requested - confirm in Fusion.")
    if length_cm is not None:
        # threadLength/threadOffset are ModelParameters carrying centimetres.
        name = safe(lambda: feature.name)
        full = safe(lambda: feature.isFullLength)
        got_len = safe(lambda: feature.threadLength.value)
        got_off = safe(lambda: feature.threadOffset.value)
        got_loc = safe(lambda: feature.threadLocation)
        if not isinstance(full, bool) or not isinstance(got_len, float)                 or not isinstance(got_off, float):
            return error("A partial thread was requested, but the feature's own extent could not "
                         f"be read back, so there is no proof it took. Remove '{name}' with "
                         "design_delete_feature.")
        if full:
            return error(f"A partial thread was requested (length {length} {units}), but '{name}' "
                         "reads back as full length - the partial extent did not take. Remove it "
                         "with design_delete_feature.")
        for label, want_cm, got_cm in (("length", length_cm, got_len),
                                       ("offset", offset_cm or 0.0, got_off)):
            if abs(got_cm - want_cm) > 1e-6:
                return error(f"The thread was created but its {label} reads back "
                             f"{round(got_cm / scale_factor, 6)} {units}, not the requested "
                             f"{round(want_cm / scale_factor, 6)}. Remove '{name}' with "
                             "design_delete_feature.")
        if got_loc is not None and got_loc != loc_enum:
            return error(f"The thread was created but sits at the wrong end of the cylinder - "
                         f"'{loc_key}' was requested. Remove '{name}' with design_delete_feature.")
        payload["length"] = round(got_len / scale_factor, 6)
        payload["offset"] = round(got_off / scale_factor, 6)
        payload["location"] = loc_key
        payload["units"] = units

    if modeled:
        delta, readable = _geom.volume_delta(bodies, vol_before)
        if not readable:
            return error("The thread was created, but no affected body's volume could be read, so "
                         "there is no proof the helix was cut. The feature remains in the timeline; "
                         f"remove it with design_delete_feature (feature "
                         f"'{safe(lambda: feature.name)}').")
        if abs(delta) < _common.NO_VOLUME_CHANGE_CM3:
            return error("A modeled thread cuts the helix into the cylinder, but the affected "
                         "body's volume is unchanged - nothing was cut. The feature remains in the "
                         "timeline; remove it with design_delete_feature (feature "
                         f"'{safe(lambda: feature.name)}').")
        # A designation too big for a shaft, or too small for a bore, builds the thread form OUTSIDE
        # the cylinder and ADDS material; every designation that fits removes it. A designation too
        # large for a BORE also removes material, so this catches the growing mis-sizes only.
        if delta > 0:
            return error(f"A modeled thread cuts material away, but the body's volume GREW by "
                         f"{round(delta, 6)} cm3 - '{designation}' does not fit this cylinder, so "
                         "the thread form was built outside it. Check the designation against the "
                         "cylinder's diameter. The feature remains in the timeline; remove it with "
                         f"design_delete_feature (feature '{safe(lambda: feature.name)}').")
        payload["volume_delta_cm3"] = round(delta, 6)
    return ok(payload)


TOOL_DESCRIPTION = (
    "Thread an existing cylindrical face; model_hole taps its holes.\n"
    + _outputs.produces_block(RETURNS)
)

thread_tool = (
    Tool.create_simple(name="model_thread", description=TOOL_DESCRIPTION)
    .add_input_property(*_FACES.as_property())
    .add_input_property("designation", {"type": "string",
            "description": "e.g. 'M8x1.25'."})
    .add_input_property("modeled", {"type": "boolean",
            "description": "Default cosmetic; true cuts the helix."})
    .add_input_property("left_handed", {"type": "boolean"})
    .add_input_property(*_LENGTH.as_property())
    .add_input_property(*_OFFSET.as_property())
    .add_input_property(*_LOCATION.as_property())
    .add_input_property("thread_type", {"type": "string",
            "description": "When several standards carry it."})
    .add_input_property("thread_class", {"type": "string"})
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
thread_item = Item.create_tool_item(tool=thread_tool, write="write", handler=handler,
                                    run_on_main_thread=True,
                                    postconditions=[_assert.FeatureHealthy()],
                                    verification=Verification(
                                        kind="inline", rung="geometry",
                                        evidence_test="tests/unit/test_model_thread.py::TestHonesty"
                                        "::test_modeled_thread_that_cut_nothing_is_error"))


def register_tool():
    register(thread_item)
