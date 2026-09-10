# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: a LIVE, factual index of the server's tool FAMILIES (the breadth map).

sys_capability_map() lists every tool family (by name prefix) with a one-line summary, its entry-point
tool, and tool count - read live from the registry so it can't drift - plus each capability NAME beside
the tool whose read answers it. Pair with sys_find_tool to search within a family. Read-only, no adsk.*.
"""

from ._common import ok
from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register, get_tools, family_of, has_tool, GATED_TOOLS

# Per-family FACTS: a one-line factual summary + the entry-point tool (the tool that starts that
# family's workflow - a fact about the family, not advice). Families not listed here still appear,
# derived from the registry, so a new family can't be silently omitted.
_FAMILY = {
    "sketch":    ("2D sketching: create sketches and add/constrain/dimension geometry.", "sketch_create"),
    "model":     ("Solid feature modeling: extrude/revolve/fillet/hole/pattern/combine + inspect/measure.", "model_extrude"),
    "surface":   ("Open (non-solid) surface modeling: extrude/revolve/patch/trim/thicken.", "surface_extrude"),
    "mesh":      ("Mesh bodies (STL/OBJ/3MF): import, edit, reduce/remesh, convert to BRep.", "mesh_insert"),
    "assembly":  ("Assembly kinematics: joints, grounding, move/capture, interference, probe.", "assembly_get"),
    "joint":     ("Joints between components: create/edit/drive joints and joint origins.", "joint_create"),
    "cam":       ("Manufacture (CAM): setups, operations, templates, tool libraries, generate toolpaths.", "cam_create_setup"),
    "data":      ("Cloud data model: hubs, projects, folders, files (create/list/upload/delete).", "data_get"),
    "drawing":   ("2D drawings from designs: sheets, placed views, dimensions, annotations, export.", "drawing_create"),
    "doc":       ("Document lifecycle: open/new/save/close/activate/copy + insert/update references.", "doc_get"),
    "design":    ("The active design as a whole: read structure, mode, recompute, configure, delete.", "design_get"),
    "view":      ("Viewport/camera: screenshots, sections, isolate/orient, workspace switch.", "view_screenshot"),
    "param":     ("User/model parameters: add/get/set/delete + favorites.", "param_get"),
    "sys":       ("Server/session utilities: find tools, this map, API docs, selection, the script hatch.", "sys_find_tool"),
    "find":      ("Find existing geometry (faces/edges/...) as stable handles for other tools.", "find_geometry"),
    "appearance":("Body/occurrence/face appearance (color) override.", "appearance_set"),
    "pmi":       ("PMI / 3D annotations on model geometry: leader notes with GD&T symbols, hole/thread callouts, imported PMI.", "pmi_get"),
    "workspace": ("Cold-boot orientation: where you are + what's here + pointers to the right deep tool.", "workspace_orient"),
    "save":      ("Tessellate a BRep body into a persistent mesh body in the design.", "save_as_mesh"),
}


# family-of-name is shared with the registry's family-gating helper (mcp_primitives/registry.py) -
# one definition, so this map and the gating checkboxes can never disagree on what a "family" is.
_family_of = family_of

# One entry per capability NAME - the names the live sweep gates steps on (tests/live/verify_core.py
# CAPABILITY_PROBES) - beside the tool whose read answers it. The map POINTS and that tool probes,
# so this module touches no adsk.* and answers even while the main thread is parked behind a modal.
_CAPABILITIES = {"machining_extension": "workspace_orient"}

_CAPABILITY_NOTE = ("read_with names the tool whose read answers that capability - call it for the "
                    "verdict, which is not taken here.")


def _capabilities_block():
    """{capability: {read_with: <tool>}} plus the note - the same disclose-then-point shape the
    family rows use, so a cold agent knows the capability exists and which read settles it."""
    return {
        "names": {name: {"read_with": tool} for name, tool in sorted(_CAPABILITIES.items())},
        "note": _CAPABILITY_NOTE,
    }


def handler() -> dict:
    """See TOOL_DESCRIPTION."""
    families = {}
    for item in get_tools():
        prim = getattr(item, "primitive", None)
        name = getattr(prim, "name", None)
        if not name:
            continue
        fam = _family_of(name)
        families.setdefault(fam, []).append(name)

    out = []
    for fam in sorted(families):
        members = sorted(families[fam])
        summary, entry = _FAMILY.get(fam, (None, None))
        # Fallbacks keep an UNMAPPED family honest: entry = a *_get/*_create if present, else the first
        # member; summary states only the fact that it's a family of N tools.
        if entry not in members:
            entry = next((m for m in members if m.endswith("_get") or "_create" in m), members[0])
        rec = {
            "family": fam,
            "summary": summary or f"{fam} tools.",
            "entry_tool": entry,
            "tool_count": len(members),
        }
        out.append(rec)

    # 'enabled_now' is a live registry check, not a settings-file read.
    gated_tools = [
        {"tool": name, "enabled_now": has_tool(name),
         "enable_path": f"Fusion Essentials Settings command -> MCP Server tab -> '{label}' checkbox"}
        for name, label in sorted(GATED_TOOLS.items())
    ]

    return ok({
        "family_count": len(out),
        "tool_count": sum(f["tool_count"] for f in out),
        "families": out,
        "capabilities": _capabilities_block(),
        "gated": {
            "tools": gated_tools,
            "note": ("A gated tool with enabled_now false is disabled here; use enable_path. "
                     "For a tool this map names as present, 'No such tool available' can mean a "
                     "stale client tool list or deny rule. Compare schema_fingerprint with the "
                     "prior map; after a change, reconnect or refresh tools, then exercise the "
                     "changed input. The fingerprint identifies server schema, not whether the "
                     "client consumed it."),
        },
        "note": ("The BREADTH map (what families exist + each one's entry tool). To go deeper, search "
                 "within a family with sys_find_tool (e.g. sys_find_tool('surface')). sys_get_guidance "
                 "carries this server's packaged design practice for building a part or an assembly. "
                 "Facts about the registry, not a recommended order."),
    })


TOOL_DESCRIPTION = (
    "Start here for help: an overview of every tool FAMILY, its entry tool and tool count, plus "
    "each capability name beside the tool whose read answers it. Then workspace_orient."
)

tool = Tool.create_simple(name="sys_capability_map", description=TOOL_DESCRIPTION).strict_schema()
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=False)


def register_tool():
    register(item)
