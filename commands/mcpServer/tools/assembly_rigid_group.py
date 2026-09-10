# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lock two or more component occurrences together as a single rigid unit (Rigid Group). WRITES."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _inputs
from . import _relations


def _resolve_many(design, names):
    """Resolve a comma string or list of occurrence handles/fullPathNames/names via the shared
    OccurrenceRef logic (handle-first, ambiguity-refusing). Returns (collection, resolved, errors)."""
    if isinstance(names, str):
        wanted = [n.strip() for n in names.split(",") if n.strip()]
    else:
        wanted = [str(n).strip() for n in (names or []) if str(n).strip()]
    coll = adsk.core.ObjectCollection.create()
    resolved, errors = [], []
    for want in wanted:
        o, err = _inputs._resolve_occurrence(want, want)
        if o is not None:
            coll.add(o)
            resolved.append(safe(lambda o=o: o.name))
        else:
            errors.append(err)
    return coll, resolved, errors


def _missing_members(rg, coll):
    """The requested occurrences the created group does NOT hold, by assembly path."""
    # Compared only when every member answered a label: a label that did not read is not a member
    # that is missing. include_children puts EXTRA members in the group, so this asks for a subset.
    held, total = _relations.rigid_group_members(rg)
    labelled = [h for h in held if h]
    if len(labelled) != total:
        return []
    wanted = [safe(lambda o=o: o.fullPathName) or safe(lambda o=o: o.name)
              for o in _common.iter_collection(coll)]
    return [w for w in wanted if w and w not in labelled]


def handler(occurrences: str = "", include_children: bool = False) -> dict:
    """Lock two or more occurrences together as one rigid unit; include_children also takes in
    their children. WRITES."""
    design = _common.design()
    if not design:
        return error("No active design with components.")
    coll, resolved, errors = _resolve_many(design, occurrences)
    if errors:
        return error("; ".join(errors))
    if coll.count < 2:
        return error("A rigid group needs at least two occurrences.")
    try:
        rg = design.rootComponent.rigidGroups.add(coll, bool(include_children))
    except Exception as e:
        return error(f"Could not create rigid group: {e}")
    if not rg:
        return error("Rigid group creation returned nothing.")
    member_count = safe(lambda: rg.occurrences.count)
    if member_count is not None and member_count < coll.count:
        return error(f"Rigid group '{safe(lambda: rg.name)}' was created but reports only "
                     f"{member_count} member(s) of the {coll.count} requested.")
    missing = _missing_members(rg, coll)
    if missing:
        gname = safe(lambda: rg.name)
        return error(f"Rigid group '{gname}' holds {member_count} member(s) but not "
                     f"{', '.join(missing)} - it locks parts that were not asked for. Remove it "
                     f"with assembly_edit_relations(kind='rigid_group', name='{gname}', "
                     "action='delete') and retry.")
    return ok({
    "assembly_rigid_group": safe(lambda: rg.name),
    "member_count": member_count,
    "grouped": resolved,
    "include_children": bool(include_children),
    "note": "Occurrences locked together as a rigid group.",
    })


TOOL_DESCRIPTION = (
"Lock two or more component occurrences together as a single rigid unit (Rigid Group)."
)
tool = (
    Tool.create_simple(name="assembly_rigid_group", description=TOOL_DESCRIPTION)
    .add_input_property("occurrences", {"type": "string", "description": "Occurrence name(s) to lock together (comma-separated)."})
    .add_input_property("include_children", {"type": "boolean", "description": "Also include the occurrences' children."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_assembly_rigid_group.py::TestMembersReadBack"
                      "::test_a_group_holding_the_right_count_of_the_wrong_parts_bites"))


def register_tool():
    register(item)
