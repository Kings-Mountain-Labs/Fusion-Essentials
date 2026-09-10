# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: place another INSTANCE of a component that already exists in this design -
the counterpart to model_create_component, which mints an empty one. WRITES.

Fusion numbers a new instance off a GLOBAL per-component counter ('BasePlate:2' - measured), so the
landed name and path are read back off the assembly tree instead of being predicted.
"""

import math

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import component_contains, error, occurrence_paths, ok, safe, scale
from . import _common
from . import _inputs
from . import _outputs

# collapse_ambiguous_occurrences is on because the target here IS the component: a name matching
# several of its instances is not an ambiguity to refuse, since instancing a component that already
# has instances is this tool's job.
_COMPONENT = _inputs.TargetRef("component", allow=("occurrence", "component"), required=True,
        collapse_ambiguous_occurrences=True)
_INTO_COMPONENT = _inputs.OccurrenceRef("into_component")

RETURNS = [
    _outputs.ReturnsName("full_path", of="new instance",
                         consumers=["joint_create", "assembly_move", "design_move_occurrence"]),
]


def _host_prefixes(paths, instance_name):
    """The distinct HOST-INSTANCE paths among `paths` - each path's portion above the new instance's
    own segment ('' = the root). This is how many hosts received the instance; the leftover paths do
    NOT count, because every host instance also contributes the new instance's own children."""
    out = set()
    for p in paths:
        parts = p.split("+")
        if instance_name in parts:
            out.add("+".join(parts[:parts.index(instance_name)]))
    return out


