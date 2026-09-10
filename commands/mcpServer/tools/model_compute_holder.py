# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: turn a solid holder model into a CAM TOOL HOLDER profile.

  model_compute_holder -> reduce a body of revolution to a stack of (height, lower/upper-diameter)
                    segments + holder library JSON, from three find_geometry handles (body, axis,
                    end datum). Read-only - computes + returns; does NOT write to a tool library.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _inputs
from . import _holder

app = adsk.core.Application.get()

# body = a SOLID body (handle preferred - holder bodies are auto-named); axis + end_datum are raw
# geometry handles resolved to live entities, then handed to the _holder axis/datum routines (which
# do the per-kind validation: a cyl/cone/edge axis, a normal planar/edge/vertex datum).
_BODY = _inputs.BodyRef("body", kind="solid", required=True)
_AXIS = _inputs.GeometryHandle("axis", require="any", required=True,
                               description="The axis of revolution.")
_END = _inputs.GeometryHandle("end_datum", require="any", required=True,
                              description="Sets z=0 on the axis.")


def handler(body: str = "", axis: str = "", end_datum: str = "",
            name: str = "", product_id: str = "", product_link: str = "", vendor: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Open the holder model first (see doc_open).")

    body_ent, berr = _BODY.resolve(body)
    if berr:
        return error(berr)
    axis_ent, aerr = _AXIS.resolve(axis)
    if aerr:
        return error(aerr)
    end_ent, eerr = _END.resolve(end_datum)
    if eerr:
        return error(eerr)

    # axis entity -> an InfiniteLine3D (the _holder routine validates the kind: cyl/cone face or
    # straight edge). A handle that resolves to something else returns None -> a precise error.
    axis_line = safe(lambda: _holder.get_axis(axis_ent))
    if axis_line is None:
        return error("'axis' must be a CYLINDRICAL or CONICAL face, or a straight EDGE - that handle "
                     "doesn't define an axis of rotation. Use find_geometry(kind=cylinder_face / "
                     "line_edge) on the holder.")

    # end datum -> the point where it meets the axis (must be normal/perpendicular to the axis).
    plane_intersect = safe(lambda: _holder.is_valid_axial_datum(end_ent, axis_line))
    if plane_intersect is None:
        return error("'end_datum' must be a PLANAR face (or edge/vertex) NORMAL to the axis - that "
                     "handle isn't a valid end datum for this axis. Pick the flat end face of the holder.")

    try:
        profile = _holder.get_tool_profile(body_ent, axis_line, plane_intersect)
    except Exception as e:
        return error(f"Could not reduce the body to a holder profile: {e}. (The body should be a "
                     "solid of revolution about the chosen axis.)")
    if not profile:
        return error("No holder profile could be derived - no coaxial faces reduced to segments. "
                     "Check that 'axis' is the true axis of revolution and the body is a turned holder.")

    holder_name = (name or "").strip() or (safe(lambda: app.activeDocument.name) or "Holder")
    holder_json = _holder.build_holder_data(profile, holder_name, product_id, product_link, vendor)
    segments = holder_json["segments"]

    return ok({
        "computed": True,
        "name": holder_name,
        "segment_count": len(segments),
        # the human-legible profile (mm) - what the holder will look like as a stack of bands
        "segments_mm": segments,
        # the full library JSON (type='holder'); add it to a tool library yourself (see note)
        "holder_json": holder_json,
        "note": "Holder profile computed (segments in mm: height, lower/upper diameter). This does "
        "NOT write to a tool library - take 'holder_json' and add it to a library yourself "
        "(a holder in a document is a FORK of library data, not a link). "
        "Pair with view_screenshot to confirm the body is the holder you meant.",
    })


TOOL_DESCRIPTION = (
    "Profile a solid holder body into CAM tool-holder segments, returned with 'holder_json'."
)

tool = (
    Tool.create_simple(name="model_compute_holder", description=TOOL_DESCRIPTION)
    .add_input_property(*_BODY.as_property())
    .add_input_property(*_AXIS.as_property())
    .add_input_property(*_END.as_property(brief=True))
    .add_input_property("name", {"type": "string",
            "description": "Default: the active document name."})
    .add_input_property("product_id", {"type": "string"})
    .add_input_property("product_link", {"type": "string"})
    .add_input_property("vendor", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
