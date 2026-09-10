# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Create a Joint Origin - a reusable coordinate frame - at a specified or COMPUTED anchor. A
computed anchor is read back and reported so the caller can verify where it landed. WRITES."""

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import apply_rename, error, ok, safe
from . import _common
from . import _inputs
from . import _sketch_detail
from . import _joints

_TARGETS = ("at", "origin")
_ANCHORS = ("coordinates", "sketch_line", "sketch_point", "geometry", "bbox_center", "face_center")
_KEYPOINTS = {"start": 0, "middle": 1, "end": 2, "center": 3}

_ANCHOR_CHOICE = _inputs.Choice("anchor", list(_ANCHORS), default="coordinates")
_TARGET_CHOICE = _inputs.Choice("target", list(_TARGETS), default="at",
                                description="'origin' ignores x/y/z.")
_KEYPOINT_CHOICE = _inputs.Choice("keypoint", list(_KEYPOINTS), default="start")

# anchor='geometry': a BRep face/edge/vertex HANDLE from find_geometry - the frame's orientation
# comes from that real geometry (a planar face's normal, a cylinder/edge's axis, a hole edge).
# anchor='face_center' reuses this same 'geometry' handle, requiring a PLANAR face.
_GEOM = _inputs.GeometryHandle("geometry", require="any")

# anchor='bbox_center': the thing whose WORLD bounding-box center becomes the origin. TargetRef resolves
# a body (handle or name), an occurrence (fullPathName), or a component - and refuses an ambiguous name.
_BBOX_TARGET = _inputs.TargetRef(
    "bbox_target", allow=("body", "occurrence", "component"),
    description="bbox_center: its box center, read ONCE - a later resize does not move the frame.")

# anchor='bbox_center': the axis the frame's Z is aligned to (world x/y/z, or a handle/name at a
# straight edge, sketch line or construction axis the axis runs along). 'flip' reverses it 180 deg.
_ORIENT_AXIS = _inputs.AxisRef("orient_axis", default="z")

# The component whose jointOrigins collection RECEIVES the origin: adding through
# sub.component.jointOrigins lands a JO whose parentComponent IS the sub-component, which is what
# lets a JO serve as the sub-component side of a joint. Omitted = root.
_COMPONENT = _inputs.OccurrenceRef("component", required=False,
                                   description="Omit for root.")


def _vec(v):
    if v is None:
        return None
    return [round(safe(lambda: v.x, 0.0), 6), round(safe(lambda: v.y, 0.0), 6),
            round(safe(lambda: v.z, 0.0), 6)]


def _is_identity(matrix):
    """True/False for a 4x4 transform reading as the identity; None when it cannot be read at all."""
    try:
        vals = [float(v) for v in (safe(lambda: matrix.asArray()) or [])]
    except Exception:
        return None
    if len(vals) != 16:
        return None
    ident = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
    return all(abs(v - i) <= 1e-9 for v, i in zip(vals, ident))


def _sits_at_world_origin(occ):
    """True when `occ` AND every occurrence it nests inside carry an identity transform - the one
    case where a world coordinate and that component's own coordinate are the same numbers; None
    when a transform could not be read. Occurrence.transform is relative to the PARENT component,
    so only the whole chain answers this."""
    node = occ
    for _ in range(64):      # a path this deep is a cycle, not an assembly
        if node is None:
            return True
        m = safe(lambda n=node: n.transform2)
        if m is None:
            m = safe(lambda n=node: n.transform)
        verdict = _is_identity(m) if m is not None else None
        if verdict is not True:
            return verdict
        node = safe(lambda n=node: n.assemblyContext)
    return None


def _world_space_refusal(anchor, occ, verdict):
    """The refusal for a WORLD-coordinate anchor aimed at a component that is not at the world origin
    (or whose placement cannot be read). A joint origin inside a component is positioned in THAT
    component's space, so those coordinates would land the frame somewhere else."""
    where = safe(lambda: occ.fullPathName) or safe(lambda: occ.name) or "that occurrence"
    why = ("could not be read" if verdict is None else
           "is not the identity - it is moved or rotated in the assembly")
    return (f"'component': anchor='{anchor}' places the frame from WORLD coordinates, but a joint "
            f"origin inside a component is positioned in THAT component's own space, and the "
            f"placement transform of '{where}' {why}. Refusing rather than landing the frame "
            "somewhere else. Anchor on geometry inside the component instead "
            "(anchor='geometry'/'face_center'/'sketch_line'), use anchor='coordinates' with "
            "target='origin' to sit at the component's own origin, or omit 'component' to land on "
            "the root, where the coordinates ARE world coordinates.")


