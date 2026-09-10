# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: insert a saved cloud document into the active design as a new component
occurrence - the API equivalent of Insert > Insert into Current Design. It comes in as an external
reference that stays linked to the source and tracks its version (never a severed embedded copy).
Referencing between two SAVED documents requires a shared project (Fusion refuses it otherwise); an
unsaved host is fine.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _doc_common
from . import _inputs
from . import _data_common
from ._data_common import _b64url_decode, _resolve_data_file


# --- target component / occupant resolution (via the shared OccurrenceRef kind) ---

_INTO_COMPONENT = _inputs.OccurrenceRef("into_component")
_REMOVE_EXISTING = _inputs.OccurrenceRef("remove_existing")


def _bound_version(design, lineage):
    """The source version this host's reference(s) to `lineage` hold - None when the source id did
    not read, when no reference matched, when a matched row's own version did not read, or when two
    rows disagree (one source can be referenced several times, at two versions)."""
    # Matched on the LINEAGE key, the way every other reference walk here matches: a reference id
    # carrying a '?version=' suffix would otherwise miss the source it addresses. An UNREAD id keys
    # to '' and would match every other unread one, so it answers nothing instead.
    want = _doc_common._lineage_key(lineage)
    if not want:
        return None
    doc = safe(lambda: design.parentDocument)
    held = [safe(lambda r=ref: r.version)
            for ref in _common.iter_collection(safe(lambda: doc.documentReferences))
            if _doc_common._lineage_key(safe(lambda r=ref: r.dataFile.id)) == want]
    # A row whose version did not read is UNKNOWN, never agreement: {2, None} is not "bound at 2".
    if not held or any(v is None for v in held):
        return None
    return held[0] if len(set(held)) == 1 else None


