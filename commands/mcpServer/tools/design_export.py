# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Exports a body/component/occurrence, or the whole design (target omitted), to a neutral CAD file
(STEP/IGES/SAT/SMT/USD/Fusion-Archive/STL/3MF/OBJ) on local disk. format=dxf is a separate 2D shape:
a sketch (dxf_sketch) or a planar face's projected outline (dxf_face) via
ExportManager.createDXFSketchExportOptions. Pair with data_upload_file to round-trip the file back
into the cloud. WRITES a file to disk (does not modify the design).
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
from . import _sketch_detail

app = adsk.core.Application.get()

# format -> (file extension, ExportManager factory name, geom_first: factory(geometry, path) rather
# than factory(path, geometry)). USD's ext must be .usdz - Fusion writes .usdz and appends that
# extension itself, so any other ext leaves the landed file's name mismatching the verified path.
_FORMATS = {
    "step": (".step", "createSTEPExportOptions", False),
    "iges": (".igs", "createIGESExportOptions", False),
    "sat": (".sat", "createSATExportOptions", False),
    "smt": (".smt", "createSMTExportOptions", False),
    "usd": (".usdz", "createUSDExportOptions", False),
    "f3d": (".f3d", "createFusionArchiveExportOptions", False),
    "stl": (".stl", "createSTLExportOptions", True),
    "3mf": (".3mf", "createC3MFExportOptions", True),
    "obj": (".obj", "createOBJExportOptions", True),
    "dxf": (".dxf", None, False),
}

_FORMAT = _inputs.Choice("format", options=list(_FORMATS), default="step")

# STL-only: bake units into the file (unitType) and pick binary vs ASCII (isBinaryFormat). stl_units
# is ALWAYS assigned - an untouched unitType takes the unit of the LAST EXPLICIT assignment made
# anywhere in the session, across documents. stl_binary omitted leaves the factory value alone.
_STL_UNITS = _inputs.Choice("stl_units", options=list(_export.STL_UNIT_MEMBERS),
    description="Baked into the file; always assigned.")

# There is deliberately NO dxf_units input: the FIRST read of DXFSketchExportOptions.units kills the
# call UNCATCHABLY - no try/except runs and the transaction rolls back with "3 : Distance unit is
# not supported by DXF" - so the DXF is written in the design's default length unit.

# format=dxf inputs: a whole SKETCH by name, or a planar FACE's projected outline (find_geometry
# handle). Exactly one of these is required when format=dxf; both are ignored otherwise.
_DXF_FACE = _inputs.GeometryHandle("dxf_face", require="planar_face", required=False)

# MEASURED against an occurrence, a body proxy and their component: these six factories take a
# COMPONENT (or the root) and RAISE "3 : invlid argument geometry" on the other two, while stl, obj
# and 3mf wrote a file for all three. Sketches beside the body are not the cause.
_COMPONENT_ONLY_FORMATS = ("step", "iges", "sat", "smt", "usd", "f3d")

# The formats measured to take an occurrence or a body, named as the remedy's fallback.
_ANY_TARGET_FORMATS = "stl/obj/3mf"


def _component_only_refusal(fmt, target, desc):
    """The refusal for a BRep-neutral export aimed at anything but a component, or None."""
    if fmt not in _COMPONENT_ONLY_FORMATS or _is_component_target(desc):
        return None
    return (f"format={fmt} cannot export {desc}: its ExportManager factory takes a COMPONENT and "
            f"answers an occurrence or a body with '3 : invlid argument geometry'. Export the "
            f"COMPONENT by its name (not '{target}', an instance/body reference), omit 'target' for "
            f"the whole design, or use format={_ANY_TARGET_FORMATS}, which do take this target.")


def _is_component_target(desc):
    """Whether _resolve_target's description says it resolved a COMPONENT (or the whole design)."""
    return (desc or "").startswith(("component ", "whole design"))