def _bbox_center_cm(entity):
    """World bounding-box CENTER (cm) of a body/occurrence/component, or None if it has no box.
    BoundingBox3D is world-axis-aligned, so its center is the geometric center in world space."""
    bb = safe(lambda: entity.boundingBox)
    if bb is None:
        return None
    mn = safe(lambda: bb.minPoint)
    mx = safe(lambda: bb.maxPoint)
    if mn is None or mx is None:
        return None
    return ((safe(lambda: mn.x, 0.0) + safe(lambda: mx.x, 0.0)) / 2.0,
            (safe(lambda: mn.y, 0.0) + safe(lambda: mx.y, 0.0)) / 2.0,
            (safe(lambda: mn.z, 0.0) + safe(lambda: mx.z, 0.0)) / 2.0)


def _axis_direction(raw_axis, flip):
    """Resolve orient_axis (world x/y/z, or a handle/name at a straight edge, sketch line or
    construction axis) to a UNIT direction (dx,dy,dz), optionally flipped 180 deg. Returns
    (dir, error)."""
    val, err = _ORIENT_AXIS.resolve(raw_axis)
    if err:
        return None, err
    tag, payload = val
    if tag == "world":
        d = [float(payload[0]), float(payload[1]), float(payload[2])]
    else:
        # ('edge', entity) - the axis runs ALONG the line. Read through the shared axis_line_of:
        # a bounded edge/sketch line carries startPoint/endPoint while a construction axis carries
        # origin/direction (and needs the world lift), and only that helper knows both shapes.
        pair, aerr = _inputs.axis_line_of("orient_axis", payload)
        if aerr:
            return None, aerr
        _point, vec = pair
        d = [safe(lambda: vec.x, 0.0), safe(lambda: vec.y, 0.0), safe(lambda: vec.z, 0.0)]
    n = (d[0] ** 2 + d[1] ** 2 + d[2] ** 2) ** 0.5
    if n <= 1e-9:
        return None, "orient_axis: the direction is zero-length."
    d = [c / n for c in d]
    if flip:
        d = [-c for c in d]
    return d, None


def _anchor_direction_line(comp, center_cm, dir_vec, length=1.0):
    """Draw a hidden helper sketch line from center_cm (cm) along dir_vec; the JO anchors on its START,
    so the frame sits AT center_cm with Z running along the line. Returns the SketchLine."""
    sketch = comp.sketches.add(comp.xYConstructionPlane)
    try:
        sketch.name = "JointOriginAnchor"
    except Exception:
        pass
    cx, cy, cz = center_cm
    dx, dy, dz = dir_vec
    P = adsk.core.Point3D.create
    line = sketch.sketchCurves.sketchLines.addByTwoPoints(
        P(cx, cy, cz), P(cx + dx * length, cy + dy * length, cz + dz * length))
    try:
        sketch.isVisible = False
    except Exception:
        pass
    return line


