# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that HOLLOWS a MESH body - MeshShellFeatures. The shell rewrites the SAME mesh
body in place (the name survives) and re-triangulates it, so the effect is read back off the input
body rather than off the feature (a MeshFeature reports no bodies of its own). WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from ._common import target_component as _target_component
from . import _geom
from . import _inputs
from ._mesh_common import _node_count, _tri_count

app = adsk.core.Application.get()

_MESH = _inputs.MeshBodyRef("mesh", required=True)
_THICKNESS = _inputs.Distance("thickness", required=True, allow_zero=False, allow_negative=False)
_UNITS = _inputs.UnitField()

_SPEC = [_MESH, _THICKNESS, _UNITS]

# The counts a shell moves: the re-triangulation is the loud half of the signal, the volume the
# meaning of it.
_COUNT_KEYS = ("triangle_count", "vertex_count")


def _counts(mb) -> dict:
    """The triangle and vertex counts of `mb` - all reads, each None when unreadable."""
    return {"triangle_count": _tri_count(mb), "vertex_count": _node_count(mb)}


def _closed(mb):
    """Whether `mb` is watertight NOW - True / False / None when the flag cannot be read."""
    return safe(lambda: bool(mb.isClosed))


def handler(mesh: str = "", thickness=None, units: str = "mm") -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")

    vals, verr = _inputs.resolve_inputs(_SPEC, {"mesh": mesh, "thickness": thickness,
                                                "units": units})
    if verr:
        return verr
    mb, thickness_cm = vals["mesh"], vals["thickness"]

    comp = _common.census_host(mb, _target_component(design))
    feats = safe(lambda: comp.features.meshShellFeatures)
    if feats is None:
        return error("This design has no meshShellFeatures collection (mesh shell unavailable "
                     "here).")

    # Captured BEFORE the mutation: the name a payload publishes and the counts the verdict rests on.
    mesh_name = safe(lambda: mb.name)
    before = _counts(mb)
    before_closed = _closed(mb)
    before_volume = _geom.volumes([mb])

    try:
        inp = feats.createInput(mb)
    except Exception as e:
        return error(f"Could not create the mesh-shell input: {e}")
    if inp is None:
        return error("meshShellFeatures.createInput returned nothing.")

    # thickness is a core.ValueInput speaking Fusion's internal cm. A ValueInput does NOT survive
    # set_verified (the read-back hands out a different proxy), so it is confirmed off the feature's
    # own ModelParameter after the add instead.
    try:
        inp.thickness = adsk.core.ValueInput.createByReal(thickness_cm)
    except Exception as e:
        return error(f"Could not set the shell thickness: {e}")

    try:
        feature = feats.add(inp)
    except Exception as e:
        return error(f"Mesh shell failed (meshShellFeatures.add raised): {e}")

    # meshShellFeatures.add returns a MeshShellFeature at PLAIN parametric scope (no BaseFeature
    # wrapper) and None in a DIRECT design while the hollow lands, so the census below is the
    # verdict and the return value never is.
    direct_no_feature = _common.direct_feature_absence(design, feature)
    if not feature and not direct_no_feature:
        return error(_common.no_feature_error(design, "Mesh shell"))

    after = _counts(mb)
    after_closed = _closed(mb)
    volume_delta_cm3, volume_readable = _geom.volume_delta([mb], before_volume)
    comparable = [k for k in _COUNT_KEYS if before[k] is not None and after[k] is not None]
    moved = [k for k in comparable if before[k] != after[k]]
    if volume_readable and volume_delta_cm3 != 0.0:
        moved.append("volume")

    if not comparable and not volume_readable:
        return error("Mesh shell raised no error, but nothing could be read back off the mesh "
                     "afterwards (triangle and vertex counts and volume are all unreadable) - the "
                     "hollow is UNVERIFIED, so it is reported as a failure.")
    if not moved:
        # The clause names only what was actually READ: a count that came back None, or a volume
        # neither end could report, must not appear in a sentence claiming it held still.
        seen = []
        if "triangle_count" in comparable:
            seen.append(f"{before['triangle_count']} triangles")
        if "vertex_count" in comparable:
            seen.append(f"{before['vertex_count']} vertices")
        if volume_readable:
            seen.append("the same volume")
        return error(f"Mesh shell reported success but '{mesh_name}' is unchanged "
                     f"({', '.join(seen)}) - nothing was hollowed. "
                     + _common.failed_effect_remedy(design, feature))

    units_key = (vals["units"] or "mm").strip().lower()
    inv_scale = _common.CM_TO_UNIT[units_key]
    # A hollow is a volume DROP on a body that is STILL closed: a body that stops reading watertight
    # reports the same drop because it encloses nothing (its volume reads 0.0), so the flag has to
    # SAY closed - an unreadable one cannot clear the body.
    closure_lost = before_closed is True and after_closed is False
    hollowed = bool(volume_readable and volume_delta_cm3 < 0.0 and after_closed is True)
    payload = {
        "hollowed": hollowed,
        "mesh": mesh_name,
        "handle": safe(lambda: mb.entityToken),
        "before": before,
        "after": after,
        "watertight": after_closed,
        "changed": moved,
        "volume_change": (round(volume_delta_cm3 * inv_scale ** 3, 6) if volume_readable else None),
        "units": units_key,
    }

    # thickness is READ BACK off the feature's own ModelParameter, never echoed: a value Fusion
    # clamped or ignored would otherwise be published as a fact about the wall that was cut.
    got = safe(lambda: feature.thickness.value) if feature else None
    if got is None:
        payload["thickness"] = None
        payload["thickness_unverified"] = "thickness could not be read back off the feature."
    elif abs(float(got) - float(thickness_cm)) > 1e-6:
        return error(f"The shell was created with thickness = {round(float(got) * inv_scale, 6)} "
                     f"{units_key}, but {round(thickness_cm * inv_scale, 6)} {units_key} was "
                     f"requested - Fusion did not take the value. "
                     + _common.failed_effect_remedy(design, feature))
    else:
        payload["thickness"] = round(float(got) * inv_scale, 6)

    if hollowed:
        note = "Mesh hollowed in place - the same body, re-triangulated. Re-read it with mesh_get."
    elif closure_lost:
        note = ("The shell left '" + str(mesh_name) + "' NO LONGER watertight (is_closed went true "
                "-> false), so this is a loss of closure, NOT a hollow. ")
        if volume_readable:
            note += (f"A body that reads open encloses nothing, so the volume change of "
                     f"{payload['volume_change']} {units_key}3 measures the closure going away "
                     "rather than material coming out. ")
        else:
            note += "Its volume could not be read at both ends, so there is no change to report. "
        note += ("Undo in Fusion, or repair the body with "
                 "mesh_repair(repair_type='close_holes').")
    elif after_closed is not True:
        note = ("The body does not report itself watertight after the shell, so a volume drop "
                "cannot be read as material coming out - the hollow is NOT confirmed. Check the "
                "body with mesh_get.")
    elif volume_readable:
        note = (f"The mesh changed but its enclosed volume did not drop (volume_change "
                f"{payload['volume_change']}), so the hollow is NOT confirmed by volume - check "
                "the body with mesh_get.")
    else:
        note = ("The mesh changed but its enclosed volume could not be read at both ends, so the "
                "hollow is NOT confirmed by volume - check the body with mesh_get.")
    if direct_no_feature:
        payload["no_timeline_feature"] = True
        note += " " + _common.DIRECT_FEATURE_NOTE
    else:
        payload["feature"] = safe(lambda: feature.name)
    payload["note"] = note
    return ok(payload)


TOOL_DESCRIPTION = (
    "Hollow a MESH body in place - the BRep model_shell cannot reach a mesh."
)

tool = _inputs.apply_to_tool(
    Tool.create_simple(name="mesh_shell", description=TOOL_DESCRIPTION), _SPEC).strict_schema()
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="geometry",
        evidence_test="tests/unit/test_mesh_shell.py::TestVerification"
                      "::test_a_shell_that_changed_nothing_is_an_error"))


def register_tool():
    register(item)
