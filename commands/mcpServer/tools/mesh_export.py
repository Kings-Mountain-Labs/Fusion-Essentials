# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: write geometry to a MESH file (STL / OBJ / 3MF) on local disk. The design is
never modified; a written file is verified against a pre-write snapshot of the output path.
"""

import os

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _assert
from . import _common
from . import _export
from . import _inputs

app = adsk.core.Application.get()

# format -> (file extension, ExportManager factory name). Each factory takes (geometry, filename).
_FORMATS = {
"obj": (".obj", "createOBJExportOptions"),
"3mf": (".3mf", "createC3MFExportOptions"),
"stl": (".stl", "createSTLExportOptions"),
}

# refinement -> the MeshRefinementSettings enum member name (set on the export options when present).
_REFINEMENTS = {
"high": "MeshRefinementHigh",
"medium": "MeshRefinementMedium",
"low": "MeshRefinementLow",
}

_EXPORT_TARGET = _inputs.BodyRef("target", kind="any", required=False,
                                 description="Omit = the whole design.")
_EXPORT_FORMAT = _inputs.Choice("format", options=list(_FORMATS), default="3mf")
_EXPORT_REFINE = _inputs.Choice("refinement", options=list(_REFINEMENTS), default="medium")
# STLExportOptions.unitType is sticky session state: an export that never assigns it writes the unit
# of the last explicit assignment made anywhere in the session, across documents. So the omitted
# case still ASSIGNS the default (mm, the unit mesh_insert defaults to) rather than leaving it alone.
_EXPORT_UNITS = _inputs.Choice("stl_units", options=list(_export.STL_UNIT_MEMBERS),
                               description="format=stl only; baked into the file.")


# ── mesh_export target resolution (broad: body handle/name, component/occurrence, whole design) ──

def _resolve_export_target(design, target):
    """Resolve 'target' -> (geometry, description, redirected_from_mesh, error): empty is the root
    component, a mesh body is redirected to its parent component, all-None is a plain miss."""
    root = safe(lambda: design.rootComponent)
    name = (target or "").strip() if isinstance(target, str) else ""
    if not name:
        return root, "whole design (root component)", False, None

    # A handle (or a name) that resolves to a real body - BRep OR mesh - via the shared resolver.
    body, berr = _EXPORT_TARGET.resolve(name)
    if body is not None and berr is None and not isinstance(body, str):
        if _inputs._is_mesh(body):
            # A bare MeshBody can't be export-written; route to its owning component (which does write
            # a file). Fall back to root if the parent can't be read.
            mesh_name = safe(lambda: body.name) or name
            comp = safe(lambda: body.parentComponent) or root
            comp_name = safe(lambda: comp.name) or "its component"
            return comp, (f"component '{comp_name}' (redirected from mesh '{mesh_name}', which cannot "
                          f"be export-written on its own)"), True, None
        return body, f"body '{safe(lambda: body.name) or name}'", False, None

    comp, comp_err = _export.find_component(design, name)
    if comp_err:
        return None, None, False, comp_err + (
            " Export one instance by its occurrence name/fullPathName, or pass a body handle from "
            "find_geometry (design_get(include=['tree']) lists the instances).")
    if comp:
        return comp, f"component '{name}'", False, None

    occ, occ_err = _inputs._resolve_occurrence("target", name)
    if occ is not None:
        return occ, f"occurrence '{safe(lambda: occ.name) or name}'", False, None
    if occ_err and _inputs.OCCURRENCE_MISS not in occ_err:
        return None, None, None, occ_err       # a refusal (several instances), not a miss

    return None, None, None, None


def _component_census(comp):
    """The redirected component's own body counts and child-occurrence count, null where a count
    did not read."""
    return {"mesh_bodies": _common.counted(lambda: comp.meshBodies.count),
            "brep_bodies": _common.counted(lambda: comp.bRepBodies.count),
            "child_occurrences": _common.counted(lambda: comp.occurrences.count)}


def _apply_refinement(opts, refine_key):
    """The refinement key when the options object reads it back after the set, else None."""
    mrs = safe(lambda: adsk.fusion.MeshRefinementSettings)
    member = _REFINEMENTS.get(refine_key)
    val = safe(lambda: getattr(mrs, member)) if (mrs is not None and member) else None
    if val is None:
        return None                      # this build carries no MeshRefinementSettings member
    return _export.applied_pair(opts, "meshRefinement", val, refine_key)[0]


def _apply_stl_units(opts, unit_key):
    """Set STLExportOptions.unitType and read it back: (applied key or None, verified)."""
    val = _export.stl_unit_enum(unit_key)
    if val is None:
        return _export.NOT_APPLIED       # this build carries no DistanceUnits member for the key
    return _export.applied_pair(opts, "unitType", val, unit_key)


def _units_not_landed(unit_key, subject):
    """The clause both export paths append when the unit's read-back disagreed with the set."""
    return f"stl_units '{unit_key}' did NOT land for {subject}: 'options_applied' is null."