def handler(document_id: str = "", into_component: str = "",
            remove_existing: str = "", x: float = 0.0, y: float = 0.0, z: float = 0.0,
            units: str = "mm", rotate_deg: float = 0.0, rotate_axis: str = "z") -> dict:
    """See TOOL_DESCRIPTION."""
    raw = (document_id or "").strip()
    if not raw:
        return error("Provide 'document_id' - the lineage URN (or web URL) of the saved cloud "
    "document to insert.")

    design = _common.design()
    if not design:
        return error("No active design. Open the host document first.")

    data_file, resolved, candidates = _resolve_data_file(raw)
    if not data_file:
        tried = ", ".join(candidates) if candidates else raw
        return error(f"Could not resolve '{raw}' to a saved document. Tried: {tried}. Pass a "
    "lineage URN or web URL (from data_get). The document must be SAVED to the cloud.")

    if (into_component or "").strip():
        into_occ, into_err = _INTO_COMPONENT.resolve(into_component)
        if into_err:
            return error(into_err)
        comp = safe(lambda: into_occ.component)
        if not comp:
            return error(f"Occurrence '{into_component}' has no component to insert into.")
        comp_desc = f"component '{safe(lambda: comp.name)}'"
    else:
        comp = design.rootComponent
        comp_desc = "root component"

    # Build the placement transform BEFORE any removal - a bad units/axis value (or a refused
    # rotation) must refuse while the assembly is still intact, not after a part is already gone.
    k = _common.scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    import math
    transform = adsk.core.Matrix3D.create()
    if rotate_deg:
        axis_vec = _inputs._AXIS_VECS.get((rotate_axis or "z").strip().lower())
        if not axis_vec:
            return error(f"Unknown rotate_axis '{rotate_axis}'. Use x, y, or z.")
        # Rotate about the world origin; the translation set below places the occurrence.
        # setToRotation answers a bool, and a false is a rotation that never landed on the matrix -
        # the occurrence would insert UNROTATED while the payload echoed the request.
        did_rot = safe(lambda: transform.setToRotation(
            math.radians(float(rotate_deg)),
            adsk.core.Vector3D.create(*axis_vec), adsk.core.Point3D.create(0, 0, 0)), False)
        if not did_rot:
            return error(f"setToRotation({rotate_deg} deg about {rotate_axis}) was refused - the "
                         "placement rotation could not be built, so nothing was inserted or removed.")
    if x or y or z:
        transform.translation = adsk.core.Vector3D.create(float(x) * k, float(y) * k, float(z) * k)

    # Optionally remove a named existing occurrence first (its joints are removed with it).
    removed = None
    if (remove_existing or "").strip():
        existing, existing_err = _REMOVE_EXISTING.resolve(remove_existing)
        if existing_err:
            return error(existing_err)
        removed = safe(lambda: existing.name)
        did = safe(lambda: existing.deleteMe(), False)
        if not did:
            return error(f"Failed to remove existing occurrence '{removed}' (deleteMe returned "
    "false). It may be referenced/locked.")

    # PARTIAL-SUCCESS DISCLOSURE: 'remove_existing' has already DELETED an occurrence by the time
    # any failure below can happen. A clean-looking failure would let the caller retry into an
    # assembly missing a part it believes is still there - every post-removal exit carries this.
    gone = (f" NOTE: occurrence '{removed}' was ALREADY REMOVED before this failure (its joints "
            "went with it) - the assembly no longer holds it." if removed else "")

    try:
        new_occ = comp.occurrences.addByInsert(data_file, transform, True)
    except Exception as e:
        return error(f"Insert failed: {e}. (An external reference requires the source and host in "
                     f"the SAME PROJECT - save the host into the source's project, then retry.){gone}")
    if not new_occ:
        return error("addByInsert returned nothing (the insert did not produce an occurrence)."
                     + gone)
    if safe(lambda: new_occ.isValid) is False:
        return error("addByInsert returned an occurrence but it reads isValid=false - the insert "
                     "did not land." + gone)
    # Verify the associative link actually formed - this tool only ever inserts a live reference,
    # so an occurrence that came in embedded (isReferencedComponent=false) is a silent failure.
    is_ref = safe(lambda: new_occ.isReferencedComponent)
    if is_ref is False:
        return error("Insert landed but the occurrence is NOT an external reference "
                     "(isReferencedComponent=false) - the associative link did not form. Confirm "
                     "the source and host share a project, then retry." + gone)

    # WHICH version the reference bound, read off the reference itself rather than assumed from the
    # insert: a source whose stream moved on binds a version the source's own tip is already past.
    lineage = safe(lambda: data_file.id)
    bound = _bound_version(design, lineage)
    latest = safe(lambda: data_file.latestVersionNumber)
    return ok({
        "inserted": True,
        "document_name": safe(lambda: data_file.name),
        "document_id": resolved,
        "into_component": comp_desc,
        "new_occurrence_name": safe(lambda: new_occ.name),
        "is_reference": is_ref,
        "bound_version": bound,
        "source_latest_version": latest,
        # null, never false, when either number did not read - an unread pair is not a stale one.
        "bound_is_tip": (bound == latest) if (bound is not None and latest is not None) else None,
        "removed_occurrence": removed,
        "placed_at": ({"x": x, "y": y, "z": z, "units": units} if (x or y or z) else "origin"),
        "rotate_deg": float(rotate_deg or 0.0),
        "note": ("Inserted at the requested placement, from the source's last SAVED cloud version. "
            "bound_version is the version this reference holds - null where none read it or two "
            "disagree; bound_is_tip whether it is the source's latest - null if either is unknown, "
            "false is brought current with doc_update_xref. A removed occurrence leaves features "
            "that referenced its geometry carrying reference failures."),
    })


TOOL_DESCRIPTION = (
    "Insert a SAVED cloud document into the active design as a linked external-reference "
    "occurrence, placed at x/y/z. It comes in at the source's last SAVED cloud version."
)

tool = (
    Tool.create_with_string_input(
        name="doc_insert_occurrence",
        description=TOOL_DESCRIPTION,
        input_param_name="document_id",
        input_param_description="Lineage URN or web URL, from data_get.",
    )
    .add_input_property(*_INTO_COMPONENT.as_property())
    .add_input_property(*_REMOVE_EXISTING.as_property())
    .add_input_property("x", {"type": "number"})
    .add_input_property("y", {"type": "number"})
    .add_input_property("z", {"type": "number"})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("rotate_deg", {"type": "number", "description": "Rotation about 'rotate_axis', in degrees."})
    .add_input_property(*_inputs.frame_axis("rotate_axis", default="z").as_property())
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # isReferencedComponent is read off the inserted occurrence and anything but a reference is an
    # error, and bound_version reads the version the host's reference actually holds.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_doc_insert_occurrence.py::TestAlwaysReference"
                      "::test_embedded_result_bites",
        rung="value"))


def register_tool():
    register(item)
