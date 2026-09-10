# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: cut a MeshBody by a plane - trim it, split it into two bodies, or split the
triangulation in place. WRITES; the write runs inside a BaseFeature edit scope in a parametric design.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from ._common import target_component as _target_component
from . import _geom
from . import _inputs
from ._mesh_common import _area_volume, _mesh_moved, _result_mesh_of, _tri_count
from ._design_common import run_in_base_feature

app = adsk.core.Application.get()

_CUT_MESH = _inputs.MeshBodyRef("mesh", required=True)
_CUT_PLANE = _inputs.PlaneRef("plane", required=True)
_CUT_TYPE = _inputs.Choice("cut_type", ["trim", "split_body", "split_faces"], default="trim")
_CUT_FILL = _inputs.Choice("fill", ["none", "minimal", "uniform"], default="minimal")

# The cut types that re-triangulate the SAME mesh instead of adding a body, so the target's own
# triangle count is what gates them; split_body is gated on the body count.
_IN_PLACE_CUTS = ("trim", "split_faces")

# What a plane that missed the mesh would have done, per cut type - the pre-flight refusal quotes it.
_MISS_EFFECT = {
    "trim": ("a trim by a plane the mesh does not straddle either keeps everything or DESTROYS the "
             "whole mesh (flip picks which), and a destroyed mesh is not recoverable through this "
             "tool"),
    "split_faces": "split_faces would split no facet, since no facet crosses the plane",
    "split_body": "split_body would leave one body, since the plane separates nothing",
}

# An emptied mesh is not recovered by removing the timeline entry that wrapped the cut, so no
# disposition sentence points a caller at design_delete_feature to get the geometry back.
_NO_RECOVERY = ("Removing the timeline entry does NOT bring the mesh data back - a "
                "design_delete_feature on the wrapping base feature returned deleted:true while the "
                "mesh still read 0 triangles. Recover the mesh from the document's own history "
                "(Fusion's undo, or doc_restore_version), not from a tool call.")

# A corner within this band counts as ON the plane, not to one side of it: 1e-7 cm is a nanometre.
_ON_PLANE_CM = 1e-7


def _plane_misses_mesh(mesh, plane_entity, plane_geom):
    """(missed, clearance_cm): does the cutting plane fail to STRADDLE the mesh's bounding box?
    clearance_cm is the nearest corner's distance to the plane, None when the box merely touches it,
    and (None, None) whenever the reads taken cannot answer."""
    # A ConstructionPlane's .geometry is COMPONENT-LOCAL, and a native body's .boundingBox is
    # component-local while an occurrence proxy's is root-space - so the two compare only when the
    # mesh AND the plane are natives of ONE component. Any other pairing answers (None, None).
    if safe(lambda: mesh.assemblyContext) is not None:
        return None, None
    if safe(lambda: plane_entity.assemblyContext) is not None:
        return None, None
    owner, host = _inputs.entity_component(plane_entity), safe(lambda: mesh.parentComponent)
    if owner is None or host is None or _common.same_component(owner, host) is not True:
        return None, None

    g = plane_geom
    if safe(lambda: g.normal) is None:
        g = safe(lambda: plane_geom.geometry)    # a ConstructionPlane carries its Plane on .geometry
    n = _geom.unit_vector(safe(lambda: g.normal))
    o = safe(lambda: g.origin)
    org = [safe(lambda ax=ax: getattr(o, ax)) for ax in ("x", "y", "z")]
    span = _geom._aabb_extents(mesh)             # the ONE body-AABB coordinate read
    if n is None or span is None or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in org):
        return None, None

    # Signed distance of each of the 8 box corners to the plane: n . (corner - origin). No corner
    # strictly BELOW the plane, or none strictly above it, means the box does not straddle it.
    d = [n[0] * (x - org[0]) + n[1] * (y - org[1]) + n[2] * (z - org[2])
         for x in span[0] for y in span[1] for z in span[2]]
    if min(d) >= -_ON_PLANE_CM or max(d) <= _ON_PLANE_CM:
        nearest = min(abs(v) for v in d)
        # A box whose nearest corner sits ON the plane TOUCHES it - reporting that as "0.0 cm clear"
        # would read as a measured gap, so the clearance is None and the caller words tangency.
        return True, (None if nearest <= _ON_PLANE_CM else round(nearest, 6))
    return False, None


