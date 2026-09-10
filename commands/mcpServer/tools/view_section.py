# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: cut the model with a section plane so the agent can see INSIDE.

view_section(action=cut|list|clear) creates/lists/removes a non-destructive Section Analysis (Inspect >
Section Analysis) and auto-aims the camera at the exposed cut face. Pair with view_set
(orient/isolate) and view_screenshot.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _inputs
from . import _view_common

app = adsk.core.Application.get()

# PlaneRef for a bare-plane cut (origin alias | construction name | planar-face handle) - the shapes
# it accepts are the kind's own contract note.
_PLANE = _inputs.PlaneRef("plane")

_ACTIONS = ("cut", "list", "clear")
_PLANES = {
"xy": "xYConstructionPlane", "top": "xYConstructionPlane",
"xz": "xZConstructionPlane", "front": "xZConstructionPlane",
"yz": "yZConstructionPlane", "right": "yZConstructionPlane",
}


def _find_occurrence(design, name):
    """Resolve a SINGLE occurrence by entityToken handle (the exact identity) or fullPathName/name via
    the shared OccurrenceRef logic - refuses an ambiguous path/name instead of cutting through the
    wrong instance. Returns (occurrence, error_or_None)."""
    return _inputs._resolve_occurrence("through", name)


# World normal of each origin plane, off the shared named-view table. The xz/front plane's
# construction-plane normal points TOWARD the front camera's eye, so it sources the look
# direction where xy/top and yz/right source the view direction.
_PLANE_NORMALS = {
"xy": _view_common.view_direction("top"), "top": _view_common.view_direction("top"),
"xz": _view_common.look_direction("front"), "front": _view_common.look_direction("front"),
"yz": _view_common.view_direction("right"), "right": _view_common.view_direction("right"),
}


def _aim_at_cut(normal, flipped):
    """Orient the camera to look straight at the exposed cut face (a section keeps the +normal
    half, so the revealing side sits along +normal; flip reverses it)."""
    nx, ny, nz = normal
    if flipped:
        nx, ny, nz = -nx, -ny, -nz
    vp = app.activeViewport
    cam = vp.camera
    t = cam.target
    import math
    e0 = cam.eye
    dist = math.sqrt((e0.x - t.x) ** 2 + (e0.y - t.y) ** 2 + (e0.z - t.z) ** 2) or 10.0
    cam.eye = adsk.core.Point3D.create(t.x + nx * dist, t.y + ny * dist, t.z + nz * dist)
    # up vector: +Z unless the cut normal IS Z (top/bottom), then use +Y
    if abs(nz) > 0.9:
        cam.upVector = adsk.core.Vector3D.create(0, 1, 0)
    else:
        cam.upVector = adsk.core.Vector3D.create(0, 0, 1)
    cam.isFitView = True
    vp.camera = cam
    vp.refresh()


