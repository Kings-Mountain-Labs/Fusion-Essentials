# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: round the edges of a body - constant radius, variable radius along a tangent
chain, chord length, or a rule fillet over the edges of whole faces. WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, scale, target_component
from . import _common
from . import _geom
from . import _inputs
from . import _assert
from ._edge_common import _BODY, _EDGES, _EDGE_FILTER_DESC, _FACES, _apply, _size_hint

app = adsk.core.Application.get()

_FILLET_TYPE = _inputs.Choice("fillet_type", ["constant", "variable", "chord_length", "rule"],
                              default="constant")
_TOPOLOGY = _inputs.Choice("topology", ["rounds_and_fillets", "rounds_only", "fillets_only"],
                           default="rounds_and_fillets")
_RULE_FACES_TWO = _inputs.GeometryHandleList("second_faces", require="face", required=False)


def _variable_radius_spec(end_radius, positions, radii):
    """(spec, error) for a variable-radius edge set: the far-end radius plus the parallel
    positions/radii arrays that place any intermediate radii along the chain."""
    if end_radius in (None, ""):
        return None, ("A variable-radius fillet needs 'end_radius' - the radius at the far end of "
                      "the edge chain, where 'radius' is the radius at the start.")
    try:
        end = float(end_radius)
    except (TypeError, ValueError):
        return None, f"'end_radius' must be a number, got {end_radius!r}."
    if end <= 0:
        return None, f"'end_radius' must be positive, got {end}."
    raw_pos = list(positions) if positions not in (None, "") else []
    raw_rad = list(radii) if radii not in (None, "") else []
    if len(raw_pos) != len(raw_rad):
        return None, (f"'positions' and 'radii' must be the same length - got {len(raw_pos)} "
                      f"position(s) and {len(raw_rad)} radius(es). Each position places the radius "
                      "paired with it at the same index.")
    pos, rad = [], []
    for i, p in enumerate(raw_pos):
        try:
            v = float(p)
        except (TypeError, ValueError):
            return None, f"'positions'[{i}] must be a number, got {p!r}."
        # The interval is OPEN: add() raises "position value must be greater than 0 and less than 1"
        # for an endpoint. The two ends already carry 'radius' and 'end_radius'.
        if not 0.0 < v < 1.0:
            return None, (f"'positions'[{i}] is {v} - a position is the fraction along the edge "
                          "chain where its radius applies, and must be between 0 and 1 exclusive. "
                          "The chain's two ends take their radii from 'radius' and 'end_radius'.")
        pos.append(v)
    for i, r in enumerate(raw_rad):
        try:
            v = float(r)
        except (TypeError, ValueError):
            return None, f"'radii'[{i}] must be a number, got {r!r}."
        if v <= 0:
            return None, f"'radii'[{i}] must be positive, got {v}."
        rad.append(v)
    return {"type": "variable", "end_radius": end, "positions": pos, "radii": rad}, None


def _topology_type(key):
    t = adsk.fusion.RuleFilletTopologyTypes
    return {"rounds_and_fillets": t.RoundsAndFilletsRuleFilletTopologyType,
            "rounds_only": t.RoundsOnlyRuleFilletTopologyType,
            "fillets_only": t.FilletsOnlyRuleFilletTopologyType}[key]