def _resolve_target(design, target):
    """Resolve 'target' -> (geometry, description, error): empty is the whole design, then a handle,
    then a component, an occurrence, or a body by name. Component stays FIRST among the name lookups
    so a component's own name is not captured by its instances' match. All-None is a plain miss."""
    root = design.rootComponent
    name = (target or "").strip()
    if not name:
        return root, "whole design (root component)", None

    # Handle / entity token -> a specific body (bodies are auto-named, so a handle is precise). Try the
    # sanctioned resolver (composite-handle aware + self-healing) FIRST; a plain name returns None here
    # and falls through to the name lookups below - so we never guess handle-vs-name by string length.
    ent = _inputs._resolve_token_entity(design, name)
    if ent is not None:
        if isinstance(ent, adsk.fusion.BRepBody):
            return ent, f"body (handle {name[:10]}...)", None
        return None, None, None

    comp, comp_err = _export.find_component(design, name)
    if comp_err:
        return None, None, comp_err + (
            " Export one instance by its occurrence name/fullPathName, or pass a body handle from "
            "find_geometry (design_get(include=['tree']) lists the instances).")
    if comp:
        return comp, f"component '{name}'", None

    # Only a PLAIN miss falls through to the body vocabulary, and the OCCURRENCE_MISS stem is what
    # marks one - the other refusal texts carry the candidate lists.
    occ, occ_err = _inputs._resolve_occurrence("target", name)
    if occ is not None:
        return occ, f"occurrence '{safe(lambda: occ.name) or name}'", None
    if occ_err and _inputs.OCCURRENCE_MISS not in occ_err:
        return None, None, occ_err

    # Body by name (root + any occurrence) - likewise ambiguity-refusing, with the same
    # stem-not-substring discrimination (BODY_MISS marks the one plain-miss text; every other
    # refusal - ambiguity, an empty scope, a multi-body component - passes through intact).
    body, body_err = _inputs._resolve_any_body("target", name)
    if body is not None:
        return body, f"body '{safe(lambda: body.name) or name}'", None
    if body_err and _inputs.BODY_MISS not in body_err:
        return None, None, body_err

    return None, None, None


def _option_spec(fmt, incl_bodies, incl_comps, stl_binary, stl_unit_key):
    """[(knob name, options property, value to ASSIGN, value to REPORT)] for every option THIS call
    writes - the one table both the read-back pass and the requested-values payload read. A build
    carrying no enum member for a knob yields a None assign value, recorded as a refusal."""
    spec = []
    if incl_bodies:
        spec.append(("invisible_bodies", "isIncludingInvisibleBodies", True, True))
    if incl_comps:
        spec.append(("invisible_components", "isIncludingInvisibleComponents", True, True))
    if fmt == "stl":
        if stl_binary is not None:
            spec.append(("stl_binary", "isBinaryFormat", bool(stl_binary), bool(stl_binary)))
        # ALWAYS, never "only when asked for": the unit is the one knob whose omitted case is not a
        # neutral default but an inherited one (see _STL_UNITS), so every STL this tool writes names
        # its unit, and every STL payload carries it.
        spec.append(("stl_units", "unitType", _export.stl_unit_enum(stl_unit_key), stl_unit_key))
    return spec


def _requested_options(fmt, incl_bodies, incl_comps, stl_binary, stl_unit_key):
    """{knob name: the value REQUESTED for it} over the same spec - what was asked for, never what
    landed. Empty when the call asked for no option at all."""
    return {name: report for name, _prop, _want, report in
            _option_spec(fmt, incl_bodies, incl_comps, stl_binary, stl_unit_key)}


# Knobs whose READ VALUE determines the written file: for these _export.applied_pair's 'changed'
# half is dropped and no verification travels beside them. isBinaryFormat qualifies - False writes a
# distinct, larger ASCII file - while unitType's read value does not, so it keeps its verification.
_READ_DETERMINES_FILE = frozenset({"stl_binary"})


def _configure_export_options(fmt, opts, incl_bodies, incl_comps, stl_binary, stl_unit_key):
    """Write each option knob through _export.applied_pair, never failing the export over one:
    (applied = the value each knob READ BACK, refused = the knobs that did not land, verified = per
    landed knob, whether that read-back could have failed - omitted for _READ_DETERMINES_FILE)."""
    applied, refused, verified = {}, [], {}
    for name, prop, want, report in _option_spec(fmt, incl_bodies, incl_comps,
                                                 stl_binary, stl_unit_key):
        if want is None:
            refused.append(name)          # this build carries no enum member to assign
            continue
        landed, changed = _export.applied_pair(opts, prop, want, report)
        if landed is None:
            refused.append(name)
            continue
        applied[name] = landed
        if name not in _READ_DETERMINES_FILE:
            verified[name] = changed
    return applied, refused, verified