def _geometry_from_args(design, comp, anchor, target, x_cm, y_cm, z_cm,
                        sketch_name, entity_index, keypoint, geometry_handle=None,
                        bbox_target=None, orient_axis="z", flip=False, meta=None,
                        sketch_component=""):
    """Build the JointGeometry + a human description. Returns (geometry, desc, err). For a COMPUTED
    anchor (bbox_center/face_center), populates meta['anchor_cm'] with the world point used, so the
    handler can read it back against the created origin."""
    JG = adsk.fusion.JointGeometry

    if anchor == "bbox_center":
        resolved, terr = _BBOX_TARGET.resolve(bbox_target)
        if terr:
            return None, None, terr
        tent, tkind = resolved
        center = _bbox_center_cm(tent)
        if center is None:
            return None, None, ("bbox_center: could not read a bounding box for the target "
                                f"({safe(lambda: tent.name) or tkind}).")
        dvec, derr = _axis_direction(orient_axis, flip)
        if derr:
            return None, None, derr
        try:
            line = _anchor_direction_line(comp, center, dvec)
        except Exception as e:
            return None, None, f"bbox_center: could not build the orientation line: {e}"
        g = safe(lambda: JG.createByCurve(line, adsk.fusion.JointKeyPointTypes.StartKeyPoint))
        if meta is not None:
            meta["anchor_cm"] = center
            meta["anchor_source"] = f"bbox center of {safe(lambda: tent.name) or tkind}"
        return g, f"bbox center of {safe(lambda: tent.name) or tkind} (Z along orient_axis)", \
            (None if g else "bbox_center: createByCurve returned nothing for the orientation line.")

    if anchor == "face_center":
        ent, herr = _GEOM.resolve(geometry_handle)
        if herr:
            return None, None, herr
        if not isinstance(ent, adsk.fusion.BRepFace):
            return None, None, "face_center: the 'geometry' handle is not a face. Pass a PLANAR face handle."
        st = safe(lambda: ent.geometry.surfaceType)
        if st != adsk.core.SurfaceTypes.PlaneSurfaceType:
            return None, None, ("face_center needs a PLANAR face; that face is curved. Use "
                                "anchor='geometry' to anchor on a cylinder/cone axis.")
        g, _, err = _joints.build_joint_geometry(ent)
        if meta is not None:
            c = safe(lambda: ent.centroid)
            if c is not None:
                meta["anchor_cm"] = (safe(lambda: c.x, 0.0), safe(lambda: c.y, 0.0), safe(lambda: c.z, 0.0))
            meta["anchor_source"] = "face centroid"
        return g, "planar face center (Z = face normal)", err

    if anchor == "geometry":
        ent, herr = _GEOM.resolve(geometry_handle)
        if herr:
            return None, None, herr
        # planar face -> frame Z = face normal; non-planar (cylinder/cone) -> axis via keypoint;
        # edge/curve -> Z along the curve (the caller's keypoint choice); vertex -> position only.
        if isinstance(ent, adsk.fusion.BRepFace):
            g, label, err = _joints.build_joint_geometry(ent)
            desc = ("planar face (Z = face normal)" if label == "planar_face@center"
                    else "non-planar face (axis from the face)")
            return g, desc, err
        if isinstance(ent, adsk.fusion.BRepEdge):
            kp_val = _KEYPOINTS.get(keypoint, 1)   # middle by default
            g, _, err = _joints.build_joint_geometry(ent, edge_keypoint=kp_val)
            return g, f"edge ({_kp_name(kp_val)}) - Z runs along the edge", err
        if isinstance(ent, adsk.fusion.BRepVertex):
            g, _, err = _joints.build_joint_geometry(ent)
            return g, "vertex (position only)", err
        return None, None, "geometry handle is not a face/edge/vertex."

    if anchor == "coordinates":
        # Anchor on the component ORIGIN and carry the target as the JO's own offsetX/Y/Z
        # parameters: an undimensioned point in a hidden auto-sketch drifts on recompute. The frame
        # stays world-aligned, so the offsets map straight to X/Y/Z of THIS component's space.
        origin_pt = safe(lambda: comp.originConstructionPoint)
        if origin_pt is None:
            return None, None, "coordinates: the component has no origin construction point to anchor on."
        g = safe(lambda: JG.createByPoint(origin_pt))
        if meta is not None:
            meta["coordinate_offsets_cm"] = (x_cm, y_cm, z_cm)
            meta["anchor_cm"] = (x_cm, y_cm, z_cm)
        return g, ("model origin" if target == "origin" else "coordinates"), \
            (None if g else "coordinates: JointGeometry.createByPoint(origin) returned nothing.")

    if anchor in ("sketch_line", "sketch_point"):
        if not (sketch_name or "").strip():
            return None, None, f"anchor '{anchor}' needs 'sketch_name'."
        # Whole-design resolve, so a JO can anchor on a sketch drawn in a sub-component;
        # 'sketch_component' narrows a shared name. That scope is NOT the handler's 'component',
        # which names the occurrence RECEIVING the joint origin.
        sketch, ambiguous = _sketch_detail.scoped_sketch(
            design, sketch_name.strip(), sketch_component, "sketch_component")
        if ambiguous:
            return None, None, ambiguous
        if not sketch:
            return None, None, (f"No sketch named '{sketch_name}'. Use sketch_get to list "
    "them (draw a direction line first with sketch_add_3d_line).")
        idx = int(entity_index or 0)

    if anchor == "sketch_line":
        lines = safe(lambda: sketch.sketchCurves.sketchLines)
        n = safe(lambda: lines.count, 0)
        if n == 0:
            return None, None, f"Sketch '{sketch_name}' has no lines to anchor on."
        if idx < 0 or idx >= n:
            return None, None, f"line index {idx} out of range (sketch '{sketch_name}' has {n} line(s))."
        line = lines.item(idx)
        kp = _KEYPOINTS.get(keypoint, 0)  # default start (frame located at the line start)
        g = safe(lambda: JG.createByCurve(line, kp))
        return g, f"sketch '{sketch_name}' line[{idx}] ({_kp_name(kp)}) - Z runs along the line", \
            (None if g else "createByCurve returned nothing (check the keypoint for this curve).")

    if anchor == "sketch_point":
        pts = safe(lambda: sketch.sketchPoints)
        n = safe(lambda: pts.count, 0)
        if idx < 0 or idx >= n:
            return None, None, f"point index {idx} out of range (sketch '{sketch_name}' has {n} point(s))."
        g = safe(lambda: JG.createByPoint(pts.item(idx)))
        return g, f"sketch '{sketch_name}' point[{idx}]", \
            (None if g else "createByPoint returned nothing.")

    return None, None, f"Unknown anchor '{anchor}'."