def _rule_fillet(radius, units, faces, second_faces, topology):
    """Round every edge of the given faces (a Rule Fillet), or only the edges between two face
    sets. The selection is the FACES, so no per-edge handle list is involved."""
    k = scale(units)
    if k is None:
        return error(f"Unknown units '{units}'. Use mm, cm, or in.")
    # The same two forms every fillet radius takes. Only a LITERAL can be guarded here, before the
    # design an expression resolves against is in hand; an expression is guarded - and its applied
    # radius compared - on the value the units engine evaluates it to, below.
    as_expression = _inputs.looks_like_expression(radius)
    r = None
    if not as_expression:
        try:
            r = float(radius)
        except Exception:
            return error("'radius' must be a number or a parameter-expression string like "
                         "'WallT/2'.")
        if r <= 0:
            return error("Provide a positive radius.")
    topo, terr = _TOPOLOGY.resolve(topology)
    if terr:
        return error(terr)
    if faces in (None, "", []):
        return error("A rule fillet needs 'faces' - find_geometry face handles. Every edge of those "
                     "faces is rounded; add 'second_faces' to round only the edges between the two "
                     "sets.")
    first, ferr = _FACES.resolve(faces)
    if ferr:
        return error(ferr)
    second = None
    if second_faces not in (None, "", []):
        second, serr = _RULE_FACES_TWO.resolve(second_faces)
        if serr:
            return error(serr)

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    verify_bodies = _geom.owning_bodies(list(first) + list(second or []))
    vol_before = _geom.volumes(verify_bodies)
    rv, want_cm, rverr = _inputs.length_value_input(radius, k, design, "radius")
    if rverr:
        return error(rverr)
    if as_expression and want_cm is not None and want_cm <= 0:
        return error(f"Provide a positive radius: the expression '{str(radius).strip()}' evaluates "
                     f"to {round(want_cm / k, 6)} {units}.")
    try:
        ri = comp.features.filletFeatures.createRuleFilletInput()
        # The face sets cross as plain Python lists, not an ObjectCollection.
        applied = (ri.setByBetweenFacesOrFeatures(list(first), list(second)) if second
                   else ri.setByAllEdges(list(first)))
        if applied is False:
            return error("The rule fillet refused the given faces, so nothing was created. Re-run "
                         "find_geometry for fresh face handles.")
        ri.radius = rv
        ri.topologyType = _topology_type(topo)
        feature = comp.features.filletFeatures.addRuleFillet(ri)
    except Exception as e:
        return error(f"Rule fillet failed: {e}.{_size_hint(str(e), 'radius')}")
    if not feature:
        return error(_common.no_feature_error(design, "Rule fillet"))

    faces_created = safe(lambda: feature.faces.count)
    vol_delta, vol_readable = _geom.volume_delta(verify_bodies, vol_before)
    moved = abs(vol_delta) >= _common.NO_VOLUME_CHANGE_CM3 if vol_readable else faces_created != 0
    if not moved:
        removed = safe(lambda: feature.deleteMe())
        return error(
            f"Rule fillet reported success but rounded nothing. topology '{topo}' may exclude every "
            "edge of the selected faces ('rounds_only' takes convex edges, 'fillets_only' concave "
            "ones), or the faces meet smoothly and have no corner to round. The feature has been "
            "rolled back."
            + ("" if removed else " (The inert fillet feature could not be auto-removed.)"))

    # ruleFilletSettings carries the applied radius (a ModelParameter, in cm) and topology, so
    # both are read off the feature rather than echoed: a setter the API silently ignored would
    # otherwise be reported as if it had landed.
    settings = safe(lambda: feature.ruleFilletSettings)
    got_r_cm = safe(lambda: settings.radius.value) if settings is not None else None
    got_topo = safe(lambda: settings.topologyType) if settings is not None else None
    want_topo = safe(lambda: _topology_type(topo))
    # Both forms are compared: a literal against its own scaled number, an expression against the
    # value the units engine evaluated it to. want_cm None means nothing answered a number, and
    # treating that as zero would roll a healthy fillet out.
    if want_cm is not None and isinstance(got_r_cm, float) and abs(got_r_cm - want_cm) > 1e-6:
        asked = f"{round(want_cm / k, 6)} {units}"
        if as_expression:
            asked += f" (what the expression '{str(radius).strip()}' evaluates to)"
        return error(f"The rule fillet was created but its radius reads back "
                     f"{round(got_r_cm / k, 6)} {units}, not the requested {asked}. Remove "
                     f"'{safe(lambda: feature.name)}' with design_delete_feature.")
    if got_topo is not None and want_topo is not None and got_topo != want_topo:
        return error(f"The rule fillet was created but its topology is not the requested "
                     f"'{topo}'. Remove '{safe(lambda: feature.name)}' with design_delete_feature.")

    payload = {
        "filleted": True,
        "feature": safe(lambda: feature.name),
        "fillet_type": "rule",
        "rule": "between_faces" if second else "all_edges",
        "radius": (round(got_r_cm / k, 6) if isinstance(got_r_cm, float)
                   else _inputs.expression_report(radius)),
        "units": units,
        "topology": topo,
        "faces_selected": len(first) + (len(second) if second else 0),
        "note": "Rule fillet created - the rounded edge set is defined by the selected FACES, not "
                "by individual edge handles. Pair with view_screenshot.",
    }
    if as_expression:
        # 'radius' is the value the FEATURE reports; without this the caller cannot tell that value
        # came from a parameter rather than from a number they fixed.
        payload["radius_expression"] = str(radius).strip()
    if faces_created is not None:
        payload["faces_created"] = faces_created
    if vol_readable:
        payload["volume_delta_cm3"] = round(vol_delta, 6)
    return ok(payload)


