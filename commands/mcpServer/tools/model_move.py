# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: reposition bodies with a Move feature recorded in the timeline.

  model_move -> translate, move along an entity, rotate, or point-to-point a body as a parametric
                timeline feature. WRITES.
"""

import math

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _assert
from . import _geom
from . import _inputs
from . import _outputs


# Sample points closer together than this (cm) are the same position.
_MOVE_EPS_CM = 1e-7

# How far a measured displacement may miss the requested one (cm) before the move is called wrong.
_MOVE_TOL_CM = 1e-4

# Vertices sampled per body alongside its bounding box: a rotation that maps a symmetric body's
# bounding box onto itself still carries that body's vertices to new positions.
_VERTEX_SAMPLE = 4

_TRANSLATE = "translate"
_ALONG = "along_entity"
_ROTATE = "rotate"
_POINT_TO_POINT = "point_to_point"

RETURNS = [
    _outputs.ReturnsName("feature", of="feature", consumers=["design_delete_feature"],
                         absent_when="no_timeline_feature"),
    _outputs.ReturnsValue("displacement", "the measured geometry displacement proving the move took"),
]

_MODE = _inputs.Choice("mode", (_TRANSLATE, _ALONG, _ROTATE, _POINT_TO_POINT), default=_TRANSLATE)
_BODIES = _inputs.BodyRefList("bodies", kind="brep")
_FACES = _inputs.GeometryHandleList("faces", require="face")
_DX = _inputs.Distance("dx", allow_zero=True)
_DY = _inputs.Distance("dy", allow_zero=True)
_DZ = _inputs.Distance("dz", allow_zero=True)
_DISTANCE = _inputs.Distance("distance", allow_zero=False, required=True)
_AXIS = _inputs.AxisRef("axis", entity_only=True)
_FROM = _inputs.GeometryHandle("from_point", require="vertex")
_TO = _inputs.GeometryHandle("to_point", require="vertex")

app = adsk.core.Application.get()


def _xyz(point):
    """(x, y, z) in cm for a Point3D-like sample, or None when a coordinate is unreadable."""
    if point is None:
        return None
    coords = [safe(lambda a=a: float(getattr(point, a))) for a in ("x", "y", "z")]
    return tuple(coords) if all(isinstance(c, float) for c in coords) else None


def _body_points(body):
    """The world samples a moved BODY has to displace: its bounding-box corners plus its first
    vertices."""
    pts = []
    box = _geom.body_aabb(body)
    if box is not None:
        for corner in ("minPoint", "maxPoint"):
            pt = _xyz(safe(lambda c=corner: getattr(box, c)))
            if pt:
                pts.append(pt)
    for i, v in enumerate(_common.iter_collection(safe(lambda: body.vertices))):
        if i >= _VERTEX_SAMPLE:
            break
        pt = _xyz(safe(lambda v=v: v.geometry))
        if pt:
            pts.append(pt)
    return tuple(pts) if pts else None


def _displacement(before, after):
    """The largest distance (cm) any sample point travelled, or None when the two samples cannot be
    compared."""
    if not before or not after or len(before) != len(after):
        return None
    return max(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5
               for b, a in zip(before, after))


_HINTS = {
    _TRANSLATE: "Check dx/dy/dz against the design's extents.",
    _ALONG: "Check that 'axis' is a world axis (x/y/z), a straight edge or a sketch line, and "
            "that 'distance' suits it.",
    _ROTATE: "Check that 'axis' is a world axis (x/y/z), a straight edge or a sketch line the "
             "bodies can turn about.",
    _POINT_TO_POINT: "Check that 'from_point' and 'to_point' are vertices on reachable geometry.",
}


def _expected_cm(mode_key, offsets_cm, dist_cm, start_pt, end_pt):
    """How far the geometry MUST travel, when the mode fixes it up front - None for a rotation,
    whose sample points each travel their own distance."""
    if mode_key == _TRANSLATE:
        return (offsets_cm[0] ** 2 + offsets_cm[1] ** 2 + offsets_cm[2] ** 2) ** 0.5
    if mode_key == _ALONG:
        return abs(dist_cm)
    if mode_key == _POINT_TO_POINT:
        a, b = _xyz(safe(lambda: start_pt.geometry)), _xyz(safe(lambda: end_pt.geometry))
        if a is None or b is None:
            return None
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5
    return None


def _verify(before, after, remedy, expected_cm=None, scale_factor=1.0, units="cm"):
    """(error_text, largest_displacement_cm) proving the move displaced the geometry, by exactly
    expected_cm when the mode fixes the distance up front (None for a rotation)."""
    dists = [d for d in (_displacement(b, a) for b, a in zip(before, after)) if d is not None]
    if not dists:
        return ("Move reported success but no moved geometry could be read back, so the result "
                f"could not be verified. Re-read it with model_inspect. {remedy}"), None
    biggest = max(dists)
    if biggest <= _MOVE_EPS_CM:
        return ("Move reported success but the geometry sits exactly where it was - nothing was "
                f"displaced. {remedy}"), None
    if expected_cm is not None and abs(biggest - expected_cm) > _MOVE_TOL_CM:
        return (f"Move displaced the geometry by {round(biggest / scale_factor, 6)} {units}, not the "
                f"{round(expected_cm / scale_factor, 6)} {units} requested - the move did not land "
                f"where it was asked to. {remedy}"), None
    return "", biggest


def _axis_entity(raw, comp, design, context=None):
    """(linear entity, error) for the axis a move takes its direction from - a world axis resolves
    to the component's origin ConstructionAxis, a handle to the edge or sketch line itself."""
    tagged, aerr = _AXIS.resolve(raw)
    if aerr:
        return None, aerr
    if tagged[0] == "edge":
        ent = tagged[1]
        occ, cerr = _inputs.single_placement("'axis': that entity", ent, comp, design)
        if cerr:
            return None, cerr
        if occ is None:
            return ent, None
        proxy = safe(lambda: ent.createForAssemblyContext(occ))
        if proxy is None:
            path = safe(lambda: occ.fullPathName) or "its one occurrence"
            return None, (f"'axis': that entity could not be brought into the move's assembly "
                          f"context ({path}). Pass a handle at geometry in the moved body's own "
                          "component, or a world axis (x/y/z).")
        return proxy, None
    ent = _inputs.world_construction_axis(comp, raw)
    if ent is None:
        return None, (f"'axis': the active component has no {raw} origin construction axis to move "
                      "along. Pass a find_geometry handle at a straight edge or sketch line.")
    # An origin construction axis is refused ("3 : Invalid entity") at defineAs unless it is
    # proxied into the occurrence the moved body belongs to.
    if context is not None:
        return _inputs._proxy_or_refuse(
            f"'axis': the {raw} origin construction axis", ent, context,
            "Pass a find_geometry handle at a straight edge or sketch line in the moved body's "
            "own component.")
    return ent, None


