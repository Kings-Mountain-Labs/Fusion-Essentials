# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: import a CAD file from LOCAL DISK through adsk.core.ImportManager - into a
component of the open design, into an existing sketch, or into a new document.
ImportManager.importToNewDocument does not accept DXF2DImportOptions or SVGImportOptions, so those
two formats import only into an open design.
"""

import os

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _inputs
from . import _sketch_detail
from . import _view_common
from . import _assert

app = adsk.core.Application.get()

_EXT_TO_FORMAT = {
    ".step": "step", ".stp": "step",
    ".iges": "iges", ".igs": "iges",
    ".sat": "sat",
    ".smt": "smt",
    ".f3d": "f3d",
    ".dxf": "dxf",
    ".svg": "svg",
}

# createFusionArchiveImportOptions accepts f3d only; an f3z archive has no ImportManager path.
_REFUSED_EXT = {
    ".f3z": ("'.f3z' cannot be imported - createFusionArchiveImportOptions takes .f3d only. Upload "
             "the archive with data_upload_file, then reference it with doc_insert_occurrence."),
}

_OPTIONS_FACTORY = {
    "step": "createSTEPImportOptions",
    "iges": "createIGESImportOptions",
    "sat": "createSATImportOptions",
    "smt": "createSMTImportOptions",
    "f3d": "createFusionArchiveImportOptions",
}

_SKETCH_FORMATS = ("dxf", "svg")

_FORMAT = _inputs.Choice("format", ["step", "iges", "sat", "smt", "f3d", "dxf", "svg"])
_INTO_COMPONENT = _inputs.OccurrenceRef("into_component")
_PLANE = _inputs.PlaneRef("plane", default="xy")


def _resolve_format(path, raw):
    """(format, error_text) - the file extension names the format."""
    ext = os.path.splitext(path)[1].lower()
    chosen, cerr = _FORMAT.resolve(raw)
    if cerr:
        return None, cerr
    refusal = _REFUSED_EXT.get(ext)
    if refusal:
        return None, refusal
    derived = _EXT_TO_FORMAT.get(ext)
    if derived is None:
        return None, (f"'{ext or path}' is not an importable extension. Supported: "
                      + ", ".join(sorted(_EXT_TO_FORMAT)) + ".")
    if chosen and chosen != derived:
        return None, (f"format='{chosen}' contradicts the file extension '{ext}', which is "
                      f"{derived}. Pass format='{derived}' or point file_path at a {chosen} file.")
    return derived, None


def _options(mgr, fmt, path):
    """(options, error_text) from the format's ImportManager factory. Creating the options object
    does NOT import - it only prepares the file."""
    factory_name = _OPTIONS_FACTORY[fmt]
    factory = safe(lambda: getattr(mgr, factory_name))
    if factory is None:
        return None, (f"This Fusion build has no ImportManager.{factory_name}, so {fmt.upper()} "
                      "import is unavailable.")
    options = safe(lambda: factory(path))
    if options is None:
        return None, (f"{factory_name} returned nothing for '{path}' - the file could not be "
                      f"prepared as {fmt.upper()} (corrupt or not really a {fmt.upper()} file?).")
    return options, None


def _created_objects(collection):
    """The objects an import returned. Nothing addresses them by position - they are counted and
    described by name/type - so an unreadable one simply drops out of the list."""
    return list(_common.iter_collection(collection))


def _describe(objects, cap=20):
    return [{"name": safe(lambda o=o: o.name), "type": type(o).__name__} for o in objects[:cap]]


def _run_import(mgr, options, target):
    """(objects, error_text). importToTarget2 returns the objects the import created, and returns
    null when the import failed."""
    try:
        created = mgr.importToTarget2(options, target)
    except Exception as e:
        return None, f"Import failed (importToTarget2 raised): {e}"
    if created is None:
        return None, ("importToTarget2 returned null, which the API reports for a FAILED import - "
                      "nothing was created.")
    return _created_objects(created), None


def _component_counts(comp):
    """What a component directly owns. An assembly file lands as occurrences, a single part as
    bodies, a DXF as sketches."""
    return {"bodies": safe(lambda: comp.bRepBodies.count, 0) or 0,
            "sketches": safe(lambda: comp.sketches.count, 0) or 0,
            "occurrences": safe(lambda: comp.occurrences.count, 0) or 0}


def _gained(before, after):
    return {k: after[k] - before[k] for k in before}


def _target_of(design, into_component):
    """(component, label, error_text) for the component an import lands in."""
    raw = (into_component or "").strip() if isinstance(into_component, str) else ""
    if not raw:
        comp = _common.target_component(design)
        if comp is None:
            return None, None, "The active design exposes no component to import into."
        return comp, f"component '{safe(lambda: comp.name)}'", None
    occ, oerr = _INTO_COMPONENT.resolve(raw)
    if oerr:
        return None, None, oerr
    comp = safe(lambda: occ.component)
    if comp is None:
        return None, None, f"Occurrence '{raw}' has no component to import into."
    return comp, f"component '{safe(lambda: comp.name)}'", None


def _active_workspace():
    """(id, name) of the workspace the UI reports active - (None, None) when neither reads."""
    ui = safe(lambda: app.userInterface)
    if ui is None:
        return None, None
    return safe(lambda: ui.activeWorkspace.id), safe(lambda: ui.activeWorkspace.name)


def _reactivate(ws_id):
    """True when the workspace carrying this id reads back active after the shared activate."""
    # A restore that raises or declines is False, which the disclosure below names rather than
    # sinking an import that already landed.
    ui = safe(lambda: app.userInterface)
    ws = safe(lambda: ui.workspaces.itemById(ws_id)) if ui is not None else None
    if ws is None:
        return False
    try:
        return _view_common.activate_workspace(ui, ws)[0] is True
    except Exception:
        return False


def _workspace_disclosure(before):
    """(payload keys, note sentence) for what the import did to the ACTIVE workspace."""
    # MEASURED: importManager activates Design BEFORE it can fail - a malformed SAT imported with
    # Manufacture active raised, and activeWorkspace read 'Design' inside the except. So the
    # workspace read before the import is re-activated by the id that read.
    before_id, before_name = before
    if before_id is None:
        return {}, ""
    after_id, after_name = _active_workspace()
    if after_id is None or after_id == before_id:
        return {}, ""
    back = before_name or before_id
    if _reactivate(before_id):
        return {"workspace_restored": back}, ""
    left = after_name or after_id
    return ({"workspace_changed": {"from": back, "to": left}},
            f" The import left '{left}' active and '{back}' did not read back active again - "
            "switch back with view_switch_workspace.")


def _restored_workspace_note(before_ws):
    """Put the workspace back after an import that FAILED, and return the sentence to append when
    the restore itself did not read back (else ''). importToTarget2 activates Design and then
    raises, so an error path that skips this leaves a Manufacture session in Design."""
    _keys, note = _workspace_disclosure(before_ws)
    return note


def _import_solid(mgr, design, path, fmt, into_component, before_ws=(None, None)):
    comp, label, terr = _target_of(design, into_component)
    if terr:
        return error(terr)
    options, oerr = _options(mgr, fmt, path)
    if oerr:
        return error(oerr)

    before = _component_counts(comp)
    objects, ierr = _run_import(mgr, options, comp)
    if ierr:
        return error(ierr + _restored_workspace_note(before_ws))
    gained = _gained(before, _component_counts(comp))

    # MEASURED: no solid format reached this state. STEP and IGES mint an occurrence named after the
    # file even from a malformed one AND from a valid file carrying no geometry; SAT, SMT and F3D
    # raise instead. The guard stands so an unmeasured empty return is never a false ok.
    if not objects and gained["bodies"] <= 0 and gained["occurrences"] <= 0:
        return error(f"The {fmt.upper()} import reported no failure but nothing landed in {label}: "
                     "importToTarget2 returned no objects and the component gained no body and no "
                     "occurrence. Read the design back with design_get(include=['tree']) - the "
                     "geometry may have landed elsewhere." + _restored_workspace_note(before_ws))

    ws_keys, ws_note = _workspace_disclosure(before_ws)
    return ok({
        "imported": True,
        "format": fmt,
        "file": path,
        "into": label,
        "objects_created": len(objects),
        "bodies_added": gained["bodies"],
        "occurrences_added": gained["occurrences"],
        "created": _describe(objects),
        "note": ("Imported as solid/surface geometry. An assembly file lands as sub-occurrences, a "
                 "single part as bodies. Inspect it with design_get(include=['tree']) and pick "
                 "faces/edges for the model tools with find_geometry.") + ws_note,
        **ws_keys,
    })


def _import_dxf(mgr, design, path, into_component, plane, before_ws=(None, None)):
    comp, label, terr = _target_of(design, into_component)
    if terr:
        return error(terr)
    planar_entity, perr = _PLANE.resolve(plane)
    if perr:
        return error(perr)
    options = safe(lambda: mgr.createDXF2DImportOptions(path, planar_entity))
    if options is None:
        return error(f"createDXF2DImportOptions returned nothing for '{path}' - the file could not "
                     "be prepared as DXF, or the plane is not a construction plane / planar face.")

    before = _component_counts(comp)
    objects, ierr = _run_import(mgr, options, comp)
    if ierr:
        return error(ierr + _restored_workspace_note(before_ws))
    # DXF2DImportOptions.results holds the created sketches - one per DXF layer carrying 2D
    # geometry, named after that layer. 3D geometry in the file is ignored.
    landed = objects or _created_objects(safe(lambda: options.results))
    gained = _gained(before, _component_counts(comp))

    if not landed and gained["sketches"] <= 0:
        return error(f"The DXF import reported no failure but no sketch landed in {label}: "
                     "importToTarget2 returned no objects, DXF2DImportOptions.results is empty and "
                     "the component gained no sketch. A DXF holding only 3D geometry imports "
                     "nothing - a 2D import ignores it." + _restored_workspace_note(before_ws))

    ws_keys, ws_note = _workspace_disclosure(before_ws)
    return ok({
        "imported": True,
        "format": "dxf",
        "file": path,
        "into": label,
        "plane": plane or "xy",
        "sketches_added": gained["sketches"],
        "created": _describe(landed),
        "note": ("One sketch per DXF layer that carries 2D geometry, named after the layer. Read "
                 "the curves with sketch_get, then extrude a profile with model_extrude.") + ws_note,
        **ws_keys,
    })


def _import_svg(mgr, design, path, sketch, sketch_component="", before_ws=(None, None)):
    # The scope is 'sketch_component', NOT 'into_component': into_component names the component a
    # DXF's new sketches or a solid's occurrence LAND in, while an SVG lands in a sketch that
    # already exists. Two different components, so the two inputs stay two inputs.
    target, requested, ambiguous = _sketch_detail.scoped_or_recent_sketch(
        design, sketch, sketch_component, "sketch_component")
    if ambiguous:
        return error(ambiguous)
    if target is None:
        if requested:
            names = _common.all_sketch_names(design)
            return error(f"No sketch named '{requested}'. Available: "
                         + (", ".join(n for n in names if n) or "(none)")
                         + ". SVG imports into an EXISTING sketch - make one with sketch_create.")
        return error("No sketch to import the SVG into. SVG curves land in an EXISTING sketch - "
                     "make one with sketch_create, then name it in 'sketch'.")
    options = safe(lambda: mgr.createSVGImportOptions(path))
    if options is None:
        return error(f"createSVGImportOptions returned nothing for '{path}' - the file could not be "
                     "prepared as SVG.")

    sketch_name = safe(lambda: target.name)
    before = safe(lambda: target.sketchCurves.count, 0) or 0
    objects, ierr = _run_import(mgr, options, target)
    if ierr:
        return error(ierr + _restored_workspace_note(before_ws))
    after = safe(lambda: target.sketchCurves.count, 0) or 0

    if not objects and after <= before:
        return error(f"The SVG import reported no failure but sketch '{sketch_name}' gained no "
                     f"curves (still {after}) and importToTarget2 returned no objects. The file may "
                     "hold no path geometry." + _restored_workspace_note(before_ws))

    ws_keys, ws_note = _workspace_disclosure(before_ws)
    return ok({
        "imported": True,
        "format": "svg",
        "file": path,
        "into": f"sketch '{sketch_name}'",
        "objects_created": len(objects),
        "curves_added": after - before,
        "note": ("SVG curves landed in the sketch at 1/96 inch per SVG unit (measured: a 96-unit "
                 "square lands 25.4 mm), with SVG's y-down axis landing as NEGATIVE sketch y. "
                 "Measure one curve with model_measure_between and scale the sketch if the size "
                 "is wrong.") + ws_note,
        **ws_keys,
    })


def _open_document_handles():
    """Every open Document OBJECT in session order (not doc_get's rows), or None when the walk has a
    hole - a slot that did not read makes 'which document is new' undecidable, not 'none'."""
    docs = safe(lambda: app.documents)
    total = safe(lambda: docs.count) if docs is not None else None
    if total is None:
        return None
    out = []
    for i in range(total):
        d = safe(lambda i=i: docs.item(i))
        if d is None:
            return None
        out.append(d)
    return out


def _newcomer(before):
    """(document, open_index) for the ONE document open now that was not open in `before`, else
    (None, None) - equality, never identity, since a Document wrapper is not identity-stable."""
    after = _open_document_handles()
    if before is None or after is None:
        return None, None
    fresh = [(i, d) for i, d in enumerate(after)
             if not any(safe(lambda d=d, b=b: d == b, False) for b in before)]
    if len(fresh) != 1:
        return None, None
    return fresh[0][1], fresh[0][0]


def _index_of(doc):
    """The 'open:N' index `doc` answers to right now, or None when the session walk cannot place it
    - read BEFORE any close, since a closed document leaves that walk."""
    handles = _open_document_handles()
    if handles is None:
        return None
    hits = [i for i, d in enumerate(handles) if safe(lambda d=d: d == doc, False)]
    return hits[0] if len(hits) == 1 else None


def _discard(doc):
    """Close the empty document THIS call opened and say which happened - closed again, or left
    open at the address that reaches it."""
    name = safe(lambda: doc.name)
    index = _index_of(doc)
    try:
        discarded = doc.close(False) is True
    except Exception:  # noqa: BLE001 - a rollback that refuses is reported, never sunk
        discarded = False
    if discarded:
        return f" The empty document '{name}' this call opened was closed again."
    where = (f" at open:{index}" if index is not None else "")
    how = (f"doc_close(name='open:{index}')" if index is not None
           else "doc_close, addressing it by the open:N doc_get publishes")
    return (f" This call left an empty document '{name}' open{where} and could not close it - "
            f"close it with {how}.")


def _blank_document_note(before):
    """The sentence for a document importToNewDocument opened and then FAILED into, told apart by
    DIFFERENCE against `before`. '' when no single newcomer can be identified."""
    doc, _index = _newcomer(before)
    return _discard(doc) if doc is not None else ""


def _import_to_new_document(mgr, fmt, path, before_ws=(None, None)):
    # A SUCCESSFUL import hands over a new document, and Design is the workspace that document
    # belongs in - so only the error paths below put the caller's workspace back.
    options, oerr = _options(mgr, fmt, path)
    if oerr:
        return error(oerr)
    # A FAILED importToNewDocument can still leave a document open and ACTIVE, so the session list
    # is read on both sides of the call and the newcomer is discarded rather than left in front of
    # a caller that never asked for it.
    before_docs = _open_document_handles()
    try:
        doc = mgr.importToNewDocument(options)
    except Exception as e:
        return error(f"Import failed (importToNewDocument raised): {e}"
                     + _blank_document_note(before_docs)
                     + _restored_workspace_note(before_ws))
    if doc is None:
        return error("importToNewDocument returned null, which the API reports for a FAILED import "
                     "- no document was created." + _blank_document_note(before_docs)
                     + _restored_workspace_note(before_ws))

    new_design = safe(lambda: adsk.fusion.Design.cast(
        doc.products.itemByProductType('DesignProductType')))
    if new_design is None:
        # This path HOLDS the document, so its address is read off it directly - a difference walk
        # can answer nothing while the document is right here, and this one is left open to inspect.
        index = _index_of(doc)
        at = f" at open:{index}" if index is not None else ""
        return error(f"A new document was opened for '{path}' but it carries no Design product to "
                     f"read the imported geometry back from. The document is open{at} - inspect it "
                     "with workspace_orient." + _restored_workspace_note(before_ws))
    bodies, _sketches = _common.design_wide_counts(new_design)
    root = safe(lambda: new_design.rootComponent)
    occurrences = (safe(lambda: root.occurrences.count, 0) or 0) if root is not None else 0

    if bodies <= 0 and occurrences <= 0:
        return error(f"A new document was created for '{path}' but holds no body and no occurrence "
                     "- the import landed nothing." + _discard(doc)
                     + _restored_workspace_note(before_ws))

    return ok({
        "imported": True,
        "format": fmt,
        "file": path,
        "into": f"new document '{safe(lambda: doc.name)}'",
        "bodies": bodies,
        "occurrences": occurrences,
        "note": ("The new document is UNSAVED and is now the active document. Save it with "
                 "doc_save_as to give it a cloud identity, or discard it with doc_close."),
    })


def handler(file_path: str = "", format: str = "", into_component: str = "", sketch: str = "",
            plane: str = "xy", new_document: bool = False, sketch_component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    path = (file_path or "").strip()
    if not path:
        return error("file_path is required - the full path to a CAD file on this machine's disk.")

    fmt, ferr = _resolve_format(path, format)
    if ferr:
        return error(ferr)
    if new_document and fmt in _SKETCH_FORMATS:
        return error(f"A {fmt.upper()} file cannot be imported to a new document - "
                     "importToNewDocument does not accept DXF or SVG options. Import into the open "
                     "design instead (new_document=false): DXF creates sketches in a component, SVG "
                     "imports into an existing sketch.")
    if not safe(lambda: os.path.isfile(path), False):
        return error(f"No readable file at '{path}'. Pass a full path on THIS machine's disk; a "
                     "cloud file must be downloaded first, or referenced with doc_insert_occurrence.")

    mgr = safe(lambda: app.importManager)
    if mgr is None:
        return error("Application.importManager is unavailable - nothing can be imported.")

    # Read BEFORE the new-document branch so BOTH import routes have a workspace to put back. That
    # importToNewDocument switches the workspace the way importToTarget2 does is NOT MEASURED - the
    # read costs nothing, and the restore below is a no-op when nothing changed.
    before_ws = _active_workspace()
    if new_document:
        return _import_to_new_document(mgr, fmt, path, before_ws)

    design = _common.design()
    if not design:
        return error("No active design to import into. Open or create a document first (see "
                     "doc_new), or pass new_document=true.")
    if fmt == "svg":
        return _import_svg(mgr, design, path, sketch, sketch_component, before_ws)
    if fmt == "dxf":
        return _import_dxf(mgr, design, path, into_component, plane, before_ws)
    return _import_solid(mgr, design, path, fmt, into_component, before_ws)


TOOL_DESCRIPTION = (
    "Import a CAD file from LOCAL DISK: solids into a component, DXF as sketches, SVG into a sketch."
)

tool = (
    Tool.create_simple(name="doc_insert_import", description=TOOL_DESCRIPTION)
    .add_input_property("file_path", {"type": "string"})
    .add_input_property(*_FORMAT.as_property())
    .add_input_property(*_INTO_COMPONENT.as_property())
    .add_input_property("sketch", {"type": "string",
            "description": "SVG only; default the most recent."})
    .add_input_property(*_sketch_detail.component_scope("sketch_component", narrows="sketch"))
    .add_input_property(*_PLANE.as_property())
    .add_input_property("new_document", {"type": "boolean"})
    .add_required_input("file_path")
    .strict_schema()
)

class _FeatureHealthyHere(_assert.FeatureHealthy):
    """FeatureHealthy, declared SKIPPED when the import lands in a NEW document: the kind walks the
    timeline items past a before-count, and with new_document=True those two reads describe two
    different timelines, so the payload says the health gate did not run."""

    input_keys = ("new_document",)

    def verify(self, kwargs, payload, before):
        if kwargs.get("new_document"):
            return "", {"feature_health_verified": False,
                        "feature_health_note": ("The import went to a NEW document, so the timeline "
                                                "health baseline taken on the previous one does not "
                                                "describe it - read the new document's timeline with "
                                                "design_get.")}
        return super().verify(kwargs, payload, before)


item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_FeatureHealthyHere()],
                             # Beside the health gate: each arm counts the REQUESTED target's own
                             # bodies/sketches/occurrences either side of the import, so where the
                             # geometry landed is read back off the component that was named.
                             verification=Verification(
                                 kind="inline",
                                 evidence_test="tests/unit/test_doc_insert_import.py::TestSolidImport"
                                               "::test_an_assembly_landing_as_occurrences_counts_as_landed",
                                 rung="value"))


def register_tool():
    register(item)