def handler(component: str = "", into_component: str = "", x: float = 0.0, y: float = 0.0,
            z: float = 0.0, units: str = "mm", rotate_deg: float = 0.0,
            rotate_axis: str = "z") -> dict:
    """See TOOL_DESCRIPTION."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")

    resolved, cerr = _COMPONENT.resolve(component)
    if cerr:
        return cerr if isinstance(cerr, dict) else error(cerr)
    entity, kind = resolved
    comp = safe(lambda: entity.component) if kind == "occurrence" else entity
    if comp is None:
        return error(f"Could not reach the component behind '{component}' to instance it.")
    comp_name = safe(lambda: comp.name) or component

    host_path = ""
    if (into_component or "").strip():
        into_occ, ierr = _INTO_COMPONENT.resolve(into_component)
        if ierr:
            return error(ierr)
        host = safe(lambda: into_occ.component)
        if host is None:
            return error(f"Occurrence '{into_component}' has no component to instance into.")
        host_path = safe(lambda: into_occ.fullPathName) or ""
        host_label = f"component '{safe(lambda: host.name)}' ({host_path})"
    else:
        host = safe(lambda: design.rootComponent)
        if host is None:
            return error("Could not reach the root component to instance into.")
        host_label = "the root component"

    # A component cannot hold an instance of itself (nor of anything it already sits inside): the
    # platform refuses it too, with '3 : add operation failed'. This guard is the named error.
    cycle = component_contains(comp, host)
    if cycle is True:
        return error(f"Refusing to instance '{comp_name}' into {host_label}: that target is "
                     f"'{comp_name}' itself or sits inside it, so the component would contain an "
                     "instance of itself. Pick a target outside it (omit 'into_component' for root).")
    if cycle is None:
        # The walk answers None for several distinct reads, so the wire says only what is common to
        # them: no verdict was reached.
        return error(f"Refusing to instance '{comp_name}' into {host_label}: whether that target "
                     f"already sits inside '{comp_name}' could not be determined - the subtree could "
                     "not be searched to a verdict, so the instance could make the component contain "
                     "itself. Check for unresolved external references with assembly_get, then "
                     "retry.")

    matrix = adsk.core.Matrix3D.create()
    if rotate_deg:
        axis_vec = _inputs._AXIS_VECS.get((rotate_axis or "z").strip().lower())
        if not axis_vec:
            return error(f"Unknown rotate_axis '{rotate_axis}'. Use x, y, or z.")
        # Rotate about the world origin; the translation set below places the instance. (Rotating
        # about the placement point would only bake a pivot correction into the translation column
        # that the next line overwrites anyway - net result is identical, so keep it explicit.)
        if not matrix.setToRotation(math.radians(float(rotate_deg)),
                                    adsk.core.Vector3D.create(*axis_vec),
                                    adsk.core.Point3D.create(0, 0, 0)):
            return error(f"Could not build the placement rotation ({rotate_deg} deg about "
                         f"'{rotate_axis}') - the instance was not created.")
    if x or y or z:
        matrix.translation = adsk.core.Vector3D.create(float(x) * k, float(y) * k, float(z) * k)

    before = occurrence_paths(design)
    occurrences = safe(lambda: host.occurrences)
    if occurrences is None:
        return error(f"Could not access the occurrences of {host_label} to instance into.")
    try:
        # The MUTATION - not safe-wrapped, so a refusal is reported instead of swallowed.
        occ = occurrences.addExistingComponent(comp, matrix)
    except Exception as e:
        return error(f"Could not instance '{comp_name}' into {host_label}: {e}")
    if not occ:
        return error(f"addExistingComponent returned nothing - no instance of '{comp_name}' was "
                     "created.")
    # read_flag: the ONE unreadable-flag read - None is unknown, so only a real False refuses.
    if _common.read_flag(lambda: occ.isValid) is False:
        return error(f"addExistingComponent returned an occurrence for '{comp_name}' but it reads "
                     "isValid=false - the instance did not land.")

    # Read the instance back off the assembly tree - the landed name is a READ, never a prediction.
    after = occurrence_paths(design)
    # An UNREADABLE walk yields the SAME empty set an empty assembly would, and the host occurrence
    # this instanced into proves the assembly is not empty - so an empty census is a failed read.
    # Naming "no new instance appeared" over it states a verdict the walk never delivered.
    if not after:
        return error(f"addExistingComponent returned an occurrence for '{comp_name}', but the "
                     "assembly census that confirms it could not be read - the instance may or may "
                     "not have landed. Re-read with design_get(include=['tree']).")
    new_paths = sorted(p for p in (after - before) if p)
    if not new_paths:
        return error(f"The call reported an occurrence for '{comp_name}' but no new instance "
                     "appeared in the assembly tree. Re-read with design_get(include=['tree']).")
    # The instance the CALLER asked for sits under the host occurrence it named; the other new paths
    # are the same instance under the host's OTHER instances ('Frame:1+Bolt:2' AND 'Frame:2+Bolt:2'
    # from one call), so the requested one is the primary.
    under_host = [p for p in new_paths if p.startswith(host_path + "+")] if host_path else new_paths
    full_path = (under_host or new_paths)[0]
    landed = full_path.split("+")[-1] or safe(lambda: occ.name)

    # Host instances are counted by their distinct PREFIXES, never by counting leftover paths: each
    # host instance also contributes the new instance's children, which are not host instances.
    children = [p for p in new_paths if p.startswith(full_path + "+")]
    host_instances = _host_prefixes(new_paths, landed) or {host_path}

    note = (f"Instanced '{comp_name}' - it LANDED as '{landed}' at '{full_path}'. Fusion numbers a "
            "new instance from a per-component counter across the whole design, so use the landed "
            "name/full_path, not a predicted one. The instance SHARES the component's geometry - "
            "editing either shows in both. Position it with assembly_move / joint_create; re-parent "
            "it with design_move_occurrence.")
    if children:
        note += (f" '{comp_name}' holds sub-components, so {len(children)} child path(s) came with "
                 f"it - a child KEEPS its own number ('{children[0].split('+')[-1]}').")
    if len(host_instances) > 1:
        note += (f" The host has {len(host_instances)} instances, so the one call landed the instance "
                 "under each of them (see 'paths').")
    # An x/y placement sets no pending-position flag, so assembly_capture_position refuses it with
    # "Nothing to capture", and in a design holding captured position markers a later joint creation
    # reverts the instance to the origin.
    if x or y or z or rotate_deg:
        note += (" The placement cannot be captured - it sets no pending-position flag, so "
                 "assembly_capture_position refuses it. In a design that HOLDS captured position "
                 "markers, a later joint creation can revert this instance to the ORIGIN. Read the "
                 "position back with model_inspect after the next joint creation, or place the "
                 "instance BY that joint instead of by x/y/z.")

    out = {
        "created": True,
        "component": comp_name,
        "occurrence": landed,
        "full_path": full_path,
        "paths": new_paths,
        "into_component": host_label,
        "position": {"x": x, "y": y, "z": z} if (x or y or z) else "origin",
        "rotate_deg": float(rotate_deg or 0.0),
        "rotate_axis": (rotate_axis or "z").lower() if rotate_deg else None,
        "units": units,
        "note": note,
    }
    return ok(out)


TOOL_DESCRIPTION = (
"Place another INSTANCE of a component already in this design - it SHARES the original's "
"geometry; placement coords in 'units', angles in degrees.\n"
+ _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="design_add_instance", description=TOOL_DESCRIPTION)
    .add_input_property(*_COMPONENT.as_property())
    .add_input_property(*_INTO_COMPONENT.as_property())
    .add_input_property("x", {"type": "number"})
    .add_input_property("y", {"type": "number"})
    .add_input_property("z", {"type": "number"})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("rotate_deg", {"type": "number",
            "description": "About 'rotate_axis'."})
    .add_input_property(*_inputs.frame_axis("rotate_axis", default="z",
            description="World axis.").as_property())
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_design_add_instance.py::TestHonesty"
                      "::test_an_occurrence_returned_with_an_unchanged_tree_is_an_error"))


def register_tool():
    register(item)