def _host_for(design, body):
    """(component that must host the move feature, occurrence the axis is proxied into, error) for
    `body`."""
    # A sub-component body needs both: hosting on the active component raises "object is not in the
    # assembly context of this component" at add(), and a native axis on the owning component
    # raises "3 : Invalid entity" at defineAs.
    root = safe(lambda: design.rootComponent)
    owner = safe(lambda: body.parentComponent) or root
    # same_component, not `is`: component wrappers are not identity-stable, so `owner is root` reads
    # False even for a root body. `is True` only - same_component answers neither when it cannot
    # tell, and an unproven owner takes the placement path rather than hosting on root.
    if owner is None or _common.same_component(owner, root) is True:
        return root, None, None
    ctx = safe(lambda: body.assemblyContext)
    if ctx is not None:
        return owner, ctx, None
    occ, err = _inputs.single_placement("'bodies': that body", body, root, design)
    if err:
        return None, None, err
    return owner, occ, None


def _translation_cm(dx, dy, dz, scale_factor):
    """([x, y, z] in cm, error) for a translate move's offsets."""
    out = []
    for kind, raw in ((_DX, dx), (_DY, dy), (_DZ, dz)):
        cm, derr = kind.resolve_scaled(raw if raw is not None else 0.0, scale_factor)
        if derr:
            return None, derr
        out.append(cm)
    if not any(out):
        return None, ("mode 'translate' needs a non-zero 'dx', 'dy' or 'dz' - all three offsets are "
                      "zero, so nothing would move.")
    return out, None