def _export_one(em, factory_name, geom_first, geom, path, configure=None):
    """Write one geometry to one path: (execute_bool, raise_error_or_None, (applied, refused,
    verified)). 'configure', if given, receives the created options object BEFORE execute() and
    returns the knob triple - see _configure_export_options; it never blocks the export. A FALSE
    execute() is not a verdict here - the caller's _landed reads the disk."""
    factory = getattr(em, factory_name)
    knobs = ({}, [], {})
    try:
        # STL/OBJ/3MF's API signature is (geometry, filename); the others are (filename, geometry).
        opts = factory(geom, path) if geom_first else factory(path, geom)
        if configure:
            knobs = configure(opts) or ({}, [], {})
        did = em.execute(opts)
    except Exception as e:
        return False, str(e), ({}, [], {})
    return bool(did), None, knobs


def _landed(fmt, path, before, executed):
    """(size_bytes, error_or_None) for a finished export - the file on disk is the verdict, and
    each refusal names the bool execute() returned."""
    # execute() returning FALSE while a valid archive lands at the path is a MEASURED shape
    # (measure_api row fusion-archive-execute-bool-vs-landed-file), so the bool decides nothing.
    size, verr = _export.verify_written(path, before)
    if not verr:
        return size, None
    if executed:
        return None, (f"{fmt.upper()} export reported success but {verr}. execute() returned true "
                      "but produced nothing - treating this as a failure, not a false success. "
                      "Check the target geometry and the output path are valid.")
    return None, f"{fmt.upper()} export failed: execute() returned false and {verr}."


def _write_dxf(design, sk, path, want_construction, want_points, want_projected):
    """Write sketch 'sk' to DXF through createDXFSketchExportOptions(filename, sketch), whose three
    content flags each default to True: (size_bytes or None, error or None)."""
    em = safe(lambda: design.exportManager)
    if em is None:
        return None, "This design exposes no exportManager - cannot export."
    factory = safe(lambda: em.createDXFSketchExportOptions)
    if factory is None:
        return None, ("This build's ExportManager has no createDXFSketchExportOptions - DXF export "
                      "is unavailable here.")
    before = _export.snapshot(path)   # so the landed check proves THIS write, not an earlier file
    try:
        opts = factory(path, sk)
    except Exception as e:
        return None, f"Could not create DXF export options: {e}"
    safe(lambda: setattr(opts, "isConstructionExported",
                         True if want_construction is None else bool(want_construction)))
    safe(lambda: setattr(opts, "isPointsExported",
                         True if want_points is None else bool(want_points)))
    safe(lambda: setattr(opts, "isProjectedGeometryExported",
                         True if want_projected is None else bool(want_projected)))
    try:
        did = em.execute(opts)
    except Exception as e:
        return None, f"DXF export failed: {e}"
    if not did:
        return None, "DXF export returned false - nothing was written."
    size, verr = _export.verify_written(path, before)
    if verr:
        return None, f"DXF export reported success but {verr}."
    return size, None


def _export_dxf(dxf_sketch, dxf_face, file_path,
                want_construction, want_points, want_projected, dxf_component=""):
    """format=dxf: write a whole SKETCH, or a planar FACE's outline projected into a scratch sketch
    that is removed again afterward (the design is left unchanged either way). Exactly one of
    dxf_sketch/dxf_face must be given.
    """
    path = (file_path or "").strip().strip('"')
    if not path:
        return error("Provide 'file_path' - the local .dxf output path.")
    if not path.lower().endswith(".dxf"):
        path = path + ".dxf"

    design = _common.design()
    if not design:
        return error("No active design to export. Open or create a document first (see doc_new).")

    sketch_name = (dxf_sketch or "").strip()
    has_face = bool((dxf_face or "").strip()) if isinstance(dxf_face, str) else bool(dxf_face)
    if sketch_name and has_face:
        return error("Pass only one of 'dxf_sketch' or 'dxf_face' for format=dxf, not both.")
    if not sketch_name and not has_face:
        return error("format=dxf needs either 'dxf_sketch' (a sketch NAME) or 'dxf_face' (a "
                     "find_geometry planar-face handle) to know what 2D geometry to write.")

    out_dir = os.path.dirname(path)
    if out_dir and not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            return error(f"Could not create output directory '{out_dir}': {e}")

    if sketch_name:
        return _export_dxf_sketch(design, sketch_name, path,
                                  want_construction, want_points, want_projected, dxf_component)
    return _export_dxf_face(design, dxf_face, path,
                            want_construction, want_points, want_projected)