def _av_phrase(before, after) -> str:
    """The area/volume evidence a flat-triangle-count verdict quotes, per signal: the before -> after
    reading, or 'unreadable' for a signal that could not be read at both ends (never a stand-in 0)."""
    return "; ".join(
        f"{label} unreadable" if b is None or a is None else f"{label} {b} -> {a} {unit}"
        for label, unit, b, a in (("area", "cm2", before[0], after[0]),
                                  ("volume", "cm3", before[1], after[1])))


def _rollback_state(design, feat, mesh) -> str:
    """What the model holds after a refused cut: the rollback attempt's outcome plus the remedy for
    whatever is still there. The proof a rollback took is the TIMELINE count dropping, not
    deleteMe()'s own answer."""
    def _disposition():
        now = _tri_count(mesh)
        reads = "no readable triangle count" if now is None else f"{now} triangles"
        return reads, (_NO_RECOVERY if now == 0 else _common.failed_effect_remedy(design, feat))

    if not feat:
        reads, remedy = _disposition()
        return ("No feature object came back from the add, so there is no feature handle to roll back "
                f"and the cut is still in the model; the mesh reads {reads}. " + remedy)

    def _items():
        # counted, never safe(read, 0): an unreadable timeline is UNKNOWN, and a coerced 0 would fake
        # the drop that IS the rollback proof.
        return _common.counted(lambda: design.timeline.count)

    before_items = _items()
    try:
        did = feat.deleteMe()
    except Exception as e:
        reads, remedy = _disposition()
        return (f"The cut feature could NOT be rolled back (deleteMe raised: {e}) - it remains in the "
                f"model and the mesh reads {reads}. " + remedy)
    after_items = _items()
    reads, remedy = _disposition()
    if before_items is not None and after_items is not None:
        if after_items < before_items:
            rolled = (f"The cut feature was rolled back - the timeline re-reads {after_items} "
                      f"item(s), down from {before_items}, and the mesh now reads {reads}.")
            # The feature is gone, so the only remedy worth adding is the one for a mesh that is STILL
            # empty after a proven removal - which is exactly the measured case.
            return rolled + ("" if _NO_RECOVERY not in remedy else " " + _NO_RECOVERY)
        return (f"The cut feature REMAINS in the model - the timeline still re-reads {after_items} "
                f"item(s) after the rollback attempt (deleteMe returned {bool(did)}), and the mesh "
                f"reads {reads}. " + remedy)
    return (f"A rollback ran (deleteMe returned {bool(did)}) but the timeline could not be re-read to "
            f"confirm it; the mesh reads {reads}. Check the design with design_get. " + remedy)