def handler(action: str = "", plane: str = "", through: str = "", offset: float = 0.0,
            units: str = "mm", flip: bool = False, show_hatch: bool = True, auto_view: bool = True) -> dict:
    """See TOOL_DESCRIPTION."""
    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        return error(f"Unknown action '{action}'. Valid: {', '.join(_ACTIONS)}.")
    design = _common.design()
    if not design:
        return error("No active design. Open a document with design geometry first.")
    sections = design.analyses.sectionAnalyses

    if action == "list":
        items = []
        for s in _common.iter_collection(sections):
            items.append({"name": safe(lambda s=s: s.name),
        "visible": safe(lambda s=s: s.isLightBulbOn)})
        return ok({"action": "list", "count": len(items), "sections": items})

    if action == "clear":
        # The pre-count is what "all removed" is judged against; an unreadable one leaves nothing to
        # walk and nothing to claim, so it refuses rather than reporting an empty sweep as success.
        before = _common.counted(lambda: sections.count)
        if before is None:
            return error("Could not read how many section analyses exist - nothing was removed. "
                         "Retry, or delete them from the browser's Analysis folder.")
        removed = []
        refused = []
        # delete from the end (deleting shifts indices) - the walk MUTATES the collection it reads,
        # so it stays positional: iter_collection is a forward generator over a shrinking collection.
        for i in range(before - 1, -1, -1):
            s = sections.item(i)
            nm = safe(lambda s=s: s.name) or f"#{i}"
            if safe(lambda s=s: s.deleteMe(), False):
                removed.append(nm)
            else:
                refused.append(nm)
        app.activeViewport.refresh()
        after = _common.counted(lambda: sections.count)
        if refused:
            return error(f"deleteMe() refused {len(refused)} of {before} section analysis(es) "
                         f"({', '.join(refused[:5])}) - the model is STILL cut by those. "
                         f"{len(removed)} were removed. Delete the rest from the browser's "
                         "Analysis folder.")
        if after:
            return error(f"Removed {len(removed)} of {before} section analysis(es) but "
                         f"sectionAnalyses still reads {after} - the model may still be cut.")
        out = {"action": "clear", "removed_count": len(removed), "removed": removed,
               "sections_before": before, "sections_after": after,
               "note": "All section analyses removed - the model is no longer cut."}
        if after is None:
            out["note"] = (f"Removed {len(removed)} of {before} section analysis(es), but the "
                           "remaining count could not be read back - view_section(list) confirms "
                           "whether the model is still cut.")
        return ok(out)

    # --- cut ---
    root = design.rootComponent
    cut_entity = None
    desc = None
    pkey = None   # origin-alias key for auto-view normal; stays None for face/construction handles
    centered_on = None
    factor = _common.scale(units)
    if factor is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    base_offset_cm = float(offset) * factor   # display units -> cm

    if through:
        occ, occ_err = _find_occurrence(design, through)
        if not occ:
            return error(occ_err)
        # Cut on the chosen origin plane (default xz/front) positioned at the occurrence center:
        # use the plane normal-aligned center coordinate as the section distance.
        pkey = (plane or "xz").strip().lower()
        if pkey not in _PLANES:
            return error(f"Unknown plane '{plane}'. Valid: {', '.join(sorted(set(_PLANES)))}.")
        cut_entity = getattr(root, _PLANES[pkey])
        # The cut exists to expose the SOLID's interior, so it is centred on the bodies-only
        # extents; the plain boundingBox counts visible sketches and datums, and one large
        # construction rectangle beside a small body carries the cut clear of that body.
        bb, centered_on = _view_common.focus_box(occ)
        if bb is None:
            # No box, no centre - cutting anyway puts the plane at the bare origin and reports a
            # cut 'through' something it never measured. The sibling view_set refuses the same way.
            return error(f"'{safe(lambda: occ.name) or through}' has no readable bounding box, so "
                         "there is no centre to cut through and nothing was cut. Pass 'plane' with "
                         "an explicit 'offset' to place the cut yourself.")
        cx = (bb.minPoint.x + bb.maxPoint.x) / 2
        cy = (bb.minPoint.y + bb.maxPoint.y) / 2
        cz = (bb.minPoint.z + bb.maxPoint.z) / 2
        # distance along the plane's normal to reach the occurrence center
        normal_coord = {"xy": cz, "top": cz, "xz": cy, "front": cy, "yz": cx, "right": cx}[pkey]
        base_offset_cm += normal_coord
        desc = f"through '{safe(lambda: occ.name)}' on {pkey} plane"
    else:
        if not (plane or "").strip():
            return error("Provide 'plane' (an origin alias xy/xz/yz, a construction-plane name, or "
    "a planar-face handle from find_geometry) or 'through' (an occurrence).")
        # plane is a PlaneRef, resolved FOR THE ROOT and never the active component: a section is a
        # document-level view, and MEASURED, a sub-component's own native plane is refused by
        # sectionAnalyses.add with '3 : object is not in the assembly context of this component'.
        cut_entity, perr = _PLANE.resolve(plane, component=root)
        if perr:
            return error(perr)
        # If the plane is an origin alias, remember its key so auto_view can aim at the cut. For a
        # construction-plane name or a planar-face handle there's no fixed WORLD normal, so pkey stays
        # None and auto-aim is skipped (rather than raising) - the section is still created.
        alias = (plane or "").strip().lower()
        if alias in _PLANE_NORMALS:
            pkey = alias
        desc = f"plane '{(plane or '')[:16]}'"

    try:
        inp = sections.createInput(cut_entity, base_offset_cm)
        if flip:
            inp.flip = True
        inp.isHatchShown = bool(show_hatch)
        sec = sections.add(inp)
    except Exception as e:
        return error(f"Failed to create section ({desc}): {e}")
    if not sec:
        return error(f"Section creation returned nothing ({desc}).")
    app.activeViewport.refresh()

    # By default, aim the camera at the exposed cut face. Without this, the camera stays where it
    # was - frequently on the SOLID side, where the model looks uncut and you'd wrongly think the
    # section failed. Pass auto_view=false to keep your current camera.
    aimed = False
    if auto_view:
        normal = _PLANE_NORMALS.get(pkey)
        if normal:
            safe(lambda: _aim_at_cut(normal, bool(flip)))
            aimed = True

    out = {
        "action": "cut",
        "section": safe(lambda: sec.name),
        "where": desc,
        "offset": round(float(offset), 3),
        "units": units,
        "flipped": bool(flip),
        "auto_viewed": aimed,
        "note": ("Model is now cut" + (" and the camera is aimed at the cut face." if aimed else
                "; the camera was left where it was (auto_view=false).") +
            " Use view_screenshot to study the interior; flip=true cuts the other half; "
            "view_section(clear) removes the cut."),
    }
    if centered_on:
        out["centered_on"] = centered_on
    return ok(out)


TOOL_DESCRIPTION = (
    "Cut the model with a live Section Analysis to see inside - a cutaway view, not a geometry "
    "edit; 'clear' removes every section in the design."
)

tool = (
    Tool.create_simple(name="view_section", description=TOOL_DESCRIPTION)
    .add_input_property("action", {"type": "string", "enum": list(_ACTIONS)})
    .add_required_input("action")
    .add_input_property(*_PLANE.as_property())
    .add_input_property("through", {"type": "string",
            "description": "Occurrence to cut through its center."})
    .add_input_property("offset", {"type": "number",
            "description": "Along the plane normal, in 'units'."})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("flip", {"type": "boolean"})
    .add_input_property("show_hatch", {"type": "boolean"})
    .add_input_property("auto_view", {"type": "boolean",
            "description": "Aims at the exposed cut face."})
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_view_section.py::TestListClear"
                      "::test_a_surviving_section_after_every_delete_returned_true_is_an_error"))


def register_tool():
    register(item)