def handler(body_name: str = "", radius: float = 1.0, units: str = "mm",
            edge_filter: str = "", edges=None, fillet_type: str = "constant",
            end_radius=None, positions=None, radii=None, chord_length=None,
            faces=None, second_faces=None, topology: str = "rounds_and_fillets") -> dict:
    """Round edges (Fillet) - constant radius, variable radius along a tangent chain, chord length,
    or a rule fillet over the edges of whole faces."""
    ftype, terr = _FILLET_TYPE.resolve(fillet_type)
    if terr:
        return error(terr)
    if ftype == "rule":
        if edges not in (None, "", []):
            return error("A rule fillet selects FACES, so 'edges' cannot be passed with "
                         "fillet_type='rule'. Drop 'edges', or use fillet_type='constant' to round "
                         "exactly those edge handles.")
        return _rule_fillet(radius, units, faces, second_faces, topology)

    variant, size = None, radius
    if ftype == "variable":
        if faces not in (None, "", []):
            return error("A variable-radius fillet cannot take 'faces': its radius runs from the "
                         "start of the edge chain to the far end, and a face set carries no such "
                         "order. Pass 'edges' handles in chain order, or fillet_type='rule' to "
                         "round every edge of those faces at one radius.")
        if edges in (None, "", []):
            return error("A variable-radius fillet needs 'edges' - find_geometry edge handles for a "
                         "single edge, or a tangentially connected chain listed in order from its "
                         "start end. An edge_filter sweep has no such order, so it cannot carry a "
                         "start-to-end radius.")
        variant, verr = _variable_radius_spec(end_radius, positions, radii)
        if verr:
            return error(verr)
    elif ftype == "chord_length":
        if chord_length in (None, ""):
            return error("A chord-length fillet needs 'chord_length' - the straight-line distance "
                         "across the rounded corner. 'radius' does not drive this type.")
        variant, size = {"type": "chord_length"}, chord_length
    return _apply("fillet", body_name, size, units, edge_filter, edges, variant=variant,
                  face_handles=faces)


TOOL_DESCRIPTION = (
    "Round (fillet) edges; model_chamfer bevels instead."
)

tool = (
    Tool.create_simple(name="model_fillet", description=TOOL_DESCRIPTION)
    .add_input_property("edges", _EDGES.schema())
    .add_input_property("body_name", _BODY.schema())
    .add_input_property("radius", {"type": ["number", "string"],
        "description": "In 'units', or a parameter expression ('WallT/2')."})
    .add_input_property(*_inputs.UNITS.as_property())
    .add_input_property("edge_filter", {"type": "string", "enum": ["all", "convex", "concave"],
        "description": _EDGE_FILTER_DESC})
    .add_input_property(*_FILLET_TYPE.as_property())
    .add_input_property("end_radius", {"type": "number"})
    .add_input_property("positions", {"type": "array", "items": {"type": "number"}})
    .add_input_property("radii", {"type": "array", "items": {"type": "number"}})
    .add_input_property("chord_length", {"type": "number"})
    .add_input_property(*_FACES.as_property())
    .add_input_property(*_RULE_FACES_TWO.as_property(brief=True))
    .add_input_property(*_TOPOLOGY.as_property())
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy()],
                             verification=Verification(
                                 kind="inline", rung="geometry",
                                 evidence_test="tests/unit/test_model_fillet.py::TestVolumeReadBack"
                                               "::test_unchanged_volume_errors_and_rolls_back"))


def register_tool():
    register(item)