def handler(mode: str = "translate", bodies=None, faces=None, dx=None, dy=None, dz=None,
            distance=None, axis: str = "", angle_deg=None, from_point: str = "",
            to_point: str = "", units: str = "mm") -> dict:
    """See TOOL_DESCRIPTION."""
    mode_key, merr = _MODE.resolve(mode)
    if merr:
        return error(merr)
    scale_factor, uerr = _inputs.UNITS.resolve(units)
    if uerr:
        return error(uerr)

    has_bodies = bodies not in (None, "", [])
    has_faces = faces not in (None, "", [])
    if has_faces:
        return error("A move feature cannot move FACES - pass 'bodies'. createInput2 accepts a "
                     "BRepFace collection and then raises InternalValidationError inside the "
                     "kernel. To push or pull a face, use model_offset_face.")
    if not has_bodies:
        return error("'bodies' is required - the bodies to move.")

    offsets_cm, dist_cm, angle_rad = None, None, None
    if mode_key == _TRANSLATE:
        offsets_cm, terr = _translation_cm(dx, dy, dz, scale_factor)
        if terr:
            return error(terr)
    elif mode_key == _ALONG:
        if not axis:
            return error("mode 'along_entity' needs 'axis' - the linear entity the move runs along.")
        dist_cm, derr = _DISTANCE.resolve_scaled(distance, scale_factor)
        if derr:
            return error(derr)
    elif mode_key == _ROTATE:
        if not axis:
            return error("mode 'rotate' needs 'axis' - the linear entity to rotate about.")
        if angle_deg is None or angle_deg == "":
            return error("mode 'rotate' needs 'angle_deg' - the rotation in degrees.")
        try:
            degrees = float(angle_deg)
        except (TypeError, ValueError):
            return error(f"'angle_deg' must be a number (rotation in degrees), got {angle_deg!r}.")
        if degrees == 0:
            return error("'angle_deg' must be non-zero - a 0 deg rotation moves nothing.")
        angle_rad = math.radians(degrees)
    else:
        missing = [n for n, v in (("from_point", from_point), ("to_point", to_point)) if not v]
        if missing:
            return error(f"mode 'point_to_point' needs {' and '.join(missing)} - a vertex handle "
                         "from find_geometry.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    ents, eerr = _BODIES.resolve(bodies)
    if eerr:
        return error(eerr)
    comp, axis_ctx, herr = _host_for(design, ents[0])
    if herr:
        return error(herr)

    axis_ent, start_pt, end_pt = None, None, None
    if mode_key in (_ALONG, _ROTATE):
        axis_ent, aerr = _axis_entity(axis, comp, design, axis_ctx)
        if aerr:
            return error(aerr)
    elif mode_key == _POINT_TO_POINT:
        start_pt, serr = _FROM.resolve(from_point)
        if serr:
            return error(serr)
        end_pt, terr = _TO.resolve(to_point)
        if terr:
            return error(terr)

    coll = adsk.core.ObjectCollection.create()
    for e in ents:
        coll.add(e)
    before = [_body_points(e) for e in ents]
    # A post-mutation proxy can stop answering .name, so the names are read here.
    body_names = [safe(lambda e=e: e.name) for e in ents]
    # A point_to_point from_point rides the body being moved: after add() the two vertices have
    # closed on each other and their separation no longer describes the travel asked for.
    expected_cm = _expected_cm(mode_key, offsets_cm, dist_cm, start_pt, end_pt)

    try:
        move_input = comp.features.moveFeatures.createInput2(coll)
        if mode_key == _TRANSLATE:
            # A ValueInput built from a real carries CENTIMETRES. isDesignSpace true measures the
            # offsets in root-component space rather than the active component's.
            defined = move_input.defineAsTranslateXYZ(
                adsk.core.ValueInput.createByReal(offsets_cm[0]),
                adsk.core.ValueInput.createByReal(offsets_cm[1]),
                adsk.core.ValueInput.createByReal(offsets_cm[2]), True)
        elif mode_key == _ALONG:
            # The entity supplies the direction; the distance carries the magnitude.
            defined = move_input.defineAsTranslateAlongEntity(
                axis_ent, adsk.core.ValueInput.createByReal(dist_cm))
        elif mode_key == _ROTATE:
            # A ValueInput built from a real carries RADIANS for an angle. The axis entity's natural
            # direction sets the right-hand-rule sense of a positive rotation.
            defined = move_input.defineAsRotate(
                axis_ent, adsk.core.ValueInput.createByReal(angle_rad))
        else:
            defined = move_input.defineAsPointToPoint(start_pt, end_pt)
        if defined is False:
            return error(f"Fusion refused the '{mode_key}' move definition, so nothing was moved. "
                         f"{_HINTS[mode_key]}")
        feature = comp.features.moveFeatures.add(move_input)
    except Exception as e:
        return error(f"Move failed: {e} {_HINTS[mode_key]}")
    # moveFeatures.add returns None in a DIRECT design while the move lands; the before/after
    # read-back below needs no feature object. In parametric a None feature stays an error.
    direct_no_feature = _common.direct_feature_absence(design, feature)
    if not feature and not direct_no_feature:
        return error(_common.no_feature_error(design, "Move"))

    # A feature can be ADDED yet fail to compute.
    if safe(lambda: feature.healthState) == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:
        msg = safe(lambda: feature.errorOrWarningMessage) or "no detail"
        return error(f"Move feature was created but failed to compute: {msg}. Try a smaller move, "
                     "or a different axis/point selection. "
                     + _common.failed_effect_remedy(design, feature))

    after = [_body_points(e) for e in ents]
    verr, moved_cm = _verify(before, after, _common.failed_effect_remedy(design, feature),
                             expected_cm, scale_factor, units)
    if verr:
        return error(verr)

    payload = {
        "moved": True,
        "mode": mode_key,
        "displacement": round(moved_cm / scale_factor, 6),
        "units": units,
        "note": ("Geometry repositioned. To reposition a component instance instead, use "
                 "assembly_move."),
    }
    if direct_no_feature:
        payload["no_timeline_feature"] = True
        payload["note"] += " " + _common.DIRECT_FEATURE_NOTE
    else:
        payload["feature"] = safe(lambda: feature.name)
        payload["note"] += (" The move is a feature in the timeline, so it replays on every "
                            "recompute.")
    payload["bodies"] = body_names
    return ok(payload)


TOOL_DESCRIPTION = (
    "Move bodies as a timeline feature; assembly_move moves an occurrence.\n"
    + _outputs.produces_block(RETURNS)
)

move_tool = (
    Tool.create_simple(name="model_move", description=TOOL_DESCRIPTION)
    .add_input_property(*_MODE.as_property())
    .add_input_property(*_BODIES.as_property())
    .add_input_property(*_FACES.as_property())
    .add_input_property(*_DX.as_property())
    .add_input_property(*_DY.as_property())
    .add_input_property(*_DZ.as_property())
    .add_input_property(*_DISTANCE.as_property())
    .add_input_property(*_AXIS.as_property())
    .add_input_property("angle_deg", {"type": "number"})
    .add_input_property(*_FROM.as_property())
    .add_input_property(*_TO.as_property(brief=True))
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
move_item = Item.create_tool_item(tool=move_tool, write="write", handler=handler,
                                  run_on_main_thread=True,
                                  postconditions=[_assert.FeatureHealthy()],
                                  verification=Verification(
                                      kind="inline", rung="geometry",
                                      evidence_test="tests/unit/test_model_move.py"
                                      "::TestExpectedMagnitude"
                                      "::test_a_sub_epsilon_shift_is_still_nothing_moved"))


def register_tool():
    register(move_item)