def _units_unverified(unit_key, subject):
    """The clause both export paths append when the options object already read the requested unit
    before it was set, so the read-back could not have failed."""
    return (f"stl_units '{unit_key}' UNVERIFIED for {subject}: 'options_verified' is false - "
            f"re-import with mesh_insert units='{unit_key}'.")


def _wrote_in_units(unit_key):
    """The clause both export paths append when the unit did read back, naming it for mesh_insert."""
    return (f"The export options read back units '{unit_key}' - re-import with mesh_insert "
            f"units='{unit_key}'.")


def _refinement_not_landed(ref, subject):
    """The clause both export paths append when refinement's read-back disagreed with the set;
    'subject' is what it happened to ('this file', '2 of 3 file(s)')."""
    return f"Refinement '{ref}' did NOT land for {subject}: 'refinement' is null."


def _write_mesh_file(em, factory_name, fmt, geom, path, ref, unit_key):
    """Create options, apply refinement (and the STL unit), execute, and VERIFY a non-empty file THIS
    call wrote landed (execute() can return True while writing nothing, and a stale file from an
    earlier export can sit at the same path). Returns (size_or_None, applied_refinement, units_pair,
    error_str), units_pair being the unit's (applied_key_or_None, verified)."""
    factory = safe(lambda: getattr(em, factory_name))
    if factory is None:
        return None, None, _export.NOT_APPLIED, f"this build's ExportManager has no {factory_name}"
    before = _export.snapshot(path)      # the baseline that makes the landed check THIS call's proof
    try:
        opts = factory(geom, path)
    except Exception as e:
        return None, None, _export.NOT_APPLIED, f"could not create {fmt.upper()} options: {e}"
    applied = _apply_refinement(opts, ref)
    # STL is the only format this tool bakes a unit into; C3MF's options object carries no unitType.
    units = _apply_stl_units(opts, unit_key) if fmt == "stl" else _export.NOT_APPLIED
    try:
        did = em.execute(opts)
    except Exception as e:
        return None, applied, units, f"{fmt.upper()} export failed: {e}"
    if not did:
        return None, applied, units, f"{fmt.upper()} export returned false"
    size, verr = _export.verify_written(path, before)
    if verr:
        return None, applied, units, f"{fmt.upper()} {verr}"
    return size, applied, units, None


