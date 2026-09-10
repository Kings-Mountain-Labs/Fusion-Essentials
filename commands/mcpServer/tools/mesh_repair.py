# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that REPAIRS a MESH body - MeshRepairFeatures (close holes, stitch and remove,
wrap, rebuild, one-touch fix). rebuild_method / density / offset are documented by the API as valid
ONLY for the rebuild repair type, so they are refused on the others before any mutation. The write
WRITES.
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
_TYPE = _inputs.Choice("repair_type",
                       ["close_holes", "stitch_and_remove", "wrap", "rebuild", "one_touch_fix"],
                       required=True)
_REBUILD = _inputs.Choice("rebuild_method",
                          ["fast", "preserve_sharp_edges", "accurate", "blocky", "adaptive",
                           "adaptive_preserve_sharp_edges"])
_OFFSET = _inputs.Distance("offset", allow_zero=True,
                           description="Offset from the original.")
_UNITS = _inputs.UnitField()

_SPEC = [_MESH, _TYPE, _REBUILD, _OFFSET, _UNITS]

# repair_type key -> the MeshRepairTypes member name.
_REPAIR_TYPES = {
    "close_holes": "CloseHolesMeshRepairType",
    "stitch_and_remove": "StitchAndRemoveMeshRepairType",
    "wrap": "WrapMeshRepairType",
    "rebuild": "RebuildMeshRepairType",
    "one_touch_fix": "OneTouchFixMeshRepairType",
}

# rebuild_method key -> the MeshRepairRebuildTypes member name.
_REBUILD_METHODS = {
    "fast": "FastMeshRepairRebuildType",
    "preserve_sharp_edges": "PreserveSharpEdgesMeshRepairRebuildType",
    "accurate": "AccurateMeshRepairRebuildType",
    "blocky": "BlockyMeshRepairRebuildType",
    "adaptive": "AdaptiveMeshRepairRebuildType",
    "adaptive_preserve_sharp_edges": "AdaptivePreserveSharpEdgesMeshRepairRebuildType",
}

# MeshRepairFeatureInput.density is the rebuild triangle density, bounded by the API at 8..256.
_DENSITY_MIN, _DENSITY_MAX = 8, 256

# The per-body facts the repair is judged on. Every one is read off the MeshBody (or its owning
# component) both before and after, so a field that reads None at either end simply drops out of the
# comparison instead of faking a verdict.
_FACT_KEYS = ("triangle_count", "vertex_count", "is_closed", "mesh_body_count")


def _facts(mb, comp) -> dict:
    """The readable state of `mb` (plus its component's mesh body count) - all reads."""
    return {
        "triangle_count": _tri_count(mb),
        "vertex_count": _node_count(mb),
        "is_closed": safe(lambda: bool(mb.isClosed)),
        "mesh_body_count": safe(lambda: comp.meshBodies.count),
    }


def _moved_facts(before: dict, after: dict):
    """(moved, comparable) - the fact keys that CHANGED, and those readable at both ends."""
    comparable = [k for k in _FACT_KEYS if before[k] is not None and after[k] is not None]
    return [k for k in comparable if before[k] != after[k]], comparable