def _export_dxf_sketch(design, sketch_name, path,
                       want_construction, want_points, want_projected, dxf_component=""):
    # 'dxf_component' narrows the design-wide walk to one component's own sketches: Fusion numbers
    # sketches per component from 1, so a name two components carry is refused, and the refusal
    # names this input rather than a rename the caller may not be able to make.
    sk, refusal = _sketch_detail.scoped_sketch(design, sketch_name, dxf_component, "dxf_component")
    if refusal:
        return error(refusal)
    if not sk:
        names = _common.all_sketch_names(design)
        return error(f"No sketch named '{sketch_name}'. Available: "
                     + (", ".join(n for n in names if n) or "(none)")
                     + ". Create one with sketch_create, or pass 'dxf_face' instead.")
    has_geom = (safe(lambda: sk.sketchCurves.sketchLines.count, 0)
                or safe(lambda: sk.sketchCurves.sketchArcs.count, 0)
                or safe(lambda: sk.sketchCurves.sketchCircles.count, 0)
                or safe(lambda: sk.sketchPoints.count, 0))
    if not has_geom:
        return error(f"Sketch '{sketch_name}' is empty - nothing to write to DXF.")

    size, werr = _write_dxf(design, sk, path, want_construction, want_points, want_projected)
    if werr:
        return error(werr)
    return ok({
        "exported": True,
        "format": "dxf",
        "source": f"sketch '{sketch_name}'",
        "file_path": path,
        "file_exists": True,
        "size_bytes": size,
        "note": "Sketch written to DXF - the standard laser/waterjet/sheet-metal handoff format.",
    })


def _sketch_curve_count(sk):
    """How many curves a scratch projection sketch holds - the face path's whole content."""
    return (safe(lambda: sk.sketchCurves.sketchLines.count, 0)
            or safe(lambda: sk.sketchCurves.sketchArcs.count, 0)
            or safe(lambda: sk.sketchCurves.sketchCircles.count, 0))


