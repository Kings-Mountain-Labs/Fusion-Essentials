# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: re-parent an occurrence - move a component instance INTO another instance's
component without rebuilding it. Occurrence.moveToComponent takes an OCCURRENCE (a Component, root
included, raises) and returns a NEW proxy, while the handle held before the call keeps reading its
old path. WRITES.
"""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import component_contains, error, occurrence_paths, ok, safe
from . import _common
from . import _geom
from . import _inputs
from . import _outputs

_OCCURRENCE = _inputs.OccurrenceRef("occurrence", required=True)
_INTO_COMPONENT = _inputs.OccurrenceRef("into_component", required=True,
        description="Receives the instance.")

RETURNS = [
    _outputs.ReturnsName("full_path", of="moved occurrence",
                         consumers=["joint_create", "assembly_move"]),
]

_POSITION_TOL_CM = 0.001      # 0.01 mm - below this a world-position shift is read-back noise


def _corner(entity):
    """The world bbox-min corner of an entity's BODIES, as (x, y, z) in cm - or None when there is
    no measurable body geometry. The evidence behind the world-position claim: a re-parent keeps the
    part where it is (measured), and a corner that moved is what would disprove it."""
    bb = _geom.body_aabb(entity) if entity is not None else None
    mn = safe(lambda: bb.minPoint) if bb is not None else None
    if mn is None:
        return None
    vals = [safe(lambda a=a: getattr(mn, a)) for a in ("x", "y", "z")]
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in vals):
        return None
    return tuple(float(v) for v in vals)


def handler(occurrence: str = "", into_component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Open a document first (see doc_open / doc_new).")

    wanted = (into_component or "").strip()
    if not wanted or wanted.lower() == "root":
        # Measured: moveToComponent takes an OCCURRENCE ('argument 2 of type Ptr<Occurrence>'), and
        # the root component is not one - passing it raises. There is no root target for this call,
        # so moving an instance back OUT to the top level has no API path here.
        return error("'into_component' is required (an occurrence whose component receives the "
                     "instance). This call cannot move an instance back to the TOP LEVEL: the API "
                     "moves an occurrence into another OCCURRENCE, and the root component is not "
                     "one. To un-nest, delete the instance (design_delete_occurrence) and place a "
                     "new one at root with design_add_instance.")

    occ, oerr = _OCCURRENCE.resolve(occurrence)
    if oerr:
        return error(oerr)
    into_occ, ierr = _INTO_COMPONENT.resolve(wanted)
    if ierr:
        return error(ierr)
    target = safe(lambda: into_occ.component)       # the component - for the cycle guard ONLY
    if target is None:
        return error(f"Occurrence '{wanted}' has no component to move into.")
    target_path = safe(lambda: into_occ.fullPathName) or wanted
    target_label = f"'{target_path}' (component '{safe(lambda: target.name)}')"

    # Capture everything the result reports BEFORE the mutation: moveToComponent hands back a NEW
    # proxy, while the handle held here keeps answering with its pre-move path (measured - it goes
    # stale silently rather than raising), so no post-move read off `occ` can be trusted.
    name = safe(lambda: occ.name) or occurrence
    previous_path = safe(lambda: occ.fullPathName) or name
    moving = safe(lambda: occ.component)
    corner_before = _corner(occ)

    if moving is None:
        return error(f"Could not read the component behind '{name}' - refusing to move it.")
    # The instance's CURRENT parent is the second-to-last segment of its assembly path. (Not
    # sourceComponent: that is measured to be the ROOT component for every occurrence, however deeply
    # nested, so it cannot answer "where does this instance sit now".)
    parent_path = "+".join(previous_path.split("+")[:-1])
    if parent_path and parent_path == target_path:
        return ok({
            "moved": True,
            "changed": False,
            "occurrence": name,
            "previous_path": previous_path,
            "full_path": previous_path,
            "into_component": target_label,
            "note": f"'{name}' already sits in {target_label} - nothing changed.",
        })
    # A component cannot contain an instance of itself. Measured: the platform refuses this itself -
    # moveToComponent raises '3 : cannot move the occurrence to the component' and the tree is
    # unchanged. This guard is the earlier, named error, not a crash shield.
    cycle = component_contains(moving, target)
    if cycle is True:
        return error(f"Refusing to move '{name}' into {target_label}: that target is its own "
                     f"component '{safe(lambda: moving.name)}' or sits inside it, so the component "
                     "would contain an instance of itself. Pick a target outside it.")
    if cycle is None:
        # The walk answers None for several distinct reads, so the wire says only what is common to
        # them: no verdict was reached.
        return error(f"Refusing to move '{name}' into {target_label}: whether "
                     f"'{safe(lambda: moving.name)}' already sits inside that target could not be "
                     "determined - its subtree could not be searched to a verdict, so a move there "
                     "could make the component contain an instance of itself. Check the target for "
                     "unresolved external references with assembly_get, then retry.")

    before = occurrence_paths(design)
    try:
        # The MUTATION - not safe-wrapped, so a refusal is reported instead of swallowed. The
        # argument is the target OCCURRENCE (measured: a Component, root included, raises). The
        # RETURN is the moved occurrence - a new proxy whose fullPathName IS the true assembly path.
        moved = occ.moveToComponent(into_occ)
    except Exception as e:
        return error(f"Could not move '{name}' into {target_label}: {e}")
    if not moved:
        return error(f"moveToComponent returned nothing - '{name}' was not moved.")

    after = occurrence_paths(design)
    # An UNREADABLE assembly walk yields the SAME empty set an empty assembly would, and the move
    # just proved at least one occurrence exists - so an empty census here is a failed read.
    if not after:
        return error(f"moveToComponent ran for '{name}', but the assembly census that confirms it "
                     "could not be read - the move may or may not have taken. Check with "
                     "design_get(include=['tree']) before acting on this result.")
    new_paths = sorted(p for p in (after - before) if p)
    if previous_path in after and not new_paths:
        return error(f"The move reported success but the assembly is unchanged - '{name}' still "
                     f"reads '{previous_path}'. Re-read with design_get(include=['tree']).")
    landed = safe(lambda: moved.fullPathName)
    full_path = landed if landed in new_paths else (new_paths[0] if new_paths
                                                    else (landed or previous_path))

    corner_after = _corner(moved)
    preserved, shift = None, None
    if corner_before is not None and corner_after is not None:
        shift = max(abs(a - b) for a, b in zip(corner_before, corner_after))
        preserved = shift <= _POSITION_TOL_CM

    note = (f"Re-parented: '{previous_path}' -> '{full_path}'. A move keeps the part's WORLD "
            "position (measured) - it changes where the instance sits in the browser tree, not "
            "where the geometry is. Every path beneath it changed too, so re-read with "
            "design_get(include=['tree']) before using a path from before this call.")
    out = {
        "moved": True,
        "changed": True,
        "occurrence": name,
        "previous_path": previous_path,
        "full_path": full_path,
        "into_component": target_label,
        "world_position_preserved": preserved,
        "note": note,
    }
    if len(new_paths) > 1:
        out["paths"] = new_paths
    if preserved is False:
        out["position_warning"] = (
            f"The re-parent LANDED, but the part's geometry also moved "
            f"{round(shift * _common.CM_TO_UNIT['mm'], 4)} mm "
            "in world space - a re-parent is measured to keep the world position, so check the "
            "placement (assembly_move) before building on it.")
    return ok(out)


TOOL_DESCRIPTION = (
"Re-parent an occurrence into another occurrence's component.\n"
+ _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="design_move_occurrence", description=TOOL_DESCRIPTION)
    .add_input_property(*_OCCURRENCE.as_property())
    .add_input_property(*_INTO_COMPONENT.as_property(brief=True))
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="geometry",
        evidence_test="tests/unit/test_design_move_occurrence.py::TestWorldPosition"
                      "::test_a_part_that_drifted_is_reported_not_hidden"))


def register_tool():
    register(item)