def handler(mesh: str = "", plane: str = "", cut_type: str = "trim",
            fill: str = "minimal", flip: bool = False) -> dict:
    """Cut a MeshBody by a plane - trim, split_body, or split_faces - optionally filling the opening."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")
    mb, merr = _CUT_MESH.resolve(mesh)
    if merr:
        return error(merr)
    pl, perr = _CUT_PLANE.resolve(plane)
    if perr:
        return error(perr)
    ct, _ = _CUT_TYPE.resolve(cut_type)
    fl, _ = _CUT_FILL.resolve(fill)

    # The mesh's name, read BEFORE the mutation, so every refusal below names what was cut from a read
    # the cut itself cannot have touched.
    mesh_name = safe(lambda: mb.name)

    comp = safe(lambda: mb.parentComponent) or _target_component(design)
    feats = safe(lambda: comp.features.meshPlaneCutFeatures)
    if feats is None:
        return error("This design has no meshPlaneCutFeatures collection (mesh plane cut "
    "unavailable here).")

    # PlaneRef can resolve a planar BRepFace; the cut wants its geometry (a core.Plane) or a
    # ConstructionPlane. A ConstructionPlane is passed through; a face is reduced to its plane.
    cut_plane = pl
    if isinstance(pl, adsk.fusion.BRepFace):
        cut_plane = safe(lambda: pl.geometry)
        if cut_plane is None:
            return error("'plane': could not read the plane geometry off that face handle.")

    # What every refusal calls the plane: its own name when it has one (a construction plane), else the
    # raw input the caller wrote. Read once, so the pre-flight and the post-cut gates cannot diverge.
    plane_label = safe(lambda: pl.name) or (plane or "").strip() or "the requested plane"

    # PRE-FLIGHT: a plane the mesh's bounding box does not straddle cannot cut it, and running the
    # feature anyway destroys the mesh - so this refusal happens BEFORE any mutation, and is skipped
    # whenever the geometry could not be read.
    missed, clearance = _plane_misses_mesh(mb, pl, cut_plane)
    if missed:
        where = (f"only TOUCHES mesh '{mesh_name}' - its bounding box lies against the plane with no "
                 "corner strictly on the far side, so nothing of the mesh is on the side the cut "
                 "works on" if clearance is None else
                 f"does not reach mesh '{mesh_name}' - all 8 corners of the mesh's bounding box are "
                 f"on ONE side of it, {clearance} cm clear at the nearest")
        return error(
            f"Refusing before any cut: '{plane_label}' {where}. The plane does not straddle the mesh, "
            f"and {_MISS_EFFECT[ct]}. Move the plane so it passes THROUGH the mesh (mesh_get reports "
            "the mesh's bounding box in display units; any clearance quoted here is in cm), then "
            "retry.")

    # The design's OWN mode, read BEFORE any scope opens: designType reads DIRECT while a
    # base-feature edit scope is open, and add() returns nothing INSIDE that scope even in a
    # parametric design - so the returned feature is no evidence of the design's mode.
    design_mode = _inputs.current_design_type(design)

    def inner_op(base_feature):
        try:
            inp = feats.createInput(mb, cut_plane)
        except Exception as e:
            return error(f"Could not create the mesh-plane-cut input: {e}")
        if inp is None:
            return error("meshPlaneCutFeatures.createInput returned nothing.")

        _IN = "MeshPlaneCutFeatureInput"
        cts = safe(lambda: adsk.fusion.MeshPlaneCutTypes)
        if cts is None:
            return error("adsk.fusion.MeshPlaneCutTypes is unavailable on this Fusion version.")
        cerr = _common.set_verified(inp, "meshPlaneCutType", safe(lambda: {
            "trim": cts.TrimMeshPlaneCutType,
            "split_body": cts.SplitBodyMeshPlaneCutType,
            "split_faces": cts.SplitFacesMeshPlaneCutType,
        }.get(ct)), f"cut_type='{ct}'", _IN)
        if cerr:
            return error(cerr)

        # meshPlaneCutFillType is "Only valid if meshPlaneCutType is not SplitFacesMeshPlaneCutType"
        # (API doc), so split_faces skips it rather than asserting a fill it cannot honour.
        fill_applied = None
        if ct != "split_faces":
            fts = safe(lambda: adsk.fusion.MeshPlaneCutFillTypes)
            if fts is None:
                return error("adsk.fusion.MeshPlaneCutFillTypes is unavailable on this Fusion version.")
            ferr = _common.set_verified(inp, "meshPlaneCutFillType", safe(lambda: {
                "none": fts.NoFillMeshPlaneCutFillType,
                "minimal": fts.MinimalMeshPlaneCutFillType,
                "uniform": fts.UniformMeshPlaneCutFillType,
            }.get(fl)), f"fill='{fl}'", _IN)
            if ferr:
                return error(ferr)
            fill_applied = fl

        if flip:
            xerr = _common.set_verified(inp, "isFlip", True, "flip=true", _IN)
            if xerr:
                return error(xerr)

        # The pre-cut readings, taken inside inner_op so they share the add's scope: the cut is
        # detected by side effect, not by the (often None) feature object.
        def _mesh_count():
            return safe(lambda: comp.meshBodies.count)
        before_mesh_count = _mesh_count()
        before_tri = _tri_count(mb)
        before_av = _area_volume(mb)

        # add() returns nothing for a non-parametric feature (a direct design, or an add inside the
        # base-feature scope), so success is the changed mesh body set, not this return.
        try:
            feat = feats.add(inp)
        except Exception as e:
            return error(f"Mesh plane cut failed (meshPlaneCutFeatures.add raised): {e}")
        # The open BaseFeature cannot be re-found once the scope closes, so capture its name here.
        return {"feat": feat, "before_mesh_count": before_mesh_count, "before_tri": before_tri,
    "before_area_volume": before_av,
    "after_mesh_count": _mesh_count(), "fill_applied": fill_applied,
    "base_feature_name": safe(lambda: base_feature.name) if base_feature else None}

    result, scope_err = run_in_base_feature(design, comp, inner_op)
    if scope_err:
        return scope_err
    if isinstance(result, dict) and result.get("isError") is True:
        return result

    feat = result["feat"]
    bf_name = result["base_feature_name"]
    before_mesh_count = result["before_mesh_count"]
    after_mesh_count = result["after_mesh_count"]
    # Parametric: the feature carries .bodies. Non-parametric (feat None): the cut applied (no
    # exception); split_body raises the mesh body count, trim/split_faces modify in place. We report
    # the observed mesh body set (before/after) rather than the unavailable feature object.
    bodies = ([{"name": safe(lambda b=b: b.name), "handle": safe(lambda b=b: b.entityToken)}
               for b in _common.result_bodies(feat)] if feat else [])

    # A cut whose fill adds exactly as many triangles as it removed lands with the triangle count
    # flat, so the mesh's own area/volume is read as a second, independent signal.
    before_tri = result["before_tri"]
    before_av = result["before_area_volume"]
    result_mesh = (_result_mesh_of(feat, mb) if feat else mb) if ct in _IN_PLACE_CUTS else None
    after_tri = _tri_count(result_mesh) if result_mesh is not None else None
    after_av = _area_volume(result_mesh) if result_mesh is not None else (None, None)
    count_flat_but_moved = False
    if ct in _IN_PLACE_CUTS and before_tri and after_tri is not None:
        if after_tri == 0:
            # A trim by a plane clear of the mesh removes every triangle and raises nothing, leaving
            # an empty husk of a body - the triangle count is the only signal that this happened.
            if ct == "trim":
                cause = (f"The likely cause is that '{plane_label}' does not pass through the mesh: "
                         "trim keeps ONE side of the plane, and every triangle was on the discarded "
                         f"side. Retry with flip={'false' if flip else 'true'} to keep the other side, "
                         "or move the plane so it passes through the mesh (mesh_get reports its "
                         "bounding box).")
            else:
                # split_faces only ADDS triangles, so flip does not choose a kept side here and
                # naming it would misdescribe the operation.
                cause = ("split_faces splits facets in place and never removes triangles (an "
                         "intersecting plane took a mesh 352 -> 368), so zero is not a "
                         "split_faces outcome at all - the mesh was destroyed rather than cut. Read "
                         "the design back with design_get before retrying.")
            return error(
                f"The {ct} cut ANNIHILATED mesh '{mesh_name}': all {before_tri} triangles were "
                "removed and the mesh body now reads 0 triangles, so no geometry of it is left. "
                + cause + " " + _rollback_state(design, feat, mb))
        if after_tri == before_tri:
            # A flat count alone cannot refuse: the refusal needs the second signal to AGREE that
            # nothing moved, since a moved area/volume over a flat count is a landed cut.
            moved = _mesh_moved(before_av, after_av)
            if moved is not True:
                if ct == "trim":
                    why = (f"'{plane_label}' cut no triangles off it, which is what a plane that does "
                           "not pass through the mesh does.")
                else:
                    why = (f"split_faces ADDS triangles where the plane crosses the facets (an "
                           f"intersecting plane took a mesh 352 -> 368), so an unchanged count means "
                           f"'{plane_label}' does not cross the mesh.")
                second = (f"Its area and volume did not move either ({_av_phrase(before_av, after_av)})."
                          if moved is False else
                          f"Its area/volume could not be read as a second check "
                          f"({_av_phrase(before_av, after_av)}), so the triangle count is the only "
                          "evidence here.")
                return error(
                    f"The {ct} cut changed nothing: mesh '{mesh_name}' still reads {before_tri} "
                    f"triangles, unchanged. {second} {why} Move the plane into the mesh (mesh_get "
                    "reports its bounding box), then retry. " + _rollback_state(design, feat, mb))
            count_flat_but_moved = True

    note = ("Mesh cut by the plane. 'trim' keeps one side, 'split_body' makes two mesh bodies, "
    "'split_faces' cuts the triangulation in place. fill controls the new opening "
    "(none / minimal / uniform). Use flip=true to keep/cut the other side.")
    if feat is None:
        note += " " + _common.null_feature_note(design, feat, bf_name, "plane cut")
        # result_body_count counts the FEATURE's bodies, so with no feature there is nothing to count
        # them off: the count is UNKNOWN, not zero (the _common.counted rule for an absent count).
        note += (" 'result_body_count' is null for the same reason - it counts the feature's bodies "
                 "and there is no feature object; the other counts here were read off the model.")

    payload = {
    "cut": True,
    "mesh": mesh_name,
    "cut_type": ct,
    "fill": result["fill_applied"],
    "flipped": bool(flip),
    "feature": safe(lambda: feat.name) if feat else None,
    "design_mode": design_mode,
    "base_feature": bf_name,
    "result_bodies": bodies,
    "result_body_count": len(bodies) if feat else None,
    "mesh_body_count": after_mesh_count,
    "mesh_bodies_before": before_mesh_count,
    }

    # Published only for the in-place cuts, where both numbers describe the same body (split_body's
    # second piece is a body of its own, counted by mesh_body_count).
    if ct in _IN_PLACE_CUTS:
        payload["triangles_before"] = before_tri
        payload["triangles_after"] = after_tri
        # The second signal - null where the build could not read it, never a stand-in 0.
        payload["area_before_cm2"], payload["area_after_cm2"] = before_av[0], after_av[0]
        payload["volume_before_cm3"], payload["volume_after_cm3"] = before_av[1], after_av[1]
        if count_flat_but_moved:
            note += (f" The triangle count is unchanged ({before_tri}), but the mesh's own geometry "
                     f"MOVED ({_av_phrase(before_av, after_av)}), so the cut landed: a fill that adds "
                     "as many triangles as the cut removed reads flat on the count alone.")
        if not before_tri or after_tri is None:
            # The gate above needs a nonzero before-count AND a readable after-count to mean anything;
            # without both, this call cannot tell a real cut from a no-op, and says so.
            note += (" The cut's effect is UNVERIFIED: the triangle count reads "
                     f"before={before_tri}, after={after_tri}, and a zero or unreadable count on "
                     "either side cannot show what the cut removed - read the mesh back with "
                     "mesh_get.")

    # split_body separates the mesh only when the body count rises: on a non-watertight mesh the cut
    # applies but yields one body, which `became_split` reports so cut:true is not read as a split.
    if ct == "split_body":
        before = before_mesh_count if before_mesh_count is not None else 0
        after = after_mesh_count if after_mesh_count is not None else 0
        became_split = after > before
        payload["became_split"] = became_split
        if not became_split:
            # Report the cause only when it was READ; split_body needs a closed mesh, but an open
            # mesh is not the only way to get one body back, so an unreadable flag stays unnamed.
            closed = safe(lambda: mb.isClosed)
            payload["mesh_is_closed"] = closed
            note += " NOTE: split_body did not separate the mesh into two bodies."
            if closed is False:
                note += (" This mesh reads is_closed=false, and split_body needs a closed mesh - "
                         "close it with mesh_repair, then retry.")
            elif closed is True:
                note += (" This mesh reads is_closed=true, so the open-mesh explanation does not "
                         "apply - check that the plane actually passes through the body.")
            else:
                note += " Its is_closed flag could not be read, so the reason is unconfirmed."

    payload["note"] = note
    return ok(payload)


TOOL_DESCRIPTION = (
    "Cut a MESH body with a plane - trim, split into two bodies, or split the triangulation."
)

_CUT_SPEC = [_CUT_MESH, _CUT_PLANE, _CUT_TYPE, _CUT_FILL]
tool = (
    _inputs.apply_to_tool(
        Tool.create_simple(name="mesh_plane_cut", description=TOOL_DESCRIPTION),
        _CUT_SPEC)
    .add_input_property("flip", {"type": "boolean",
            "description": "Keeps the other side of the plane."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler,
    run_on_main_thread=True,
    verification=Verification(
        kind="inline",
        rung="geometry",
        evidence_test="tests/unit/test_mesh_plane_cut.py::TestPlaneCut"
       "::test_trim_refuses_when_the_triangle_count_is_unchanged"))


def register_tool():
    register(item)
