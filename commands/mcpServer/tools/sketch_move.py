# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: MOVE existing sketch entities by one transform in the sketch's own frame.
WRITES; the move is judged by the entities' coordinates, never by Sketch.move's own bool.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _assert
from . import _common
from . import _sketch_detail
from ._sketch_detail import _prepare, _requested, _transform_wire

app = adsk.core.Application.get()

# cm; a coordinate change below this is solver round-off, not a move.
_MOVE_EPS_CM = 1e-7


def _moved(before, after):
    """True when two position fingerprints differ by more than round-off, False when every readable
    sample matches, None when nothing comparable could be read (no verdict). A fingerprint is a
    tuple of (x, y, z) samples - box corners and endpoints - any of which is None for an entity that
    does not carry it, so only the samples present on BOTH sides are compared."""
    if before is None or after is None:
        return None
    pairs = [(b, a) for b, a in zip(before, after) if b is not None and a is not None]
    if not pairs:
        return None
    return any(abs(bv - av) > _MOVE_EPS_CM for b, a in pairs for bv, av in zip(b, a))


def handler(sketch_name: str = "", entities: str = "", units: str = "mm", dx=None, dy=None,
            rotation_deg=None, center_x=None, center_y=None, scale_factor=None,
            component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    design, sketch, ents, refs, coll, matrix, unit, err = _prepare(
        sketch_name, entities, units, dx, dy, rotation_deg, center_x, center_y, scale_factor,
        component)
    if err:
        return err

    name = safe(lambda: sketch.name)
    errors_before, _warn, _total = _common.timeline_health(design)
    before = [_assert.entity_position(e) for e in ents]
    try:
        did = sketch.move(coll, matrix)
    except Exception as e:
        return error(f"Could not move {', '.join(refs)} in sketch '{name}': {e}")
    # The bool cannot be the verdict: "Transform respects any constraints that would normally
    # prohibit the move", so a refused entity and a moved one share one true return. Coordinates
    # decide instead - and isFixed does NOT hold an entity still against an API move.
    after = [_assert.entity_position(e) for e in ents]
    verdicts = [_moved(b, a) for b, a in zip(before, after)]
    moved = [r for r, v in zip(refs, verdicts) if v is True]
    stayed = [r for r, v in zip(refs, verdicts) if v is False]
    unread = [r for r, v in zip(refs, verdicts) if v is None]

    if not did and not moved:
        return error(f"Fusion declined the move in sketch '{name}' (Sketch.move returned false) and "
                     f"none of {', '.join(refs)} changed position.")
    if not moved and not unread:
        return error(f"move returned {bool(did)} but {', '.join(refs)} read the same coordinates "
                     "afterwards, so nothing in the sketch changed. Either the transform is one "
                     "this geometry is symmetric under (a circle rotated about its own centre), or "
                     "a constraint refused it: sketch_get(include_entities=true) lists this "
                     "sketch's constraints and dimensions, and "
                     "sketch_delete_entity(target='constraint:<index>') removes one.")

    errors_after, _warn_after, _total_after = _common.timeline_health(design)
    broke = [n for n in errors_after if n not in errors_before]
    note = ("The entities keep their ids - a move adds and removes nothing, so no renumbering. "
            "Re-read sketch_get(include_entities=true) for the new coordinates.")
    if stayed:
        note = (f"Partial: {', '.join(stayed)} read the same coordinates afterwards - an existing "
                "constraint or dimension refused the move for those, or the transform leaves them "
                "where they were. " + note)
    if not did:
        note = ("Sketch.move returned false yet the coordinates changed - this reports what the "
                "geometry shows, not the return value. " + note)
    if unread:
        note += (f" {', '.join(unread)} could not be re-read, so its move is unconfirmed.")
    if broke:
        note += (" This edit put " + ", ".join(broke) + " into an error state - the feature(s) "
                 "downstream of this sketch no longer compute.")

    out = {"moved": True, "sketch": name, "entities": refs, "moved_entities": moved,
           "requested": _requested(unit, dx, dy, rotation_deg, scale_factor), "note": note}
    if stayed:
        out["unmoved_entities"] = stayed
    if unread:
        out["unverified_entities"] = unread
    if broke:
        out["downstream_broken"] = broke
    return ok(out)


TOOL_DESCRIPTION = (
    "MOVE existing sketch entities by one transform."
)

tool = _transform_wire(
    Tool.create_simple(name="sketch_move", description=TOOL_DESCRIPTION)
    .add_input_property("sketch_name", {"type": "string"})
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE))

item = Item.create_tool_item(tool=tool, write="write", handler=handler,
                             run_on_main_thread=True,
                             postconditions=[_assert.SketchCurvesChanged(
                                 scope_keys=("component",))])


def register_tool():
    register(item)