def _kp_name(kp_value):
    for k, v in _KEYPOINTS.items():
        if v == kp_value:
            return k
    return str(kp_value)


def handler(anchor: str = "coordinates", target: str = "at", units: str = "mm",
            x: float = 0.0, y: float = 0.0, z: float = 0.0,
            sketch_name: str = "", entity_index: int = 0, keypoint: str = "start",
            geometry: str = "", name: str = "",
            bbox_target: str = "", orient_axis: str = "z", flip: bool = False,
            component: str = "", sketch_component: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")

    anchor = (anchor or "coordinates").strip().lower()
    if anchor not in _ANCHORS:
        return error(f"Unknown anchor '{anchor}'. Valid: {', '.join(_ANCHORS)}.")
    orient_axis = (orient_axis or "z").strip() or "z"

    target = (target or "at").strip().lower()
    if anchor == "coordinates" and target not in _TARGETS:
        return error(f"Unknown target '{target}'. Valid: {', '.join(_TARGETS)}.")

    kp = (keypoint or "start").strip().lower()
    if kp not in _KEYPOINTS:
        return error(f"Unknown keypoint '{keypoint}'. Valid: {', '.join(_KEYPOINTS)}.")

    scale = _common.scale(units)
    if scale is None:
        return error(f"Unknown units '{units}'. Valid: mm, cm, in.")

    if anchor == "coordinates" and target == "origin":
        x_cm = y_cm = z_cm = 0.0
    else:
        x_cm, y_cm, z_cm = x * scale, y * scale, z * scale

    # 'component' names the occurrence whose component RECEIVES the origin; omitted lands at root.
    # The component is the NATIVE one, so the anchor and its offsets are built in that component's
    # own space, the way the platform stores them.
    comp = design.rootComponent
    target_occ = None
    if (component or "").strip():
        target_occ, cerr = _COMPONENT.resolve(component)
        if cerr:
            return error(cerr)
        native = safe(lambda: target_occ.component)
        if native is None:
            return error(f"'component': occurrence '{safe(lambda: target_occ.fullPathName) or component}' "
                         "has no readable component to receive the joint origin.")
        comp = native
        # A WORLD-coordinate anchor agrees with the component's own space only while that component
        # sits at the world origin unrotated; an entity anchor carries no coordinates of ours.
        world_anchor = (anchor == "bbox_center"
                        or (anchor == "coordinates"
                            and any(abs(v) > 1e-12 for v in (x_cm, y_cm, z_cm))))
        if world_anchor:
            aligned = _sits_at_world_origin(target_occ)
            if aligned is not True:
                return error(_world_space_refusal(anchor, target_occ, aligned))

    meta = {}
    geom, desc, err = _geometry_from_args(
        design, comp, anchor, target, x_cm, y_cm, z_cm, sketch_name, entity_index, kp, geometry,
        bbox_target, orient_axis, flip, meta, sketch_component)
    if err:
        return error(err)
    if target_occ is not None and anchor == "coordinates":
        desc = "component origin" if target == "origin" else "coordinates in the component's space"
    if not geom:
        return error("Could not build joint geometry from the given anchor.")

    try:
        jo_input = comp.jointOrigins.createInput(geom)
    except Exception as e:
        return error(f"Could not create joint-origin input: {e}")
    if not jo_input:
        return error("createInput returned nothing for this geometry.")

    # anchor='coordinates': place the frame with the JO's own parametric offsets from the model origin
    # (createByReal is cm). NOT safe()-swallowed - a failure to place the frame where asked must surface,
    # not silently ship a mislocated origin. Read back off the created JO below to prove they took.
    coord_off = meta.get("coordinate_offsets_cm")
    if coord_off is not None:
        ox, oy, oz = coord_off
        try:
            jo_input.offsetX = adsk.core.ValueInput.createByReal(ox)
            jo_input.offsetY = adsk.core.ValueInput.createByReal(oy)
            jo_input.offsetZ = adsk.core.ValueInput.createByReal(oz)
        except Exception as e:
            return error(f"Could not set the coordinate offsets on the joint origin: {e}")

    # Resulting frame axes (Z primary, X secondary, Y third) - confirms orientation took.
    axes = {
    "primary_axis_Z": _vec(safe(lambda: jo_input.primaryAxisVector)),
    "secondary_axis_X": _vec(safe(lambda: jo_input.secondaryAxisVector)),
    "third_axis_Y": _vec(safe(lambda: jo_input.thirdAxisVector)),
    }

    try:
        joint_origin = comp.jointOrigins.add(jo_input)
    except Exception as e:
        return error(f"Joint origin creation failed: {e}")
    if not joint_origin:
        return error("jointOrigins.add returned nothing.")

    # Set-then-verify the LANDING: which component the origin actually belongs to is read back off the
    # created JO, never assumed from the collection it was added to. A JO on the wrong component cannot
    # serve as that component's side of a joint, so a mismatch is a failure - roll it back.
    landed_comp = safe(lambda: joint_origin.parentComponent)
    landed_name = safe(lambda: landed_comp.name) if landed_comp is not None else None
    want_name = safe(lambda: comp.name) or "?"
    # TRI-STATE. A PROVEN mismatch is rolled back; an unproven one is not, because deleting a
    # created origin on a comparison that was never made destroys work over an unreadable token.
    # It is disclosed instead - component_verified below is the read-back's own verdict.
    landed_here = (_common.same_component(landed_comp, comp) if landed_comp is not None else None)
    if landed_here is False:
        got_name = landed_name or "?"
        safe(lambda: joint_origin.deleteMe())
        return error(f"Joint origin landed on component '{got_name}', not the requested "
                     f"'{want_name}'. Rolled it back; nothing changed.")

    jo_name_final, rename_warning = apply_rename(joint_origin, name)

    # anchor='coordinates': prove the parametric offsets took by reading them BACK off the created
    # JO - a value that did not stick is a mislocated origin, not success.
    offset_params = None
    if coord_off is not None:
        ox, oy, oz = coord_off
        got = (safe(lambda: joint_origin.offsetX.value), safe(lambda: joint_origin.offsetY.value),
               safe(lambda: joint_origin.offsetZ.value))
        if None not in got:
            dist = ((got[0] - ox) ** 2 + (got[1] - oy) ** 2 + (got[2] - oz) ** 2) ** 0.5
            if dist > 1e-3:                        # > 0.001 cm: the offsets did not take
                safe(lambda: joint_origin.deleteMe())
                return error(
                    f"Coordinate offsets did not take: asked "
                    f"{[round(v, 4) for v in (ox, oy, oz)]} cm but the joint origin reports "
                    f"{[round(v, 4) for v in got]} cm. Rolled the origin back; nothing changed.")
            inv = (1.0 / scale) if scale else 1.0
            offset_params = {"x": round(got[0] * inv, 6), "y": round(got[1] * inv, 6),
                             "z": round(got[2] * inv, 6), "units": units}

    # Name the dNN model parameters holding the JO's offsetX/Y/Z (exemplar: model_extrude's
    # model_parameters block) so an agent can drive the frame with param_set '<dNN>' '<expression>'
    # - creation takes numeric offsets only. Read live off the created JO, never assumed.
    param_names = {}
    for key, getter in (("offset_x", lambda: joint_origin.offsetX.name),
                        ("offset_y", lambda: joint_origin.offsetY.name),
                        ("offset_z", lambda: joint_origin.offsetZ.name)):
        nm = safe(getter)
        if nm:
            param_names[key] = nm

    # For a COMPUTED anchor, read the created origin back and prove it landed on the point we computed.
    # A wrong landing is a failure, not a false success - roll the origin back and error.
    computed = None
    readback = None
    if anchor in ("bbox_center", "face_center") and meta.get("anchor_cm"):
        inv = (1.0 / scale) if scale else 1.0
        cx, cy, cz = meta["anchor_cm"]
        computed = {"x": round(cx * inv, 6), "y": round(cy * inv, 6), "z": round(cz * inv, 6),
                    "units": units, "source": meta.get("anchor_source", "")}
        actual = safe(lambda: joint_origin.geometry.origin)
        if actual is not None:
            ox = safe(lambda: actual.x)
            oy = safe(lambda: actual.y)
            oz = safe(lambda: actual.z)
            if None not in (ox, oy, oz):
                readback = {"x": round(ox * inv, 6), "y": round(oy * inv, 6),
                            "z": round(oz * inv, 6), "units": units}
                dist = ((ox - cx) ** 2 + (oy - cy) ** 2 + (oz - cz) ** 2) ** 0.5
                if dist > 1e-3:   # > 0.001 cm (0.01 mm): the origin did NOT land on the computed anchor
                    safe(lambda: joint_origin.deleteMe())
                    return error(
                        f"Joint origin landed at {readback} but the computed anchor was {computed} "
                        f"(off by {round(dist, 4)} cm). Rolled the origin back; nothing changed.")

    payload = {
    "created": True,
    "joint_origin_name": jo_name_final,
    "anchor": anchor,
    "anchored_on": desc,
    "frame_axes": axes,
    "component": landed_name or want_name,
    "component_verified": landed_here is True,
    "joint_origin_count": safe(lambda: comp.jointOrigins.count),
    "note": ("Joint origin created. frame_axes shows the resulting Z/X/Y directions. For an "
        "oriented frame: anchor='bbox_center' (Z = orient_axis) / 'face_center' (Z = face normal) "
        "/ a sketch line. anchor='coordinates' is world-aligned and PARAMETRIC - the location is "
        "held by offsetX/Y/Z parameters from the model origin (offset_parameters, read back). A "
        "computed anchor reports computed_anchor + origin_readback."),
    }
    if anchor == "coordinates":
        # The authoritative, read-back location (the verified offset parameters); falls back to the
        # requested values only if the read-back was unavailable.
        payload["location"] = offset_params or {"x": (0.0 if target == "origin" else x),
    "y": (0.0 if target == "origin" else y),
    "z": (0.0 if target == "origin" else z), "units": units}
        payload["held_by"] = ("parametric offsetX/Y/Z from the component's origin"
                              if target_occ is not None else
                              "parametric offsetX/Y/Z from the model origin")
        if offset_params is not None:
            payload["offset_parameters"] = offset_params
    if landed_here is not True:
        # Every un-proven landing is disclosed. An UNREADABLE parentComponent is the weaker read,
        # and its 'component' field falls back to the name the CALLER asked for - so each clause
        # names the read that actually failed.
        detail = ("was read but could not be matched against the requested component "
                  f"'{want_name}'" if landed_comp is not None else
                  f"did not read at all, so 'component' below repeats the requested '{want_name}' "
                  "rather than a landing that was confirmed")
        payload["note"] += (f" The origin's own parentComponent {detail}, so which component it "
                            "belongs to is UNVERIFIED - a joint origin on another component cannot "
                            "serve as this one's side of a joint. Confirm with "
                            "assembly_get(include=['joint_origins']).")
    if param_names:
        payload["model_parameters"] = param_names
        payload["note"] += (" model_parameters names the dNN offset params - param_set one to an "
                            "expression to drive this frame parametrically.")
    if target_occ is not None:
        payload["note"] += (f" This origin lives in component '{payload['component']}' - its offsets "
                            "are measured from THAT component's origin, so it can serve as that "
                            "component's side of a joint.")
    else:
        # Omitting 'component' lands the origin on the ROOT component even while another component is
        # the active edit target (measured) - unlike sketch/extrude, which build into the active one.
        # Disclosed only when the two actually differ, naming both components as read.
        active = _common.target_component(design)
        # `is False`: the note ASSERTS that another component is the active edit target, so it fires
        # only on a proven difference - an unproven pair says nothing rather than naming a component
        # it could not tell apart from the root.
        if active is not None and _common.same_component(active, design.rootComponent) is False:
            active_name = safe(lambda: active.name) or "?"
            payload["active_component"] = active_name
            payload["note"] += (f" Landed on the root component '{payload['component']}' while "
                                f"component '{active_name}' is the active edit target - this tool "
                                "does not follow the active component; pass 'component' to land the "
                                "origin inside one.")
    if computed is not None:
        payload["computed_anchor"] = computed
        if readback is not None:
            payload["origin_readback"] = readback
    if rename_warning:
        payload["rename_warning"] = rename_warning
    return ok(payload)


TOOL_DESCRIPTION = (
    "Create a Joint Origin, a reusable coordinate frame; feed it to joint_create or "
    "joint_at_geometry by name."
)

tool = (
    Tool.create_simple(name="joint_create_origin", description=TOOL_DESCRIPTION)
    .add_input_property(*_ANCHOR_CHOICE.as_property())
    .add_input_property("geometry", _GEOM.schema())
    .add_input_property(*_TARGET_CHOICE.as_property())
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("x", {"type": "number"})
    .add_input_property("y", {"type": "number"})
    .add_input_property("z", {"type": "number"})
    .add_input_property("sketch_name", {"type": "string"})
    .add_input_property(*_sketch_detail.component_scope("sketch_component",
                                                        narrows="sketch_name"))
    .add_input_property("entity_index", {"type": "integer"})
    .add_input_property(*_KEYPOINT_CHOICE.as_property())
    .add_input_property(*_BBOX_TARGET.as_property())
    .add_input_property(*_ORIENT_AXIS.as_property())
    .add_input_property("flip", {"type": "boolean"})
    .add_input_property("name", {"type": "string"})
    .add_input_property(*_COMPONENT.as_property())
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_joint_create_origin.py::TestBboxCenterHandler"
                      "::test_wrong_landing_point_errors_and_rolls_back"))


def register_tool():
    register(item)
