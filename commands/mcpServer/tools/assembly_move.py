# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Translate (and optionally rotate) an occurrence by editing its transform - a free move, with no
joint or relationship created. WRITES."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale
from . import _common
from . import _inputs
# The world bbox-min corner of an instance's BODIES, over one shared position tolerance - the same
# reader design_move_occurrence judges its own world-position claim by.
from .design_move_occurrence import _POSITION_TOL_CM, _corner

# rotate_axis is an AxisRef: a world axis x/y/z, OR a straight-edge handle the rotation runs along.
_ROTATE_AXIS = _inputs.AxisRef("rotate_axis", default="z", description="For rotate_deg.")


def _occurrence_joint_names(occ):
    """Names of the joints an occurrence participates in (empty if none/unreadable)."""
    out = []
    for j in _common.iter_collection(safe(lambda: occ.joints)):
        nm = safe(lambda j=j: j.name)
        if nm:
            out.append(nm)
    return out


def handler(occurrence: str = "", dx: float = 0.0, dy: float = 0.0, dz: float = 0.0,
            rotate_deg: float = 0.0, rotate_axis: str = "z", units: str = "mm",
            rotate_x: float = 0.0, rotate_y: float = 0.0, rotate_z: float = 0.0,
            quiet: bool = False) -> dict:
    """Translate (and optionally rotate) an occurrence by editing its transform - a free move.
    rotate_deg/rotate_axis is a SINGLE rotation about a world axis or edge handle; rotate_x/y/z
    compose a MULTI-AXIS orientation (applied X then Y then Z). A jointed target gets a
    'jointed_warning' naming the joints unless quiet=true. WRITES."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    multi = (rotate_x or 0) or (rotate_y or 0) or (rotate_z or 0)
    if dx == 0 and dy == 0 and dz == 0 and (rotate_deg or 0) == 0 and not multi:
        return error("Provide a translation (dx/dy/dz), rotate_deg, or rotate_x/y/z - no movement specified.")
    if (rotate_deg or 0) and multi:
        return error("Use EITHER rotate_deg (single axis) OR rotate_x/y/z (multi-axis), not both.")
    design = _common.design()
    if not design:
        return error("No active design with components.")
    # The shared OccurrenceRef logic: an entityToken handle (the exact identity) or a
    # fullPathName/name, REFUSING an ambiguous one instead of grabbing the first instance.
    occ, occ_err = _inputs._resolve_occurrence(occurrence, occurrence)
    if not occ:
        return error(occ_err)

    # Moving a JOINTED occurrence poses it along its DOF - allowed (this is the sanctioned pose path),
    # but the pose is transient and a move that fights the joints over-constrains the solve. So we
    # proceed and WARN (naming the joints + the capture/probe next step) rather than refuse.
    joint_names = _occurrence_joint_names(occ)
    corner_before = _corner(occ)

    import math
    mat = adsk.core.Matrix3D.create()
    axis_desc = None
    try:
        if rotate_deg:
            # rotate_axis is an AxisRef: a world axis x/y/z (rotation axis through the occurrence's
            # current origin), OR a straight-edge handle (rotation about THAT edge's line - direction
            # AND a point both come from the edge, so you can swing a part about a real hinge edge).
            ax, aerr = _ROTATE_AXIS.resolve(rotate_axis)
            if aerr:
                return error(aerr)
            if ax[0] == "edge":
                pair, aerr = _inputs.axis_line_of("rotate_axis", ax[1])
                if aerr:
                    return error(aerr)
                pt, axis_dir = pair
                mat.setToRotation(math.radians(float(rotate_deg)), axis_dir, pt)
                axis_desc = "edge"
            else:
                axis_vec = ax[1]
                # rotate about the world axis through the occurrence's current origin
                t = safe(lambda: occ.transform2)
                origin = safe(lambda: t.translation.asPoint()) or adsk.core.Point3D.create(0, 0, 0)
                mat.setToRotation(math.radians(float(rotate_deg)),
                                  adsk.core.Vector3D.create(*axis_vec), origin)
                axis_desc = (rotate_axis or "z").strip().lower()
        elif multi:
            # compose X then Y then Z rotations about the occurrence's current origin
            t = safe(lambda: occ.transform2)
            origin = safe(lambda: t.translation.asPoint()) or adsk.core.Point3D.create(0, 0, 0)
            for ang, vec in ((rotate_x, (1, 0, 0)), (rotate_y, (0, 1, 0)), (rotate_z, (0, 0, 1))):
                if ang:
                    r = adsk.core.Matrix3D.create()
                    r.setToRotation(math.radians(float(ang)), adsk.core.Vector3D.create(*vec), origin)
                    mat.transformBy(r)
            axis_desc = "multi"
        # translation - COMPOSE it as its own matrix, never assign mat.translation directly:
        # setToRotation bakes a pivot-correction term into that column, and overwriting it rotates
        # the part about the WORLD origin instead of its own.
        if dx or dy or dz:
            vec = adsk.core.Vector3D.create(float(dx) * k, float(dy) * k, float(dz) * k)
            tmat = adsk.core.Matrix3D.create()
            tmat.translation = vec
            mat.transformBy(tmat)
        # compose onto the existing transform. transform2 (not transform) - it is the property
        # assembly_get's own read path prefers, with a live-verified bbox-correctness comment
        # (_occ_world in assembly_get.py); the read-modify-write here matches that same source of truth.
        base = safe(lambda: occ.transform2) or adsk.core.Matrix3D.create()
        before_arr = safe(lambda: tuple(base.asArray()))
        base.transformBy(mat)
        occ.transform2 = base
    except Exception as e:
        return error(f"Could not move '{safe(lambda: occ.name)}': {e}")

    # Read the pose back off the occurrence - the assignment can be accepted yet not take (a
    # grounded/jointed occurrence snaps back), and the payload must report the ACTUAL pose.
    after = safe(lambda: occ.transform2)
    after_arr = safe(lambda: tuple(after.asArray())) if after is not None else None
    requested = bool(dx or dy or dz or rotate_deg or multi)
    if (requested and before_arr is not None and after_arr is not None
            and after_arr == before_arr):
        return error(f"Move was accepted but '{safe(lambda: occ.name)}' reads an unchanged "
                     "transform - it did not move. A grounded/jointed occurrence can snap back: "
                     "free it (assembly_ground false) or pose it through its joint (joint_drive).")
    # An UNREADABLE transform is not a confirmation. With either side of the compare missing nothing
    # here read the move back, so publishing moved:true (with a null position beside it) would assert
    # an effect no read took - the same shape assembly_ground refuses when its flag will not read.
    if requested and (before_arr is None or after_arr is None):
        which = ("before and after" if before_arr is None and after_arr is None
                 else "before" if before_arr is None else "after")
        return error(f"'{safe(lambda: occ.name)}' was moved but its transform could not be read "
                     f"{which} the change, so the move is UNCONFIRMED - nothing here confirms the "
                     "occurrence actually moved, and it may have snapped back. Re-read the position "
                     "with assembly_get (occurrence origin) or model_inspect.")
    # The transform is a CLAIM; the part's own body corner is the EVIDENCE. Judged only where a
    # TRANSLATION bigger than the read noise was asked for - a rotation can leave a symmetric part's
    # world box exactly where it read before, and a sub-tolerance nudge moves nothing measurable.
    asked_cm = ((float(dx) * k) ** 2 + (float(dy) * k) ** 2 + (float(dz) * k) ** 2) ** 0.5
    corner_after = _corner(occ)
    carried = None
    if asked_cm > _POSITION_TOL_CM and corner_before is not None and corner_after is not None:
        carried = max(abs(a - b) for a, b in zip(corner_before, corner_after))
        if carried <= _POSITION_TOL_CM:
            return error(
                f"'{safe(lambda: occ.name)}' reads a CHANGED transform but its body geometry did "
                "NOT move - the reposition did not reach the part, and the transform it now reads "
                "is a claim nothing carried out. A pattern/mirror FEATURE re-derives its instances, "
                "overwriting a free move of one: position those through the owning feature "
                "(design_edit_timeline / the pattern's own tool) instead.")
    # Report the pose back in the caller's 'units' (translation.* is Fusion-internal cm; k is cm-per-unit).
    position = safe(lambda: {"x": round(after.translation.x / k, 4),
                             "y": round(after.translation.y / k, 4),
                             "z": round(after.translation.z / k, 4)}) if after is not None else None

    note = ("Occurrence repositioned (free move, no joint). This pose is UNCAPTURED - creating a joint "
            "ANYWHERE in the assembly (even on other parts) or a recompute can silently REVERT it; call "
            "assembly_capture_position to bake it into the timeline. Pair with view_screenshot / "
            "assembly_inspect_interference to check the new position.")
    result = {
    "moved": True,
    "occurrence": safe(lambda: occ.name),
    "position": position,
    "translation": {"x": dx, "y": dy, "z": dz},
    "rotate_deg": float(rotate_deg or 0.0),
    "rotate_axis": axis_desc if (rotate_deg or multi) else None,
    "rotate_xyz": ({"x": rotate_x, "y": rotate_y, "z": rotate_z} if multi else None),
    "units": units,
    }
    if carried is not None:
        # How far the part's own body corner travelled - measured, not the transform's word for it.
        result["geometry_moved_mm"] = round(carried * 10.0, 4)
    if joint_names and not quiet:
        result["jointed_joints"] = joint_names
        result["jointed_warning"] = (
            f"'{safe(lambda: occ.name)}' is in {len(joint_names)} joint(s) "
            f"({', '.join(joint_names[:6])}): this pose is TRANSIENT - call assembly_capture_position "
            "to keep it, and assembly_get to confirm the joints stayed healthy (a move that fights "
            "the joints over-constrains the solve).")
        note = "Occurrence posed (jointed - see jointed_warning). Pair with view_screenshot to view."
    result["note"] = note
    return ok(result)


TOOL_DESCRIPTION = (
"Move an occurrence by editing its transform - a free reposition, no joint. The pose is TRANSIENT "
"until assembly_capture_position bakes it in."
)
tool = (
    Tool.create_simple(name="assembly_move", description=TOOL_DESCRIPTION)
    .add_input_property("occurrence", {"type": "string", "description": "Occurrence name or full path."})
    .add_input_property("dx", {"type": "number", "description": "In 'units'."})
    .add_input_property("dy", {"type": "number", "description": "In 'units'."})
    .add_input_property("dz", {"type": "number", "description": "In 'units'."})
    .add_input_property("rotate_deg", {"type": "number", "description": "Degrees about 'rotate_axis', through the current origin."})
    .add_input_property("rotate_axis", _ROTATE_AXIS.schema())
    .add_input_property("rotate_x", {"type": "number", "description": "Degrees about world X (composed X->Y->Z)."})
    .add_input_property("rotate_y", {"type": "number"})
    .add_input_property("rotate_z", {"type": "number"})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("quiet", {"type": "boolean"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="geometry",
        evidence_test="tests/unit/test_assembly_move.py::TestBodyGeometryCarried"
                      "::test_a_transform_the_body_geometry_did_not_follow_bites"))


def register_tool():
    register(item)