def _export_dxf_face(design, dxf_face, path, want_construction, want_points, want_projected):
    # MEASURED: with this flag false the written DXF's ENTITIES section is EMPTY, because a scratch
    # sketch on a face holds the projected outline and nothing else. The file still lands ~2 KB of
    # header, so the export would report exported:true over a drawing with no curve in it.
    if want_projected is not None and not want_projected:
        return error("'dxf_export_projected' (false) writes an ENTITIES section with no curve in "
                     "it for 'dxf_face': the scratch sketch this path projects the outline into "
                     "holds nothing else - refusing rather than writing an empty DXF. Omit it, or "
                     "pass 'dxf_sketch' to filter a sketch that has geometry of its own.")

    face, ferr = _DXF_FACE.resolve(dxf_face)
    if ferr:
        return error(ferr)

    # A find_geometry handle at a sub-component's face resolves to a PROXY (measured: assemblyContext
    # reads the occurrence), and the scratch sketch is built in the component that OWNS the face, so
    # the native face is what both the sketch's plane and the projection are taken from.
    native = _common._native_of(face)
    comp = safe(lambda: native.body.parentComponent) or safe(lambda: design.rootComponent)
    if comp is None:
        return error("Could not resolve a component to build the projection sketch in.")
    try:
        sk = comp.sketches.add(native)
    except Exception as e:
        return error(f"Could not create a projection sketch on the face: {e}")
    if not sk:
        return error("Could not create a projection sketch on the face (sketches.add returned nothing).")
    sk_name = safe(lambda: sk.name) or "scratch sketch"

    def _cleanup():
        return bool(safe(lambda: sk.deleteMe(), False))

    # MEASURED on a root-owned body and on a sub-component's: project2 of the FACE into a sketch that
    # lies on that same face raises '2 : InternalValidationError : res', while its EDGES project. A
    # sketch Fusion already filled is left alone - projecting again doubles every outline curve.
    if not _sketch_curve_count(sk):
        try:
            sk.project2(list(_common.iter_collection(safe(lambda: native.edges))), False)
        except Exception as e:
            cleaned = _cleanup()
            msg = f"Could not project the face's edges into a sketch for DXF: {e}"
            if not cleaned:
                msg += f" Also failed to remove the scratch sketch '{sk_name}' - delete it manually."
            return error(msg)

    if not _sketch_curve_count(sk):
        cleaned = _cleanup()
        msg = "Face projection produced no sketch geometry - nothing to write to DXF."
        if not cleaned:
            msg += f" Also failed to remove the scratch sketch '{sk_name}' - delete it manually."
        return error(msg)

    size, werr = _write_dxf(design, sk, path, want_construction, want_points, want_projected)
    cleaned = _cleanup()
    if werr:
        msg = werr
        if not cleaned:
            msg += f" Also failed to remove the scratch sketch '{sk_name}' - delete it manually."
        return error(msg)

    note = ("Face outline projected into a scratch sketch, written to DXF, and the scratch sketch "
            "removed - the design is unchanged.")
    if not cleaned:
        note = (f"DXF written, but the scratch projection sketch '{sk_name}' could not be removed - "
                "it remains in the design; delete it manually.")

    return ok({
        "exported": True,
        "format": "dxf",
        "source": "face profile (projected)",
        "file_path": path,
        "file_exists": True,
        "size_bytes": size,
        "note": note,
    })