def handler(mesh: str = "", repair_type: str = "", rebuild_method: str = "", density=None,
            offset=None, units: str = "mm") -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")

    vals, verr = _inputs.resolve_inputs(_SPEC, {
        "mesh": mesh, "repair_type": repair_type, "rebuild_method": rebuild_method,
        "offset": offset, "units": units})
    if verr:
        return verr
    mb, rtype, rmethod, offset_cm = (vals["mesh"], vals["repair_type"], vals["rebuild_method"],
                                     vals["offset"])

    given = [n for n, v in (("rebuild_method", rebuild_method), ("density", density),
                            ("offset", offset)) if v not in (None, "")]
    if rtype != "rebuild" and given:
        return error(f"{', '.join(given)} applies to repair_type='rebuild' only (got "
                     f"repair_type='{rtype}') - drop it, or switch repair_type to 'rebuild'.")
    if offset not in (None, "") and rmethod != "accurate":
        return error(f"'offset' applies to rebuild_method='accurate' only (got rebuild_method="
                     f"'{rmethod or 'fast'}').")

    dens = None
    if density not in (None, ""):
        try:
            dens = float(density)
        except (TypeError, ValueError):
            return error(f"'density' must be a number between {_DENSITY_MIN} and {_DENSITY_MAX} "
                         f"(got '{density}').")
        if not (_DENSITY_MIN <= dens <= _DENSITY_MAX):
            return error(f"'density' must be between {_DENSITY_MIN} and {_DENSITY_MAX} "
                         f"(got {dens}).")

    comp = _common.census_host(mb, _target_component(design))
    feats = safe(lambda: comp.features.meshRepairFeatures)
    if feats is None:
        return error("This design has no meshRepairFeatures collection (mesh repair unavailable "
                     "here).")

    before = _facts(mb, comp)
    before_volume = _geom.volumes([mb])

    # The design's OWN mode: a missing feature object is a fact about the RETURN, never evidence of
    # the mode, so the mode is read off the design itself.
    design_mode = _inputs.current_design_type(design)

    def inner_op(base_feature):
        try:
            inp = feats.createInput(mb)
        except Exception as e:
            return error(f"Could not create the mesh-repair input: {e}")
        if inp is None:
            return error("meshRepairFeatures.createInput returned nothing.")

        try:
            # A SWIG proxy ACCEPTS an assignment to a name it does not define and reads it back, so
            # a misspelled property would run the DEFAULT repair and report success - set_verified
            # reads the enum int back off the property that matters.
            rt = safe(lambda: adsk.fusion.MeshRepairTypes)
            serr = _common.set_verified(inp, "meshRepairType",
                                        safe(lambda: getattr(rt, _REPAIR_TYPES[rtype])),
                                        f"repair_type='{rtype}'", "MeshRepairFeatureInput")
            if serr:
                return error(serr)
            if rtype == "rebuild":
                if rmethod:
                    bt = safe(lambda: adsk.fusion.MeshRepairRebuildTypes)
                    serr = _common.set_verified(
                        inp, "meshRepairRebuildType",
                        safe(lambda: getattr(bt, _REBUILD_METHODS[rmethod])),
                        f"rebuild_method='{rmethod}'", "MeshRepairFeatureInput")
                    if serr:
                        return error(serr)
                # density and offset are typed core.ValueInput (createByReal speaks internal cm) and
                # do NOT survive set_verified - a read-back yields a different proxy - so they are
                # confirmed off the created feature's ModelParameter instead.
                if dens is not None:
                    inp.density = adsk.core.ValueInput.createByReal(dens)
                if offset_cm is not None:
                    inp.offset = adsk.core.ValueInput.createByReal(offset_cm)
        except Exception as e:
            return error(f"Could not configure the mesh-repair input: {e}")

        # Mutation - direct call, no safe() around it. A falsy return is NOT a failure: this add
        # "Returns nothing in the case where the feature is non-parametric" (a DIRECT design).
        # Success is judged on the mesh, not on the return.
        try:
            return feats.add(inp)
        except Exception as e:
            return error(f"Mesh repair failed (meshRepairFeatures.add raised): {e}")

    # No base-feature scope: live-measured, meshRepairFeatures.add() returns a real
    # MeshRepairFeature at plain parametric scope. This is unlike MeshRemoveFeatures, whose add
    # is parametric-only and must run OUTSIDE such a scope.
    feat = inner_op(None)
    if isinstance(feat, dict) and feat.get("isError") is True:
        return feat   # inner_op returned a _common.error()

    # A MeshFeature's bodies and faces "will always return null", so the repaired body is the input
    # mesh itself - the after-state is read straight off it.
    after = _facts(mb, comp)
    volume_delta_cm3, volume_readable = _geom.volume_delta([mb], before_volume)
    moved, comparable = _moved_facts(before, after)
    if volume_readable and volume_delta_cm3 != 0.0:
        moved.append("volume")

    if not comparable and not volume_readable:
        return error(f"The {rtype} repair raised no error, but nothing could be read back off the "
                     "mesh afterwards (triangle/vertex counts, is_closed and volume are all "
                     "unreadable) - the repair is UNVERIFIED, so it is reported as a failure.")

    # A repair that moved nothing is only a FAILURE when the mesh had that kind of defect to fix -
    # one_touch_fix on a clean closed box moves nothing and is correct to. is_closed is the only
    # defect state a MeshBody exposes, so it is the only one that can convict.
    closes_holes = rtype in ("close_holes", "one_touch_fix", "wrap")
    if not moved and closes_holes and before["is_closed"] is False:
        return error(f"The {rtype} repair reported success but the mesh is unchanged "
                     f"({before['triangle_count']} triangles, {before['vertex_count']} vertices) "
                     "and is STILL not watertight - the holes it was asked to close are still "
                     "there. Try repair_type='rebuild', or check the mesh with mesh_get.")

    units_key = (vals["units"] or "mm").strip().lower()
    inv_scale = _common.CM_TO_UNIT[units_key]
    payload = {
        "repaired": True,
        "mesh": safe(lambda: mb.name),
        "handle": safe(lambda: mb.entityToken),
        "repair_type": rtype,
        "feature": safe(lambda: feat.name) if feat else None,
        "design_mode": design_mode,
        "before": {k: before[k] for k in _FACT_KEYS},
        "after": {k: after[k] for k in _FACT_KEYS},
        "changed": moved,
        "volume_change": (round(volume_delta_cm3 * inv_scale ** 3, 6) if volume_readable else None),
        "units": units_key,
        "watertight": after["is_closed"],
    }
    if rtype == "rebuild":
        payload["rebuild_method"] = rmethod or None
        # density and offset are READ BACK off the feature's own ModelParameters, never echoed - a
        # request the API silently clamped or ignored would otherwise be published as a fact. A null
        # means the API's own default was left alone; unreadable is stated as such, not guessed.
        for key, want, scale in (("density", dens, 1.0), ("offset", offset_cm, inv_scale)):
            if want is None:
                payload[key] = None
                continue
            got = safe(lambda key=key: getattr(feat, key).value) if feat else None
            if got is None:
                payload[key] = None
                payload[f"{key}_unverified"] = f"{key} could not be read back off the feature."
                continue
            if abs(float(got) - float(want)) > 1e-6:
                return error(f"The rebuild was created with {key} = {got}, but {want} was requested "
                             f"- Fusion did not take the value. The mesh has been rebuilt at "
                             f"{got}; re-run with a {key} the API accepts, or undo in Fusion.")
            payload[key] = round(float(got) * scale, 6) if key == "offset" else float(got)

    note = "Mesh repaired. Re-read the body with mesh_get."
    if not moved:
        note = (f"Nothing changed: the mesh reads the same before and after "
                f"({before['triangle_count']} triangles, {before['vertex_count']} vertices, "
                f"is_closed={before['is_closed']}), so {rtype} found nothing of its kind to fix. "
                "A MeshBody exposes no defect count beyond is_closed, so this is reported as it "
                "was measured rather than judged.")
    elif rtype == "close_holes" and not after["is_closed"]:
        note = ("PARTIAL: the mesh changed but is still NOT watertight (is_closed false) - holes "
                "remain. Re-run close_holes or try repair_type='one_touch_fix', then check with "
                "mesh_get.")
    if feat is None:
        # This repair opens no base-feature scope (the add is parametric at plain scope), so the
        # helper's no-scope branch is the honest one outside a DIRECT design.
        note += " " + _common.null_feature_note(design, feat, None, "repair")
    payload["note"] = note
    return ok(payload)


TOOL_DESCRIPTION = (
    "Repair a MESH body - close holes, stitch, wrap, rebuild, or one-touch fix."
)

tool = (
    _inputs.apply_to_tool(
        Tool.create_simple(name="mesh_repair", description=TOOL_DESCRIPTION), _SPEC)
    .add_input_property("density", {"type": "number",
                                    "description": "Rebuild triangle density."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="geometry",
        evidence_test="tests/unit/test_mesh_repair.py::TestVerification"
                      "::test_close_holes_that_leaves_an_open_mesh_untouched_is_an_error"))


def register_tool():
    register(item)
