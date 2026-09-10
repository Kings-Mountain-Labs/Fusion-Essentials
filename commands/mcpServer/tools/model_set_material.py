# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: assign a PHYSICAL material (a density-bearing adsk.core.Material) to a body,
occurrence, component, or the whole design. WRITES.

Distinct from appearance_set (cosmetic color): this one carries the mass and density model_inspect
reports.
"""

import difflib

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, iter_collection, ok, safe
from . import _common
from . import _inputs
from . import _materials
from . import _outputs

# A physical material lives on a BRepBody, a MeshBody or a Component (whole design = root
# component); a FACE carries none. MeshBody.material is settable and echoes on read-back, and an
# unassigned mesh carries a default steel density.
_TARGET = _inputs.TargetRef("target", allow=("body", "mesh", "occurrence", "component", "design"))

RETURNS = [
    _outputs.ReturnsName("material", of="material", consumers=["model_inspect"]),
    _outputs.ReturnsValue("density_kg_per_m3", "the assigned material's density - makes model_inspect mass trustworthy"),
]


def _catalog(design, selected=None):
    """Physical materials as (object, name, scope), optionally restricted to one catalog owner."""
    owners = ([selected] if selected else [(design, "document")] + [
        (lib, safe(lambda lib=lib: lib.name) or "library") for lib in _materials.libraries()])
    out = []
    for owner, scope in owners:
        for mat in iter_collection(_materials.collection(owner, "materials")):
            name = safe(lambda mat=mat: mat.name)
            if name:
                out.append((mat, name, scope))
    return out


def _find_material(design, name, library="", material_id=""):
    """Resolve an exact material name and optional source selectors, refusing multiple matches."""
    want = (name or "").strip()
    if not want:
        return None, None, "Provide 'material' - a name from design_get(include=['materials'])."
    selected = None
    if library:
        if library.strip().lower() == "document":
            selected = (design, "document")
        else:
            lib, lerr = _materials.find_library(library)
            if lerr:
                return None, None, lerr
            selected = (lib, safe(lambda: lib.name))
    catalog = _catalog(design, selected)
    if not catalog:
        return None, None, "No materials available in the requested catalog scope."
    wl = want.lower()
    exact = [(m, nm, scope) for m, nm, scope in catalog if nm.lower() == wl
             and (not material_id or safe(lambda m=m: m.id) == material_id)]
    doc_hits = [row for row in exact if row[2] == "document"]
    if doc_hits and not library:
        exact = doc_hits
    if not exact:
        names = sorted({nm for _m, nm, _s in catalog})
        near = difflib.get_close_matches(want, names, n=6, cutoff=0.4)
        hint = (" Nearest: " + ", ".join(f"'{n}'" for n in near)) if near else ""
        selector = f" with material_id='{material_id}'" if material_id else ""
        return None, None, f"No material named '{want}'{selector} in the requested catalog scope.{hint}"
    if len(exact) > 1:
        listed = ", ".join(f"'{nm}' (in {scope}, id={safe(lambda m=m: m.id)})"
                           for m, nm, scope in exact[:6])
        return None, None, (f"'{want}' is ambiguous: {listed}. Pass 'library' and 'material_id' "
                            "from design_get(include=['materials']); use library='document' "
                            "for a document copy.")
    mat, _name, scope = exact[0]
    return mat, scope, None


def _density_kg_per_m3(entity):
    """The entity's physical density in kg/m3, or None. PhysicalProperties.density is kg per cubic
    centimeter (the API's unit); x 1e6 converts to the kg/m3 humans read (steel ~ 7850)."""
    pp = safe(lambda: entity.physicalProperties)
    d = safe(lambda: pp.density) if pp is not None else None
    return round(d * 1e6, 3) if isinstance(d, (int, float)) else None


def _owner_name(body):
    """The name of the component a body belongs to, or None."""
    return safe(lambda: body.parentComponent.name)


def _body_label(row):
    """'<component>/<body>' for one applied/failed row - a body name is unique only inside its own
    component, so a design-wide walk can emit several rows reading 'Body1'."""
    comp = row.get("component")
    return f"{comp}/{row.get('body')}" if comp else str(row.get("body"))


def handler(target: str = "", material: str = "", library: str = "",
            material_id: str = "") -> dict:
    """Assign a physical material to the resolved target, then read back each body's material + density
    to prove it took. WRITES."""
    design = _common.design()
    if not design:
        return error("No active design with geometry.")

    mat, scope, merr = _find_material(design, material, library, material_id)
    if merr:
        return error(merr)

    resolved, terr = _TARGET.resolve(target)
    if terr:
        return terr if isinstance(terr, dict) else error(terr)
    entity, kind = resolved
    whole_design = kind == "design"
    if whole_design:
        kind = "component" # whole design = every component's bodies, not just the root's

    # Collect the bodies to assign to. A component/design/occurrence assigns PER BODY (so
    # model_inspect's per-body mass is trustworthy and a partial failure stays visible); a body target
    # is just that one body.
    comps, walked = None, None
    if kind in ("body", "mesh"):
        bodies = [(_owner_name(entity), entity)]
        desc = f"{'mesh ' if kind == 'mesh' else ''}body '{safe(lambda: entity.name)}'"
    elif kind == "occurrence":
        # BRep AND mesh bodies: an unreached mesh keeps default steel density, silently wrong mass.
        occ_owner = safe(lambda: entity.component.name)
        bodies = [(occ_owner, b) for b in
                  (list(iter_collection(safe(lambda: entity.bRepBodies)))
                   + list(iter_collection(safe(lambda: entity.component.meshBodies))))]
        desc = f"occurrence '{safe(lambda: entity.fullPathName) or safe(lambda: entity.name)}'"
    else: # component
        # An empty target is the WHOLE design: a body lives on the component that owns it, so a
        # root-only read misses every body held by a child component. all_components DEGRADES to
        # [root] when the design's own collection will not read, which `walked` keeps apart.
        walked = safe(lambda: design.allComponents) is not None if whole_design else None
        comps = _common.all_components(design) if whole_design else [entity]
        bodies = []
        for c in comps:
            cname = safe(lambda c=c: c.name)
            bodies += [(cname, b) for b in
                       (list(iter_collection(safe(lambda c=c: c.bRepBodies)))
                        + list(iter_collection(safe(lambda c=c: c.meshBodies))))]
        if not whole_design:
            desc = f"component '{safe(lambda: entity.name)}'"
        elif walked:
            desc = "the whole design (%d component(s))" % len(comps)
        else:
            desc = "the design's ROOT component (its component list did not read)"

    if not bodies:
        return error(f"{desc} has no bodies to assign a material to.")

    want_name = safe(lambda: mat.name)
    selected_id = safe(lambda: mat.id)
    if not want_name or not selected_id:
        return error("Selected material source identity is unavailable (name or id could not be read); "
                     "refusing mutation. Read design_get(include=['materials']) and retry with a "
                     "source carrying both fields.")
    selected_source = {"name": want_name, "id": selected_id, "library": scope}
    applied = []
    failed = []
    for owner, b in bodies:
        bname = safe(lambda b=b: b.name)
        try:
            # The MUTATION - NOT safe-wrapped so a genuine failure raises here and is reported per
            # body, never swallowed into a false success.
            b.material = mat
        except Exception as e:
            failed.append({"body": bname, "component": owner, "error": str(e)})
            continue
        # Verify the assigned material by exact readback of its source name and nonempty asset ID.
        actual = safe(lambda b=b: b.material)
        got = safe(lambda actual=actual: actual.name) if actual is not None else None
        got_id = safe(lambda actual=actual: actual.id) if actual is not None else None
        if got != want_name or got_id != selected_id:
            failed.append({"body": bname, "component": owner,
                           "error": (f"assignment effect is UNVERIFIED: selected name='{want_name}', "
                                     f"id='{selected_id}'; actual name='{got}', id='{got_id}'")})
            continue
        applied.append({"body": bname, "component": owner, "material": got,
                        "material_id": got_id,
                        "density_kg_per_m3": _density_kg_per_m3(b)})

    if not applied:
        first = (f"{_body_label(failed[0])}: {failed[0]['error']}" if failed else "unknown error")
        return error(f"No body assignment was verified for material '{want_name}' on {desc}: {first}.")

    note = (f"Physical material '{want_name}' assigned (source: {scope}). model_inspect mass/density "
            "now reflects this material. This is NOT color - use appearance_set for cosmetic color.")
    if whole_design and walked:
        note = (f"Empty target: assigned to the bodies of all {len(comps)} component(s) the design "
                "listed. " + note)
    elif whole_design:
        note = ("Empty target: the design's component list did not read, so only the ROOT "
                "component's bodies were reached - a child component's bodies keep the material "
                "they had. " + note)
    if failed:
        note = (f"Material assigned to {len(applied)} of {len(applied) + len(failed)} bodies; "
                f"{len(failed)} failed - see 'failed' ('<component>/<body>' names each). " + note)

    result = {
        "assigned": True,
        "target": desc,
        "kind": kind,
        "material": want_name,
        "source": scope,
        "selected_source": selected_source,
        "density_kg_per_m3": applied[0]["density_kg_per_m3"],
        "applied_to": applied,
        "note": note,
    }
    if whole_design:
        result["components_covered"] = len(comps)
        if not walked:
            # The count is the ROOT alone, not a design-wide census - a reader must not take it
            # for one.
            result["component_walk_complete"] = False
    if failed:
        result["failed"] = failed
    return ok(result)


_DESC = (
"Assign a PHYSICAL (density-bearing) material; appearance_set does color.\n"
+ _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="model_set_material", description=_DESC)
    .add_input_property(*_TARGET.as_property())
    .add_input_property("material", {"type": "string",
            "description": "Exact name from design_get materials."})
    .add_input_property("library", {"type": "string",
            "description": "Library name/id, or 'document'."})
    .add_input_property("material_id", {"type": "string",
            "description": "Source asset id; combine with material name."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="value",
        evidence_test="tests/unit/test_model_set_material.py::TestPartialSuccess"
                      "::test_a_swallowed_assignment_on_the_only_body_is_an_error"))


def register_tool():
    register(item)