def handler(format: str = "step", file_path: str = "", target: str = "",
            split_by_component: bool = False, dxf_sketch: str = "", dxf_face: str = "",
            include_invisible_bodies: bool = False, include_invisible_components: bool = False,
            stl_binary=None, stl_units: str = "",
            dxf_export_construction=None, dxf_export_points=None,
            dxf_export_projected=None, dxf_component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    fmt, ferr = _FORMAT.resolve(format)
    if ferr:
        return error(ferr)

    # 'mm' when omitted, which is NOT a schema default: an stl_units on another format is refused
    # below, so sending 'mm' is not the same call as omitting it.
    stl_unit_key, sue = _STL_UNITS.resolve(stl_units or "mm")
    if sue:
        return error(sue)
    # Both STL-only knobs are refused on another format rather than dropped, ahead of the dxf
    # dispatch so the 2D branch answers as the neutral-CAD formats do.
    if (stl_units or "").strip() and fmt != "stl":
        return error(f"'stl_units' ('{stl_unit_key}') applies to format=stl only, and this call "
                     f"asked for format={fmt} - refusing rather than dropping it. Export as stl to "
                     "bake the unit into the file, or omit 'stl_units'.")
    # Keyed on `is not None`, never truthiness: stl_binary=False IS a request (ASCII).
    if stl_binary is not None and fmt != "stl":
        return error(f"'stl_binary' ({'true' if stl_binary else 'false'}) applies to format=stl "
                     f"only, and this call asked for format={fmt} - refusing rather than dropping "
                     "it. Export as stl to choose binary or ASCII, or omit 'stl_binary'.")

    # The dxf branch writes the ONE sketch/face named by dxf_sketch/dxf_face, and the split walk picks
    # its own top-level occurrences - so every knob that cannot reach either write is refused here.
    if fmt == "dxf":
        if (target or "").strip():
            return error(f"'target' ('{target.strip()}') does not apply to format=dxf, which writes "
                         "the 2D geometry named by 'dxf_sketch' or 'dxf_face' - refusing rather than "
                         "dropping it. Name the sketch or face instead, or omit 'target'.")
        if split_by_component:
            return error("'split_by_component' (true) does not apply to format=dxf, which writes one "
                         "sketch or face to one file - refusing rather than dropping it. Call "
                         "design_export once per sketch/face, or omit 'split_by_component'.")
        for knob in ("include_invisible_bodies" if include_invisible_bodies else "",
                     "include_invisible_components" if include_invisible_components else ""):
            if knob:
                return error(f"'{knob}' (true) applies to the 3D formats only, and this call asked "
                             "for format=dxf, whose source is one named sketch or face - refusing "
                             f"rather than dropping it. Export as a 3D format, or omit '{knob}'.")
        return _export_dxf(dxf_sketch, dxf_face, file_path,
                           dxf_export_construction, dxf_export_points, dxf_export_projected,
                           dxf_component)

    if split_by_component and (target or "").strip():
        return error(f"'target' ('{target.strip()}') and split_by_component=true cannot be combined: "
                     "the split writes one file per TOP-LEVEL occurrence and would not narrow to "
                     "that target - refusing rather than dropping it. Omit 'target' to split the "
                     "whole design, or omit 'split_by_component' to export just that target.")

    ext, factory_name, geom_first = _FORMATS[fmt]

    path = (file_path or "").strip().strip('"')
    if not path:
        return error("Provide 'file_path' - the local output path (a file, or a DIRECTORY when "
    "split_by_component=true). The format extension is appended if missing.")

    design = _common.design()
    if not design:
        return error("No active design to export. Open or create a document first (see doc_new).")

    em = design.exportManager

    def configure(opts):
        return _configure_export_options(fmt, opts, include_invisible_bodies,
                                         include_invisible_components, stl_binary, stl_unit_key)

    # ---- per-component split: one file per top-level occurrence into directory 'path' ----
    if split_by_component:
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

        # Each file gets its OWN options object and so its own knob read-back, collected here by
        # path and folded into split_by_occurrence's per-file records below.
        applied_by_path = {}
        verified_by_path = {}
        false_execute_paths = set()

        def _write_one(occ, fpath):
            # The BRep-neutral factories RAISE on an Occurrence (measured), so for those formats
            # each file is written from the occurrence's own COMPONENT; the mesh formats take the
            # occurrence itself.
            geom = occ
            if fmt in _COMPONENT_ONLY_FORMATS:
                geom = safe(lambda: occ.component)
                if geom is None:
                    occ_name = safe(lambda: occ.fullPathName) or safe(lambda: occ.name) or "?"
                    return None, (f"the component behind '{occ_name}' did not read, and "
                                  f"format={fmt} does not export an occurrence.")
            before = _export.snapshot(fpath)     # the baseline this file's landed check is proven on
            executed, eerr, knobs = _export_one(em, factory_name, geom_first, geom, fpath, configure)
            if eerr:
                return None, f"{fmt.upper()} export failed: {eerr}"
            # VERIFY the file is actually on disk, non-empty, and written by THIS call - execute()'s
            # bool is NOT proof either way, and neither is a stale file at the same path.
            size, verr = _landed(fmt, fpath, before, executed)
            if verr:
                return None, verr
            applied_by_path[fpath] = knobs[0]
            verified_by_path[fpath] = knobs[2]
            if not executed:
                false_execute_paths.add(fpath)
            return size, None

        files, errors = _export.split_by_occurrence(occs, out_dir, ext, _write_one)
        if not files:
            # ZERO deliverables is a FAILED export, not a success carrying exported:false - nothing
            # landed on disk, so the per-occurrence reasons travel in the error text instead.
            return error(f"{fmt.upper()} split export wrote NO files - all "
                         f"{len(errors)} occurrence(s) failed: "
                         + _export.failure_detail(errors))
        for rec in files:
            if rec.get("file_path") in false_execute_paths:
                rec["execute_returned_false"] = True
        requested = _requested_options(fmt, include_invisible_bodies,
                                       include_invisible_components, stl_binary, stl_unit_key)
        if requested:
            for rec in files:
                # What LANDED for THIS file: the value its own options object read back, and null for
                # a requested knob that did not read back - never the request echoed.
                landed = applied_by_path.get(rec.get("file_path")) or {}
                rec["options_applied"] = {name: landed.get(name) for name in requested}
                # ...and whether THIS file's read-back could have failed, for the knobs that
                # publish it. False where the options object already read the requested value:
                # that equality proves nothing. Absent for a knob whose read determines the file.
                backed = verified_by_path.get(rec.get("file_path")) or {}
                landed_evidence = {name: bool(v) for name, v in backed.items()
                                   if rec["options_applied"].get(name) is not None}
                if landed_evidence:
                    rec["options_verified"] = landed_evidence
        unlanded = [rec for rec in files if any(v is None for v in
                                                rec.get("options_applied", {}).values())]
        unverified = [rec for rec in files if any(
            v is False for v in (rec.get("options_verified") or {}).values())]
        out = {
            "exported": True,
            "format": fmt,
            "split_by_component": True,
            "directory": out_dir,
            "file_count": len(files),
            "files": files,
            "note": f"Exported {len(files)} component(s) to separate {fmt.upper()} files. Each "
            "top-level occurrence is one file - ready to print/assemble individually."
            + (f" format={fmt} takes no occurrence, so each file was written from that "
               "occurrence's COMPONENT." if fmt in _COMPONENT_ONLY_FORMATS else ""),
        }
        if requested:
            out["options_requested"] = requested
        if errors:
            # PARTIAL success: the shortfall is its own flag plus the per-occurrence reasons, so a
            # caller reading file_count alone cannot miss the occurrences that produced no file.
            out["partial"] = True
            out["failed"] = errors
            out["note"] = (f"PARTIAL: {len(files)} of {len(files) + len(errors)} top-level "
                           f"occurrence(s) exported to separate {fmt.upper()} files; "
                           f"{len(errors)} produced NO file - see 'failed'.")
        if false_execute_paths:
            out["note"] += (f" ExportManager.execute() returned FALSE for {len(false_execute_paths)}"
                            f" of the {len(files)} file(s), which landed non-empty anyway - each "
                            "such record carries 'execute_returned_false'.")
        if unlanded:
            # ONE fact was observed per null: that file's export options did not read back the value
            # set on them. WHY, and what the writer then used instead, is not readable from here, so
            # the sentence names neither - it points at the per-file key and the request.
            names = sorted({name for rec in unlanded
                            for name, v in rec["options_applied"].items() if v is None})
            out["note"] += (f" {', '.join(names)} did NOT land for {len(unlanded)} of the "
                            f"{len(files)} exported file(s): those files' export options did not "
                            "read back the value that was set, so each such file's "
                            "'options_applied' carries null for it. 'options_requested' is what was "
                            "asked for.")
        if unverified:
            # TWO facts were observed per false: the file's options object reads the value under
            # 'options_applied', and it READ IT BEFORE the assignment. What the writer then did is
            # not readable from here, so this claims nothing about the files.
            names = sorted({name for rec in unverified
                            for name, v in rec["options_verified"].items() if v is False})
            out["note"] += (f" {', '.join(names)} is set but UNVERIFIED for {len(unverified)} of "
                            f"the {len(files)} exported file(s): those files' export options "
                            "already read the requested value BEFORE it was set, so reading it "
                            "back after cannot tell an assignment that took from one that was "
                            "dropped - each such file's 'options_verified' carries false for it.")
        return ok(out)

    # ---- single-target export ----
    if not path.lower().endswith(ext):
        path = path + ext

    geom, desc, terr = _resolve_target(design, target)
    if geom is None:
        return error(terr or (f"Export target '{target}' not found. Pass a body/component NAME, an "
    "occurrence fullPathName (e.g. Bracket:2 - the precise way to pick one instance), or omit "
    "'target' to export the whole design."))
    # Refused BEFORE the directory is made and the file is touched: the factory's own raise names
    # neither the target nor a route that works.
    kind_err = _component_only_refusal(fmt, target, desc)
    if kind_err:
        return error(kind_err)

    # make sure the destination directory exists
    out_dir = os.path.dirname(path)
    if out_dir and not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            return error(f"Could not create output directory '{out_dir}': {e}")

    before = _export.snapshot(path)   # the pre-write state the landed check is proven against
    executed, eerr, applied_opts = _export_one(em, factory_name, geom_first, geom, path, configure)
    if eerr:
        return error(f"{fmt.upper()} export failed: {eerr}")

    # VERIFY the file is actually on disk, non-empty, and written by THIS call - execute()'s bool is
    # NOT proof either way, and a stale file from an earlier export at the same path is not this
    # export's deliverable. That comparison is the SOURCE OF TRUTH for success.
    size, verr = _landed(fmt, path, before, executed)
    if verr:
        return error(verr)

    out = {
        "exported": True,
        "format": fmt,
        "target": desc,
    "file_path": path,
    "file_exists": True,
    "size_bytes": size,
    "note": ("Exported to local disk. To round-trip into the cloud, upload it with "
            "data_upload_file (STEP/IGES are translated to a Fusion design on the cloud)."),
    }
    if not executed:
        out["execute_returned_false"] = True
        out["note"] += (" ExportManager.execute() returned FALSE for this call and the file at "
                        "'file_path' landed non-empty anyway - 'execute_returned_false' carries "
                        "that, and the file on disk is what this result stands on.")
    applied_opts, refused_opts, verified_opts = applied_opts
    # What was ASKED FOR, one entry per knob this call writes - the key the split path above and the
    # sibling mesh_export both publish, and the only place a REFUSED knob's attempted value is
    # readable: 'options_refused' names the knob, never the value it was asked with.
    requested_opts = _requested_options(fmt, include_invisible_bodies,
                                        include_invisible_components, stl_binary, stl_unit_key)
    if requested_opts:
        out["options_requested"] = requested_opts
    if applied_opts:
        # The VALUE each knob actually holds, read back off the options object - never a
        # did-it-stick flag under the knob's own name.
        out["options_applied"] = applied_opts
        # Whether each of those values is BACKED: true only where the read-back could have failed
        # (the options object was not already reading the requested value). Absent for the knobs in
        # _READ_DETERMINES_FILE.
        if verified_opts:
            out["options_verified"] = verified_opts
        unverified_opts = sorted(n for n, backed in verified_opts.items() if not backed)
        if unverified_opts:
            out["note"] += (" " + ", ".join(unverified_opts) + " is set but UNVERIFIED: the export "
                            "options already read the requested value BEFORE it was set, so "
                            "reading it back after cannot tell an assignment that took from one "
                            "that was dropped - 'options_verified' carries false for it.")
    if refused_opts:
        out["options_refused"] = refused_opts
        out["note"] += (" The export landed, but Fusion did not take these options: "
                        + ", ".join(refused_opts)
                        + ". 'options_requested' carries the value each was asked with.")
    return ok(out)


TOOL_DESCRIPTION = (
    "Export a body, component/occurrence or the whole design (omit 'target') to a CAD file on "
    "local disk."
)

tool = (
    Tool.create_simple(name="design_export", description=TOOL_DESCRIPTION)
    .add_input_property(_FORMAT.name, _FORMAT.schema())
    .add_input_property("file_path", {"type": "string",
            "description": "Output path; a DIRECTORY when split_by_component."})
    .add_input_property("target", {"type": "string",
            "description": "A find_geometry handle or a body/component/occurrence name; a shared "
                           "name exports the COMPONENT, a fullPathName (Bracket:2) one instance."})
    .add_input_property("split_by_component", {"type": "boolean",
            "description": "One file per top-level occurrence."})
    .add_input_property("include_invisible_bodies", {"type": "boolean"})
    .add_input_property("include_invisible_components", {"type": "boolean"})
    .add_input_property("stl_binary", {"type": "boolean"})
    .add_input_property(_STL_UNITS.name, _STL_UNITS.schema())
    .add_input_property("dxf_sketch", {"type": "string"})
    .add_input_property("dxf_component", {"type": "string",
            "description": "Narrows 'dxf_sketch' to one component."})
    .add_input_property(_DXF_FACE.name, _DXF_FACE.schema())
    .add_input_property("dxf_export_construction", {"type": "boolean",
            "description": "Default true."})
    .add_input_property("dxf_export_points", {"type": "boolean",
            "description": "Default true."})
    .add_input_property("dxf_export_projected", {"type": "boolean",
            "description": "Default true."})
    .strict_schema()
)

# DeliverablesExist re-stats every claimed deliverable (single file_path or split-mode files[]) - a
# redundant gate over the handler's per-path inline verification, which stays (it builds the payload).
item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_assert.DeliverablesExist()])


def register_tool():
    register(item)