def handler(format: str = "3mf", file_path: str = "", target: str = "",
            refinement: str = "medium", stl_units: str = "",
            split_by_component: bool = False) -> dict:
    """Export 'target' (body/mesh/component/occurrence, or whole design) to 'file_path' as a mesh, or
    with split_by_component one file per top-level occurrence into the directory 'file_path'."""
    fmt, ferr = _EXPORT_FORMAT.resolve(format)
    if ferr:
        return error(ferr)
    ref, rerr = _EXPORT_REFINE.resolve(refinement)
    if rerr:
        return error(rerr)
    # 'mm' when omitted, which is NOT a schema default: an stl_units on another format is refused
    # below, so sending 'mm' is not the same call as omitting it.
    unit_key, uerr = _EXPORT_UNITS.resolve(stl_units or "mm")
    if uerr:
        return error(uerr)
    if (stl_units or "").strip() and fmt != "stl":
        return error(f"'stl_units' ('{unit_key}') applies to format=stl only, and this call asked "
                     f"for format={fmt} - refusing rather than dropping it. Export as stl to bake "
                     f"the unit into the file, or omit 'stl_units'.")
    if split_by_component and (target or "").strip():
        return error(f"'target' ('{target.strip()}') and split_by_component=true cannot be combined: "
                     "the split writes one file per TOP-LEVEL occurrence and would not narrow to "
                     "that target - refusing rather than dropping it. Omit 'target' to split the "
                     "whole design, or omit 'split_by_component' to export just that target.")
    ext, factory_name = _FORMATS[fmt]

    path = (file_path or "").strip().strip('"')
    if not path:
        return error("Provide 'file_path' - the local output path (a file, or a DIRECTORY when "
    "split_by_component=true). The format extension is appended if missing.")

    design = _common.design()
    if not design:
        return error("No active design to export. Open or create a document first (see doc_new).")

    # ---- per-component split: one mesh file per top-level occurrence into directory 'path' ----
    if split_by_component:
        em = safe(lambda: design.exportManager)
        if em is None:
            return error("This design exposes no exportManager - cannot export.")
        out_dir = path
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            return error(f"Could not create output directory '{out_dir}': {e}")
        occs = _export.top_level_occurrences(design)
        if occs is None:
            return error("The root component's occurrences did not read, so which components this "
                         "split would write one file each for is unknown - refusing rather than "
                         "reporting a zero-file export. Export without split_by_component to write "
                         "the whole design as one file.")
        if not occs:
            return error("No top-level occurrences to split - the design has no component instances. "
                         "Export without split_by_component to write the whole design as one file.")

        # Per output path, folded into split_by_occurrence's records below.
        applied_by_path = {}
        units_by_path = {}

        def _write_one(occ, fpath):
            size, applied, units, eerr = _write_mesh_file(em, factory_name, fmt, occ, fpath, ref,
                                                          unit_key)
            applied_by_path[fpath] = applied
            units_by_path[fpath] = units
            return size, eerr

        files, errors = _export.split_by_occurrence(occs, out_dir, ext, _write_one)
        if not files:
            return error(f"{fmt.upper()} split export wrote NO files - all "
                         f"{len(errors)} occurrence(s) failed: "
                         + _export.failure_detail(errors))
        for rec in files:
            # what LANDED for THIS file (each file got its own options object); null when the set
            # did not take, never the request echoed.
            rec["refinement"] = applied_by_path.get(rec.get("file_path"))
            if fmt == "stl":
                u_applied, u_verified = units_by_path.get(rec.get("file_path"), _export.NOT_APPLIED)
                rec["options_applied"] = {"stl_units": u_applied}
                rec["options_verified"] = {"stl_units": u_verified}
        unlanded = [rec for rec in files if rec["refinement"] is None]
        unlanded_units = [rec for rec in files
                          if (rec.get("options_applied") or {}).get("stl_units") is None
                          and fmt == "stl"]
        unverified_units = [rec for rec in files
                            if fmt == "stl"
                            and (rec.get("options_applied") or {}).get("stl_units") is not None
                            and not (rec.get("options_verified") or {}).get("stl_units")]
        head = (f"Exported {len(files)} component(s) to separate {fmt.upper()} mesh files - one "
                "per top-level occurrence.")
        out = {
            "exported": True,
            "format": fmt,
            "split_by_component": True,
            "directory": out_dir,
            "refinement_requested": ref,
            "file_count": len(files),
            "files": files,
        }
        if fmt == "stl":
            out["options_requested"] = {"stl_units": unit_key}
        if errors:
            # PARTIAL success: some occurrences produced no file. Disclosed as its own flag plus the
            # per-occurrence reasons, so a caller reading file_count alone cannot miss the shortfall.
            out["partial"] = True
            out["failed"] = errors
            head = (f"PARTIAL: {len(files)} of {len(files) + len(errors)} occurrence(s) exported; "
                    f"{len(errors)} produced NO file - see 'failed'.")
        clauses = []
        if unlanded:
            clauses.append(_refinement_not_landed(
                ref, f"{len(unlanded)} of {len(files)} file(s)"))
        if unlanded_units:
            clauses.append(_units_not_landed(
                unit_key, f"{len(unlanded_units)} of {len(files)} file(s)"))
        if unverified_units:
            clauses.append(_units_unverified(
                unit_key, f"{len(unverified_units)} of {len(files)} file(s)"))
        elif fmt == "stl" and not unlanded_units:
            clauses.append(_wrote_in_units(unit_key))
        out["note"] = " ".join([head] + clauses)
        return ok(out)

    # ---- single-target export ----
    if not path.lower().endswith(ext):
        path = path + ext

    geom, desc, redirected_from_mesh, terr = _resolve_export_target(design, target)
    if geom is None:
        return error(terr or
                     (f"Export target '{target}' not found. Pass a body HANDLE from find_geometry "
                      "(precise), a body/mesh/component/occurrence NAME, or omit 'target' to export "
                      "the whole design."))

    # make sure the destination directory exists
    out_dir = os.path.dirname(path)
    if out_dir and not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            return error(f"Could not create output directory '{out_dir}': {e}")

    em = safe(lambda: design.exportManager)
    if em is None:
        return error("This design exposes no exportManager - cannot export.")
    factory = safe(lambda: getattr(em, factory_name))
    if factory is None:
        return error(f"This build's ExportManager has no {factory_name} - {fmt.upper()} export "
    "is unavailable here.")

    # The pre-write state of the target, so the landed check below proves THIS export produced the
    # file rather than finding an earlier one still sitting there.
    before = _export.snapshot(path)

    # All three mesh factories take (geometry, filename). Mutation (execute) is NOT wrapped in safe.
    try:
        opts = factory(geom, path)
    except Exception as e:
        return error(f"Could not create {fmt.upper()} export options: {e}")
    applied_refinement = _apply_refinement(opts, ref)
    applied_units, units_verified = (_apply_stl_units(opts, unit_key) if fmt == "stl"
                                     else _export.NOT_APPLIED)
    try:
        did = em.execute(opts)
    except Exception as e:
        return error(f"{fmt.upper()} export failed: {e}")
    if not did:
        return error(f"{fmt.upper()} export returned false - nothing was written.")

    # execute() returning truthy is not proof a file was written (a MeshBody target returns True and
    # writes nothing), and a stale file from an earlier export can sit at the path - so success is
    # this before/after comparison, never the execute() return.
    size, verr = _export.verify_written(path, before)
    if verr:
        if redirected_from_mesh:
            return error(
                f"{fmt.upper()} export wrote no file for this MESH target: the redirect to its "
                f"owning component ({desc}) produced no file either. Convert the mesh with "
                f"mesh_to_brep and export the resulting solid, or place it in a component that "
                f"exports.")
        return error(
            f"{fmt.upper()} export reported success but {verr}. execute() returned True but produced "
            f"nothing - treating this as a FAILURE, not a false success. Check the target geometry "
            f"and the output path are valid.")

    # A component export writes the bodies of the occurrences below it too, BRep tessellated
    # (measure_api mesh-export-component-descends-into-children).
    head = ("MESH target redirected to its owning component: the file holds EVERY body of that "
            "component and of the occurrences below it, BRep tessellated. 'component_census' "
            "counts its own." if redirected_from_mesh else
            "Exported a MESH file to local disk - the design was not modified.")
    clauses = []
    if applied_refinement is None:
        clauses.append(_refinement_not_landed(ref, "this file"))
    if fmt == "stl":
        if applied_units is None:
            clauses.append(_units_not_landed(unit_key, "this file"))
        elif units_verified:
            clauses.append(_wrote_in_units(applied_units))
        else:
            clauses.append(_units_unverified(applied_units, "this file"))
    note = " ".join([head] + clauses)
    payload = {
        "exported": True,
        "format": fmt,
        "target": desc,
        "redirected_from_mesh": redirected_from_mesh,
        "refinement": applied_refinement,          # what LANDED; null when the set did not take
        "refinement_requested": ref,
        "file_path": path,
        "file_exists": True,
        "size_bytes": size,
        "note": note,
    }
    if redirected_from_mesh:
        payload["component_census"] = _component_census(geom)
    if fmt == "stl":
        # options_applied is what the options object read BACK, null when it did not; the request
        # travels under its own key. options_verified is false when the read-back could not bite -
        # the object already read the requested unit before it was set.
        payload["options_applied"] = {"stl_units": applied_units}
        payload["options_requested"] = {"stl_units": unit_key}
        payload["options_verified"] = {"stl_units": units_verified}
    return ok(payload)


# ── tool registration ────────────────────────────────────────────────────────────────────────

TOOL_DESCRIPTION = (
    "Export geometry to a MESH file on local disk; design_export writes neutral BRep formats."
)

_EXPORT_SPEC = [_EXPORT_FORMAT, _EXPORT_REFINE, _EXPORT_UNITS, _EXPORT_TARGET]
tool = (
    _inputs.apply_to_tool(
        Tool.create_simple(name="mesh_export", description=TOOL_DESCRIPTION),
        _EXPORT_SPEC)
    .add_input_property("file_path", {"type": "string",
            "description": "Output path; a DIRECTORY when split_by_component."})
    .add_required_input("file_path")
    .add_input_property("split_by_component", {"type": "boolean",
            "description": "One file per top-level occurrence."})
    .strict_schema()
)
# DeliverablesExist re-stats every claimed deliverable (single file_path or split-mode files[]) - a
# redundant gate; the handler's factored _write_mesh_file verification stays (it builds the payload).
item = Item.create_tool_item(tool=tool, write="write", handler=handler,
                             run_on_main_thread=True,
                             postconditions=[_assert.DeliverablesExist()])


def register_tool():
    register(item)
