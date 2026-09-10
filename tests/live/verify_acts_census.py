# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""ACT rows: the strategy census - the rest of the hub's vocabulary, driven and proved cutting.

The other CAM acts drive the strategies a job needs. These drive what is LEFT of the two hub setups'
vocabularies, grouped by the geometry kind each family takes, and prove every one CUTS through its
own machining time - hasToolpath reads true on an empty toolpath. A strategy the hub cannot feed is
not created: it is a CENSUS row carrying the message the platform answered with, which is what
tests/generated/STRATEGY_COMPETENCE.md publishes beside the proven ones.
"""

from verify_acts_cam import (
    _all_cut, _launched_on, _offers, _op_deleted, _op_named, _reveal, _selected)
from verify_acts_hub import (
    HUB_COMP, HUB_MILL_SETUP, HUB_TURN_SETUP, _BALL_AT, _CHAMFER_AT, _CHAMFER_R, _FLAT6_AT,
    _FLAT_AT, _FLANGE_R, _FLANGE_T, _GROOVE_AT, _HUB_X, _PART_END, _SLOT_AT, _setup_created,
    _THREAD_INSERT_AT, _TURN_AT)
from verify_core import (
    _RECALL, _ctx_get, _fg, _measured, _near, _num, _recall, _refused, _watch)

# The three cutters the hub's shop set does not carry: the cutting family refuses a mill by TYPE,
# the hub's 12 mm thread mill in an 11 mm counterbore says "Tool doesn't fit.", and a probing
# strategy takes no tool but a probe. Added in ONE call, before anything generates.
_CENSUS_TOOLS = (("waterjet", None), ("thread mill", 6.0), ("probe", None))
_WATERJET_AT, _THREAD_MILL_AT, _PROBE_AT = 0, 1, 2

# The pocket-clearing beat's own setup and operation - see _CENSUS_POCKET for why it needs one.
HUB_POCKET_SETUP = "PocketClearFirst"
_POCKET_OP = "PocketClear"

# THE GEOMETRY THE CENSUS AIMS AT, in the hub's own numbers. The flange rim carries a 1.5 mm round,
# so the circle at the flange TOP is that fillet's upper tangent - r 38.5, not the flange's own 40.
_RIM_R, _RIM_TOL = 38.5, 0.2
_CBORE_R = 5.5                   # the counterbored bolt holes' 11 mm walls
_POCKET_Z = -4.0                 # the flange pocket floor, 4 mm down from the flange top
_POCKET_XY = 21.0                # that arc slot's own centroid, on the 32 mm bolt circle
_GROOVE_R, _GROOVE_TOP_Z = 22.0, -45.0   # the shaft groove the single-groove cycle drops into
_SHAFT_R, _SHAFT_MID_Z = 25.0, -32.5     # the shaft wall the turned thread runs on
# The flange's outer wall, which the swarf family cuts with the side of the cutter. Its centroid
# sits at half its height: the wall runs from the rim round's lower tangent down to the underside.
_WALL_R, _WALL_Z = 40.0, -10.75
# THE INCLINED FLAT on the shaft's plain stretch, which three_plus_two tilts its axis onto. Its
# centroid is where the 30 deg datum's own outward direction leaves the hub axis, MEASURED there;
# the AREA is what says the query found the flat and not a wall beside it.
_FLAT_AREA, _FLAT_TOL = 416.9, 1.0
_FLAT_NEAR = [_HUB_X + 18.05, 10.42, -34.5]
# The pair of rim circles morph is driven by - the flange's top tangent circle and its underside
# rim, which bound the flange wall between them.
_LOWER_RIM_Z = -_FLANGE_T
# The chamfer at the part end, whose bounding circle the turning chamfer cycle is aimed at.
_CHAMFER_EDGE_Z = -_PART_END

# The whole-model 3D families: MEASURED, each generates a NON-EMPTY toolpath over the setup's own
# model with no geometry selection at all, so one create is the whole beat. What decides membership
# is GENERATION wall clock, not how long the toolpath cuts.
_WHOLE_MODEL = (
    ("parallel", "Parallel", _BALL_AT), ("scallop", "Scallop", _BALL_AT),
    ("pencil", "Pencil", _BALL_AT), ("contour3d", "Contour3D", _BALL_AT),
    ("radial", "Radial", _BALL_AT), ("spiral", "Spiral", _BALL_AT),
    ("morphed_spiral", "MorphedSpiral", _BALL_AT),
    ("flat", "Flat", _FLAT_AT), ("horizontal", "Horizontal", _FLAT_AT),
)

# The whole-model family in an act of its OWN because its generation eats most of a default boundary
# poll's budget: (strategy, operation name, tool offset, the machining seconds it cut on the hub).
# Its act carries a poll budget sized to it - see POLL_AFTER in verify_program.
_LONG_FAMILIES = (
    ("ramp", "Ramp", _BALL_AT, 2163.8),
)
_LONG_NAMES = [n for _s, n, _i, _secs in _LONG_FAMILIES]

# The turned cycles that take no selection either, each with the insert kind it was measured to
# take: adaptive roughing is on the GROOVING insert, since the general one errors "Tool (turning
# general) is not supported for the strategy."
_TURNED = (
    ("turning_adaptive_roughing", "TurnAdaptive", _GROOVE_AT),
    ("turning_profile", "TurnProfile", _TURN_AT),
    ("turning_groove_roughing", "TurnGrooveRough", _GROOVE_AT),
    ("turning_groove_finishing", "TurnGrooveFinish", _GROOVE_AT),
    ("turning_profile_groove", "TurnProfileGroove", _GROOVE_AT),
)

# The neighbour pair the competence table's nuance section is drawn from, as
# (op, strategy, its own 'strategy' PARAMETER id, the parameters it carries and its neighbour does
# not) x2. The parameter id reads a DIFFERENT vocabulary from the strategies list - ledger
# STRATEGY-VOCAB-1 - which is why the table publishes both. Every pair here is one a beat below
# CREATES, so the sweep asserts the published diff rather than the table asserting itself.
NUANCE = (
    ("TurnGrooveRough", "turning_groove_roughing", "'turningGrooveRoughing'",
     ("maximumGrooveStepdown", "usePecking"),
     "TurnGrooveFinish", "turning_groove_finishing", "'turningGrooveFinishing'",
     ("doLeadIn", "nullPass")),
)

# The neighbour pairs measured BY HAND with cam_compare_operations on the CAM overview samples, as
# (first, second, what separates them). No beat creates these operations, so the table publishes
# them under the census's own word for a reading with no receipt row behind it - measured - and
# 'd' in a value is the cutter diameter.
MEASURED_NUANCE = (
    ("2D contour", "the same contour with multiple passes",
     "doRoughingPasses, and maximumRoughingSteps 1 -> 6"),
    ("2D contour", "its Trimmed sibling", "useStockContours, and 15 steps"),
    ("2D contour", "its Rest machining sibling",
     "useRestMachining and restMaterialCutterDiameter"),
    ("parallel", "steep areas", "machineSteepAreas alone"),
    ("contour3d", "shallow areas", "machineShallowAreas alone"),
    ("parallel", "scallop",
     "31 differences: stepover 0.5d vs 0.1d, boundaryOverlap, collapseBisector"),
    ("pocket", "adaptive",
     "114 differences: optimalLoad 0.4d, stepdown 2.5d vs 0.1d, and no leads or compensation on "
     "adaptive"),
)

_MILL_NAMES = ([n for _s, n, _i in _WHOLE_MODEL]
               + ["Slot", "Circular", "ThreadMill", "TraceRim", "ProjectRim", "Adaptive2D",
                  "WaterjetProfile", "ThreePlusTwo", "MorphPair"])
# The Machining Extension's own families this milling setup can feed, each on the geometry kind it
# was measured to take. The ROTARY families are not among them: they wrap a model about a rotary
# axis, and the sibling that drives one builds a setup with a machine and a bound WCS to turn about.
_EXT_NAMES = ["MxContour", "AdvSwarf", "ProbeGeom"]
_TURN_NAMES = [n for _s, n, _i in _TURNED] + ["TurnThread", "TurnSingleGroove", "TurnChamfer"]


def _pocket_floor(p):
    """find_geometry: the flange pocket's arc-slot floor - ONE face, at the pocket depth, facing up.
    The depth and the normal together are what separate it from the square pocket's floor beside it
    and from the flange top the pocket was cut down from."""
    ms = p.get("matches") or []
    m = ms[0] if ms else {}
    pos, nrm = m.get("position") or [0, 0, 9], m.get("normal") or [0, 0, 0]
    return _measured(f"one up-facing floor at z {_POCKET_Z:g} mm",
                     {"count": len(ms), "position": m.get("position"), "normal": m.get("normal"),
                      "area": m.get("area")},
                     len(ms) == 1 and _near(pos[2], _POCKET_Z, 0.5) and nrm[2] > 0.999)


def _rim_circle(p):
    """find_geometry: the circle at the flange top - the fillet's upper tangent, told from the
    flange's own r 40 rims by BOTH its radius and its height, since the round leaves three circles
    of nearly that radius within the query's own 5 per cent band."""
    ms = p.get("matches") or []
    m = ms[0] if ms else {}
    pos = m.get("position") or [0, 0, 9]
    return _measured(f"one r{_RIM_R} circle at the flange top",
                     {"count": len(ms), "radius": m.get("radius"), "position": m.get("position")},
                     len(ms) == 1 and _near(m.get("radius"), _RIM_R, _RIM_TOL)
                     and _near(pos[2], 0.0, 0.5))


def _cbore_walls(p):
    """find_geometry: the four counterbore walls of the bolt circle, all of one radius."""
    ms = p.get("matches") or []
    return _measured(f"four r{_CBORE_R} counterbore walls",
                     {"count": len(ms), "radii": [m.get("radius") for m in ms]},
                     len(ms) == 4 and all(_near(m.get("radius"), _CBORE_R, 0.1) for m in ms))


def _flange_top(p):
    """find_geometry: the flange's own top face, told by its HEIGHT and its normal. Its centroid is
    off the hub axis - the bolt holes and the two pockets are cut out of it - so a query measured at
    the axis names the wrong thing here."""
    ms = p.get("matches") or []
    m = ms[0] if ms else {}
    pos, nrm = m.get("position") or [0, 0, 9], m.get("normal") or [0, 0, 0]
    return _measured("one up-facing face at the flange top, z 0",
                     {"count": len(ms), "position": pos, "normal": nrm, "area": m.get("area")},
                     len(ms) == 1 and m.get("kind") == "planar_face"
                     and _near(pos[2], 0.0, 0.1) and nrm[2] > 0.999
                     and _num(m.get("area")) and m["area"] > 0)


def _wedge_flat(p):
    """find_geometry: the inclined flat on the shaft, told by its AREA. Its plane is neither an
    origin plane nor the shaft wall, so a query that landed on a neighbour reads a different
    area here - which is the reading that says the tool axis is tilted onto the flat itself."""
    ms = p.get("matches") or []
    m = ms[0] if ms else {}
    return _measured(f"one planar flat of {_FLAT_AREA} mm2 on the shaft",
                     {"count": len(ms), "area": m.get("area"), "position": m.get("position"),
                      "normal": m.get("normal")},
                     len(ms) == 1 and m.get("kind") == "planar_face"
                     and _near(m.get("area"), _FLAT_AREA, _FLAT_TOL))


def _round_at(radius, z):
    """find_geometry: the ONE round face or circular edge of that radius, told apart by where its
    centroid sits down the hub's axis - the query's own radius band is 5 per cent wide, and the
    shaft carries two circles of one radius at different heights."""
    def check(p):
        ms = p.get("matches") or []
        m = ms[0] if ms else {}
        pos = m.get("position") or [0, 0, 9]
        return _measured(f"one r{radius} circle centred at z {z:g} mm",
                         {"count": len(ms), "kind": m.get("kind"), "radius": m.get("radius"),
                          "position": m.get("position")},
                         len(ms) == 1 and _near(m.get("radius"), radius, 0.1)
                         and _near(pos[2], z, 0.5))
    return check


def _nuance(row):
    """cam_compare_operations over one neighbour pair: each side's own 'strategy' parameter id, and
    the parameters one side carries that the other does NOT - the diff facts the competence table's
    nuance column is built from, asserted here so the published table cannot drift from the job."""
    a_op, _a, a_id, a_only, b_op, _b, b_id, b_only = row

    def check(p):
        diffs = {d.get("parameter"): d for d in (p.get("differences") or [])}
        strategy = diffs.get("strategy") or {}
        missing = [n for n in a_only + b_only if n not in diffs]
        shared = [n for n in a_only if diffs.get(n, {}).get("operation_b") != "(not present)"]
        shared += [n for n in b_only if diffs.get(n, {}).get("operation_a") != "(not present)"]
        return _measured(f"'{a_op}' and '{b_op}' differ in the parameters the table names",
                         {"difference_count": p.get("difference_count"), "strategy": strategy,
                          "missing": missing, "carried_by_both": shared},
                         not missing and not shared
                         and strategy.get("operation_a") == a_id
                         and strategy.get("operation_b") == b_id)
    return check


def _creates(setup, rows, base_key):
    """One create row per (strategy, name, tool offset), all behind SkipGeneration."""
    return [("cam_create_operation",
             (lambda c, s=s, n=n, i=i: {"setup": setup, "strategy": s, "name": n,
                                        "tool_scope": "document",
                                        "tool_index": _ctx_get(c, base_key, "the hub tool base") + i,
                                        "generate": False}),
             _op_named(setup, s, n), None)
            for s, n, i in rows]


# ACT 10c7: the milling census - what the hub's MillTop offers that no other beat drives, created
# behind SkipGeneration, aimed at the geometry kind each family takes, launched in one generate.
_CENSUS_MILL = [
    _watch(HUB_COMP + ":1"),
    ("cam_edit_tools", {"action": "list", "scope": "document"},
     lambda p: _num(p.get("tool_count")) and p["tool_count"] >= 1,
     ("census_tool", _recall("census_tool", lambda p: p["tool_count"]))),
    ("cam_edit_tools", {"action": "add", "scope": "document",
                        "add_tools": [dict({"from_type": t},
                                           **({"diameter": f"{d:g} mm"} if d else {}))
                                      for t, d in _CENSUS_TOOLS]},
     lambda p: p.get("added") == len(_CENSUS_TOOLS)
     and p.get("tool_count") == _RECALL.get("census_tool") + len(_CENSUS_TOOLS), None),
    # REFUSED: a strategy this setup OFFERS whose isGenerationAllowed reads false. Creating it would
    # succeed and then never generate, so the refusal names the flag it was read from.
    ("cam_create_operation", {"setup": HUB_MILL_SETUP, "strategy": "hole_machining",
                              "tool_scope": "document", "tool_index": 0},
     _refused("isGenerationAllowed false in setup"), None),
    # REFUSED for a DIFFERENT reason: operations.add answers with a name for these two and the
    # setup's own count does not move, so nothing landed. The guard reads that count rather than the
    # return, which is what turns a silent no-op into a refusal - and the refusal names what does
    # the job instead, which is the half a bare "did not land" left the caller to guess.
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "hole_recognition", "name": "HoleRecognition",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _FLAT_AT},
     _refused("did not land", "is not an operation", "cam_select_geometry(selection='holes')"),
     None),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "folder", "name": "StrategyFolder",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _FLAT_AT},
     _refused("did not land", "cam_edit_folders"), None),
] + _creates(HUB_MILL_SETUP, _WHOLE_MODEL, "hub_tool_base") + [
    # THE POCKET FLOOR: the flange pocket's arc slot, 12 mm wide on the bolt circle - a slot-shaped
    # closed contour, which is the shape the slot family wants and the bracket had none of (SLOT-1).
    ("find_geometry", {"target": HUB_COMP, "kind": "planar_face",
                       "nearest_to": [_HUB_X + _POCKET_XY, _POCKET_XY, _POCKET_Z],
                       "max_results": 1}, _pocket_floor, _fg("census_pocket")),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "slot", "name": "Slot",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _SLOT_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "slot", "Slot"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "Slot", "selection": "pocket",
                "handles": [_ctx_get(c, "census_pocket", "the arc-slot pocket floor")],
                "generate": False}, _selected(1), None),
    # THE BOLT HOLES: the counterbore walls, reached through the 'holes' kind, which lands them on
    # 'circularFaces' for both families below. The thread family's own 'thread' selection is REFUSED
    # here - it is the turning thread's input - so a thread-milled hole is aimed through 'holes'.
    ("find_geometry", {"target": HUB_COMP, "kind": "cylinder_face", "radius": _CBORE_R,
                       "max_results": 6}, _cbore_walls,
     ("census_cbores", _recall("census_cbores", lambda p: [m["handle"] for m in p["matches"]]))),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "circular", "name": "Circular",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _FLAT_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "circular", "Circular"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "Circular", "selection": "holes",
                "handles": _ctx_get(c, "census_cbores", "the counterbore walls"),
                "generate": False}, _selected(4), None),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "thread", "name": "ThreadMill",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "census_tool", "the census tool base") + _THREAD_MILL_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "thread", "ThreadMill"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "ThreadMill", "selection": "thread",
                "handles": _ctx_get(c, "census_cbores", "the counterbore walls")},
     _refused("threadFaces", "turning thread strategy"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "ThreadMill", "selection": "holes",
                "handles": _ctx_get(c, "census_cbores", "the counterbore walls"),
                "generate": False}, _selected(4), None),
    # THE ONE CHAIN: the circle at the flange top. Two families take it and neither is a rail pair -
    # the trace runs the cutter along it, the projection drops its passes onto the model under it.
    ("find_geometry", {"target": HUB_COMP, "kind": "circular_edge", "radius": _RIM_R,
                       "nearest_to": [_HUB_X, 0, 0], "max_results": 1},
     _rim_circle, _fg("census_rim")),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "trace", "name": "TraceRim",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _CHAMFER_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "trace", "TraceRim"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "TraceRim", "selection": "chain",
                "handles": [_ctx_get(c, "census_rim", "the flange-top circle")],
                "generate": False}, _selected(1), None),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "project", "name": "ProjectRim",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _BALL_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "project", "ProjectRim"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "ProjectRim", "selection": "chain",
                "handles": [_ctx_get(c, "census_rim", "the flange-top circle")],
                "generate": False}, _selected(1), None),
    # THE 2D ADAPTIVE, roughing the flange pocket the lathe cannot reach. MEASURED on this setup,
    # whose stock is the rest the turning setup left: aimed at the SILHOUETTE it generates EMPTY -
    # nothing stands outside the turned profile - and a 10 mm cutter inside the 12 mm arc slot did
    # the same, so it is cut with the shop set's 6 mm mill.
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "adaptive2d", "name": "Adaptive2D",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _FLAT6_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "adaptive2d", "Adaptive2D"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "Adaptive2D", "selection": "pocket",
                "handles": [_ctx_get(c, "census_pocket", "the arc-slot pocket floor")],
                "generate": False}, _selected(1), None),
    # THE SILHOUETTE, on the cutting family: a waterjet profiles the part's outline.
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "profile2d", "name": "WaterjetProfile",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "census_tool", "the census tool base") + _WATERJET_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "profile2d", "WaterjetProfile"), None),
    ("cam_select_geometry", {"operation": "WaterjetProfile", "selection": "silhouette",
                             "generate": False},
     lambda p: p["setup_models_selected"] is True, None),
    # THE TILTED AXIS: the inclined flat on the shaft, whose own normal becomes the tool axis. The
    # orientation kind lands the face on machiningDirections and engages toolAxisMode in one call.
    ("find_geometry", {"target": HUB_COMP, "kind": "planar_face", "nearest_to": _FLAT_NEAR,
                       "max_results": 1}, _wedge_flat, _fg("census_flat")),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "three_plus_two", "name": "ThreePlusTwo",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _FLAT_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "three_plus_two", "ThreePlusTwo"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "ThreePlusTwo", "selection": "orientation",
                "handles": [_ctx_get(c, "census_flat", "the shaft's inclined flat")],
                "generate": False}, _selected(1), None),
    # THE CURVE PAIR: morph is driven by TWO rim circles, one CurveSelection each. Measured, the
    # same pair fed as ONE selection walks into a single path and reports 'No passes to link'.
    ("find_geometry", {"target": HUB_COMP, "kind": "circular_edge", "radius": _FLANGE_R,
                       "nearest_to": [_HUB_X, 0, _LOWER_RIM_Z], "max_results": 1},
     _round_at(_FLANGE_R, _LOWER_RIM_Z), _fg("census_lower_rim")),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "morph", "name": "MorphPair",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _BALL_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "morph", "MorphPair"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "MorphPair", "selection": "chain",
                "handles": [_ctx_get(c, "census_rim", "the flange-top circle"),
                            _ctx_get(c, "census_lower_rim", "the flange's underside rim")],
                "generate": False}, _selected(2), None),
    # THE MANUAL NC PASS. This beat proves the CREATE and nothing more: measured, a generated manual
    # operation's time row answers "3 : Machining time could not be calculated." and it never
    # appears in empty_toolpaths, so neither oracle this act uses can judge it. It is taken back out
    # rather than left standing under a claim no read here supports.
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "manual", "name": "ManualNC",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _FLAT_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "manual", "ManualNC"), None),
    ("cam_delete", {"entity": "ManualNC"}, _op_deleted("ManualNC"), None),
    ("cam_generate", {"target": HUB_MILL_SETUP, "skip_valid": False},
     _launched_on(HUB_MILL_SETUP), None),
]


# ACT 10c8: the milling census read, once the act boundary's poll has certified that generation -
# the hub framed once, every strategy shown in that frame, then the non-empty oracle per strategy.
_CENSUS_MILL_READ = [
    _watch(HUB_COMP + ":1"),
] + _reveal(_MILL_NAMES) + [
    ("cam_get", {"include": ["time"], "setup": HUB_MILL_SETUP},
     _all_cut(HUB_MILL_SETUP, len(_MILL_NAMES), names=_MILL_NAMES), None),
]


def _launched_op(name):
    """cam_generate over ONE operation: the resolved node's kind beside the name asked for, so the
    payload says the name reached an operation rather than a setup sharing it."""
    def check(p):
        return _measured(f"generation launched over operation '{name}'",
                         {"launched": p.get("launched"), "target": p.get("target"),
                          "handle": p.get("handle"), "launch_reasons": p.get("launch_reasons")},
                         p.get("launched") is True and p.get("target") == f"operation '{name}'"
                         and bool(p.get("handle")))
    return check


# ACT 10c8b: THE LONG FAMILY - the whole-model strategy whose GENERATION wall clock is the reason it
# rides an act of its own rather than the census beat above. Nothing is selected: it cuts the setup's
# own model, so one create is the whole beat. It is launched BY NAME: measured, a setup-scoped
# generate rebuilds every operation the setup holds whatever skip_valid says, which over this setup
# would be the census's sixteen as well.
_CENSUS_LONG = [
    _watch(HUB_COMP + ":1"),
] + _creates(HUB_MILL_SETUP, [(s, n, i) for s, n, i, _secs in _LONG_FAMILIES],
             "hub_tool_base") + [
    ("cam_generate", {"target": n, "skip_valid": False}, _launched_op(n), None)
    for n in _LONG_NAMES
]


# ACT 10c8c: the pair read, behind the boundary poll their own budget sizes.
_CENSUS_LONG_READ = [
    _watch(HUB_COMP + ":1"),
] + _reveal(_LONG_NAMES) + [
    ("cam_get", {"include": ["time"], "setup": HUB_MILL_SETUP},
     _all_cut(HUB_MILL_SETUP, len(_LONG_NAMES), names=_LONG_NAMES), None),
]


# ACT 10c8d: POCKET CLEARING, in a SETUP OF ITS OWN so that it runs FIRST. The family rest-machines
# from the operations before it, so behind the census setup's sixteen whole-model families the same
# operation generates EMPTY; a setup no other operation has cleared is what gives it a toolpath.
_CENSUS_POCKET = [
    _watch(HUB_COMP + ":1"),
    ("cam_create_setup", {"name": HUB_POCKET_SETUP, "operation_type": "milling",
                          "models": [HUB_COMP + ":1"]},
     _setup_created(HUB_POCKET_SETUP, "milling"), None),
    ("cam_create_operation",
     lambda c: {"setup": HUB_POCKET_SETUP, "strategy": "pocket_clearing", "name": _POCKET_OP,
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _FLAT6_AT,
                "generate": False},
     _op_named(HUB_POCKET_SETUP, "pocket_clearing", _POCKET_OP), None),
    ("cam_select_geometry",
     lambda c: {"operation": _POCKET_OP, "selection": "pocket",
                "handles": [_ctx_get(c, "census_pocket", "the arc-slot pocket floor")],
                "generate": False}, _selected(1), None),
    ("cam_generate", {"target": HUB_POCKET_SETUP, "skip_valid": False},
     _launched_on(HUB_POCKET_SETUP), None),
]


# ACT 10c8e: its read, behind the act boundary's poll - machining time above zero is the oracle,
# since hasToolpath reads true on an empty one.
_CENSUS_POCKET_READ = [
    _watch(HUB_COMP + ":1"),
] + _reveal([_POCKET_OP]) + [
    ("cam_get", {"include": ["time"], "setup": HUB_POCKET_SETUP},
     _all_cut(HUB_POCKET_SETUP, 1, names=[_POCKET_OP]), None),
]


# ACT 10c11: the extension families, created on the milling setup once the census above has taken
# its reads. The generate is skip_valid, so only these three compute - the library add that would
# have invalidated the rest happened at the top of the census act, before anything generated.
_CENSUS_EXT = [
    _watch(HUB_COMP + ":1"),
    # the setup's own vocabulary first: a strategy this installation reads blocked reds HERE, naming
    # it, rather than inside the create that used it.
    ("cam_get", {"include": ["strategies"], "setup": HUB_MILL_SETUP},
     _offers(HUB_MILL_SETUP, "multi_axis_contour", "advanced_swarf", "probe_geometry"), None),
    # THE FLANGE-TOP CIRCLE, re-found rather than recalled: several generations stand between this
    # act and the one that first measured it, and a handle is short-lived.
    ("find_geometry", {"target": HUB_COMP, "kind": "circular_edge", "radius": _RIM_R,
                       "nearest_to": [_HUB_X, 0, 0], "max_results": 1},
     _rim_circle, _fg("ext_rim")),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "multi_axis_contour", "name": "MxContour",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _BALL_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "multi_axis_contour", "MxContour"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "MxContour", "selection": "chain",
                "handles": [_ctx_get(c, "ext_rim", "the flange-top circle")],
                "generate": False}, _selected(1), None),
    # THE FLANGE WALL, cut with the SIDE of the cutter. The swarf family carries no curve parameter
    # at all - the refusal below names the surface set it carries instead - so the wall is handed to
    # that set rather than to a rail pair.
    ("find_geometry", {"target": HUB_COMP, "kind": "cylinder_face", "radius": _WALL_R,
                       "nearest_to": [_HUB_X, 0, _WALL_Z], "max_results": 1},
     _round_at(_WALL_R, _WALL_Z), _fg("ext_wall")),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "advanced_swarf", "name": "AdvSwarf",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _FLAT_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "advanced_swarf", "AdvSwarf"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "AdvSwarf", "selection": "chain",
                "handles": [_ctx_get(c, "ext_rim", "the flange-top circle")]},
     _refused("has no curve-selection parameter", "surface set(s) swarf"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "AdvSwarf", "selection": "surfaces", "surface_target": "swarf",
                "handles": [_ctx_get(c, "ext_wall", "the flange wall")],
                "generate": False}, _selected(1), None),
    # THE PROBING CYCLE that measures a FACE, on the probe this act's library add cloned: the create
    # is refused any other tool kind, naming the type it was handed.
    ("find_geometry", {"target": HUB_COMP, "kind": "planar_face",
                       "nearest_to": [_HUB_X, 0, 0], "max_results": 1},
     _flange_top, _fg("ext_top")),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "probe_geometry", "name": "ProbeGeom",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _FLAT_AT},
     _refused("needs a PROBE", "flat end mill"), None),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "probe_geometry", "name": "ProbeGeom",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "census_tool", "the census tool base") + _PROBE_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "probe_geometry", "ProbeGeom"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "ProbeGeom", "selection": "probe",
                "handles": [_ctx_get(c, "ext_top", "the flange top")],
                "generate": False}, _selected(1), None),
    ("cam_generate", {"target": HUB_MILL_SETUP, "skip_valid": True},
     _launched_on(HUB_MILL_SETUP, skip_valid=True), None),
]


# ACT 10c12: the extension families read, behind their own boundary poll - the same reveal, then the
# non-empty oracle over the three.
_CENSUS_EXT_READ = [
    _watch(HUB_COMP + ":1"),
] + _reveal(_EXT_NAMES) + [
    ("cam_get", {"include": ["time"], "setup": HUB_MILL_SETUP},
     _all_cut(HUB_MILL_SETUP, len(_EXT_NAMES), names=_EXT_NAMES), None),
]


# ACT 10c9: the turning census, on the hub's mill-turn setup - the cycles the sweep's four-op lathe
# job leaves undriven, each on the insert kind it was measured to take.
_CENSUS_TURN = _creates(HUB_TURN_SETUP, _TURNED, "hub_tool_base") + [
    # THE SHAFT WALL the turned thread runs on: the 'thread' selection kind, whose refusal on the
    # milling thread operation above named this strategy family as its home.
    ("find_geometry", {"target": HUB_COMP, "kind": "cylinder_face", "radius": _SHAFT_R,
                       "nearest_to": [_HUB_X, 0, _SHAFT_MID_Z], "max_results": 1},
     _round_at(_SHAFT_R, _SHAFT_MID_Z), _fg("census_shaft")),
    ("cam_create_operation",
     lambda c: {"setup": HUB_TURN_SETUP, "strategy": "turning_thread", "name": "TurnThread",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _THREAD_INSERT_AT,
                "generate": False},
     _op_named(HUB_TURN_SETUP, "turning_thread", "TurnThread"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "TurnThread", "selection": "thread",
                "handles": [_ctx_get(c, "census_shaft", "the shaft wall")],
                "generate": False}, _selected(1), None),
    # THE SHAFT GROOVE: the 'groove' kind takes an EDGE, and the single-groove cycle drops the insert
    # into the groove that edge bounds.
    ("find_geometry", {"target": HUB_COMP, "kind": "circular_edge", "radius": _GROOVE_R,
                       "nearest_to": [_HUB_X, 0, _GROOVE_TOP_Z], "max_results": 1},
     _round_at(_GROOVE_R, _GROOVE_TOP_Z), _fg("census_groove")),
    ("cam_create_operation",
     lambda c: {"setup": HUB_TURN_SETUP, "strategy": "turning_single_groove",
                "name": "TurnSingleGroove", "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _GROOVE_AT,
                "generate": False},
     _op_named(HUB_TURN_SETUP, "turning_single_groove", "TurnSingleGroove"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "TurnSingleGroove", "selection": "groove",
                "handles": [_ctx_get(c, "census_groove", "the shaft groove")],
                "generate": False}, _selected(1), None),
    # THE TURNING CHAMFER, aimed at the chamfer that breaks the part end. Its positions are a DIRECT
    # object set, not a curve one, so the chain kind is refused first - naming what the call looked
    # for - and the 'chamfer' kind then lands the chamfer's own bounding circle on 'chamfers'.
    ("cam_create_operation",
     lambda c: {"setup": HUB_TURN_SETUP, "strategy": "turning_chamfer", "name": "TurnChamfer",
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _TURN_AT,
                "generate": False},
     _op_named(HUB_TURN_SETUP, "turning_chamfer", "TurnChamfer"), None),
    ("cam_select_geometry",
     lambda c: {"operation": "TurnChamfer", "selection": "chain",
                "handles": [_ctx_get(c, "census_groove", "the shaft groove")]},
     _refused("has no curve-selection parameter", "'chamfers' (selection='chamfer')"), None),
    ("find_geometry", {"target": HUB_COMP, "kind": "circular_edge", "radius": _CHAMFER_R,
                       "nearest_to": [_HUB_X, 0, _CHAMFER_EDGE_Z], "max_results": 1},
     _round_at(_CHAMFER_R, _CHAMFER_EDGE_Z), _fg("census_chamfer")),
    ("cam_select_geometry",
     lambda c: {"operation": "TurnChamfer", "selection": "chamfer",
                "handles": [_ctx_get(c, "census_chamfer", "the part-end chamfer's circle")],
                "generate": False}, _selected(1), None),
    ("cam_generate", {"target": HUB_TURN_SETUP, "skip_valid": False},
     _launched_on(HUB_TURN_SETUP), None),
]


# ACT 10c10: the turning census read, behind its own boundary poll - the same reveal, then the
# non-empty oracle and the neighbour diff the table's nuance column is drawn from.
_CENSUS_TURN_READ = [
    _watch(HUB_COMP + ":1"),
] + _reveal(_TURN_NAMES) + [
    ("cam_get", {"include": ["time"], "setup": HUB_TURN_SETUP},
     _all_cut(HUB_TURN_SETUP, len(_TURN_NAMES), names=_TURN_NAMES), None),
    ("cam_compare_operations", {"operation_a": NUANCE[0][0], "operation_b": NUANCE[0][4],
                                "max_results": 400}, _nuance(NUANCE[0]), None),
]


# THE CENSUS: one record per strategy the hub's two setups offer, which
# tests/generated/STRATEGY_COMPETENCE.md is generated from. A row is
# (name, verdict, the geometry kind it takes, the tool kind, where it is proven OR the measured
# reason it is not) - and every reason is the message the platform answered with, never a guess.
PROVEN, MEASURED, CREATED, REFUSED, SKIPPED = (
    "proven", "measured", "created", "refused", "skipped")

_LONG = tuple(
    (strategy, PROVEN, "none - the setup's model",
     "ball end mill" if offset == _BALL_AT else "flat end mill",
     f"ACT 10c8b {name}, machining time read in ACT 10c8c - launched BY NAME, on an act poll budget "
     f"of its own; it cut {secs:g} s of toolpath")
    for strategy, name, offset, secs in _LONG_FAMILIES)

CENSUS = _LONG + (
    # --- the census beats above ---------------------------------------------------------------
    ("parallel", PROVEN, "none - the setup's model", "ball end mill", "ACT 10c7 Parallel"),
    ("scallop", PROVEN, "none - the setup's model", "ball end mill", "ACT 10c7 Scallop"),
    ("pencil", PROVEN, "none - the setup's model", "ball end mill", "ACT 10c7 Pencil"),
    ("contour3d", PROVEN, "none - the setup's model", "ball end mill", "ACT 10c7 Contour3D"),
    ("radial", PROVEN, "none - the setup's model", "ball end mill", "ACT 10c7 Radial"),
    ("spiral", PROVEN, "none - the setup's model", "ball end mill", "ACT 10c7 Spiral"),
    ("morphed_spiral", PROVEN, "none - the setup's model", "ball end mill",
     "ACT 10c7 MorphedSpiral"),
    ("flat", PROVEN, "none - the setup's model", "flat end mill", "ACT 10c7 Flat"),
    ("horizontal", PROVEN, "none - the setup's model", "flat end mill", "ACT 10c7 Horizontal"),
    ("slot", PROVEN, "pocket (a slot-shaped floor)", "slot mill", "ACT 10c7 Slot"),
    ("circular", PROVEN, "holes -> circularFaces", "flat end mill", "ACT 10c7 Circular"),
    ("thread", PROVEN, "holes -> circularFaces", "thread mill that fits the bore",
     "ACT 10c7 ThreadMill"),
    ("trace", PROVEN, "chain", "chamfer mill", "ACT 10c7 TraceRim"),
    ("project", PROVEN, "chain", "ball end mill", "ACT 10c7 ProjectRim"),
    ("adaptive2d", PROVEN, "pocket", "flat end mill that fits the pocket", "ACT 10c7 Adaptive2D"),
    ("profile2d", PROVEN, "silhouette", "waterjet (a cutting tool)", "ACT 10c7 WaterjetProfile"),
    ("manual", CREATED, "none", "any",
     "ACT 10c7 ManualNC creates it and reads the name back, then deletes it - a generated Manual NC "
     "answers '3 : Machining time could not be calculated.' on its time row and never appears in "
     "empty_toolpaths, so neither non-empty oracle can judge it"),
    ("turning_adaptive_roughing", PROVEN, "none - the setup's model", "turning grooving insert",
     "ACT 10c9 TurnAdaptive"),
    ("turning_profile", PROVEN, "none - the setup's model", "turning general insert",
     "ACT 10c9 TurnProfile"),
    ("turning_groove_roughing", PROVEN, "none - the setup's model", "turning grooving insert",
     "ACT 10c9 TurnGrooveRough"),
    ("turning_groove_finishing", PROVEN, "none - the setup's model", "turning grooving insert",
     "ACT 10c9 TurnGrooveFinish"),
    ("turning_profile_groove", PROVEN, "none - the setup's model", "turning grooving insert",
     "ACT 10c9 TurnProfileGroove"),
    ("turning_thread", PROVEN, "thread -> threadFaces", "turning threading insert",
     "ACT 10c9 TurnThread"),
    ("turning_single_groove", PROVEN, "groove -> grooves (an edge)", "turning grooving insert",
     "ACT 10c9 TurnSingleGroove"),
    # --- the CAM acts before them --------------------------------------------------------------
    ("face", PROVEN, "face", "face mill", "ACT 10a"),
    ("adaptive", PROVEN, "none - the setup's model", "flat end mill", "ACT 10a"),
    ("contour2d", PROVEN, "silhouette / chain", "flat end mill", "ACT 10a, ACT 10c5"),
    ("pocket2d", PROVEN, "pocket", "flat end mill", "ACT 10a"),
    ("chamfer2d", PROVEN, "face", "chamfer mill", "ACT 10a"),
    ("drill", PROVEN, "holes -> holeFaces", "drill / center drill", "ACT 10a"),
    ("bore", PROVEN, "holes -> circularFaces", "flat end mill", "ACT 10a"),
    ("engrave", PROVEN, "sketch", "chamfer mill", "ACT 10a"),
    ("steep_and_shallow", PROVEN, "face", "ball end mill", "ACT 10a (machining extension)"),
    ("probe", PROVEN, "probe", "probe", "ACT 10a (machining extension)"),
    ("swarf", PROVEN, "chain (a RAIL PAIR)", "flat end mill", "ACT 10c (machining extension)"),
    ("deburr", PROVEN, "chain (edges)", "ball end mill", "ACT 10c (machining extension)"),
    ("geodesic", PROVEN, "surfaces -> driveSurfaces", "ball end mill",
     "ACT 10c (machining extension)"),
    ("multiaxis_finishing", PROVEN, "surfaces -> floorSurfaces", "ball end mill",
     "ACT 10c (machining extension)"),
    ("multiaxis_roughing", PROVEN, "surfaces -> floorSurfaces", "flat end mill",
     "ACT 10c (machining extension)"),
    ("flow2", PROVEN, "surfaces -> driveSurfaces", "ball end mill",
     "ACT 10c (machining extension)"),
    ("turning_face", PROVEN, "none - the setup's model", "turning general insert",
     "ACT 10c4 TurnFace"),
    ("turning_profile_roughing", PROVEN, "none - the setup's model", "turning general insert",
     "ACT 10c4 TurnRough"),
    ("turning_profile_finishing", PROVEN, "none - the setup's model", "turning general insert",
     "ACT 10c4 TurnFinish"),
    ("turning_part", PROVEN, "none - the setup's model", "turning grooving insert",
     "ACT 10c4 TurnPart"),
    # --- refused up front, each on a read the create tool takes before it mutates ---------------
    ("chamfer", REFUSED, "-", "-",
     "isGenerationAllowed reads false; refused live by ACT 10c on the cameo setup"),
    ("flow", REFUSED, "-", "-", "isGenerationAllowed reads false on this installation"),
    ("inclined_walls", REFUSED, "-", "-", "isGenerationAllowed reads false on this installation"),
    ("hole_machining", REFUSED, "-", "-",
     "isGenerationAllowed reads false; refused live by ACT 10c7"),
    ("hole_recognition", REFUSED, "-", "-",
     "operations.add answered with a name and the setup's operation count did not move; "
     "refused live by ACT 10c7"),
    ("folder", REFUSED, "-", "-",
     "operations.add answered with a name and the setup's operation count did not move; "
     "refused live by ACT 10c7"),
    ("turning_chamfer", PROVEN, "chamfer -> chamfers (an edge)", "turning general insert",
     "ACT 10c9 TurnChamfer on the circle bounding the hub's part-end chamfer, machining time read "
     "in ACT 10c10; its positions are a DIRECT object set, so the chain kind is refused naming "
     "'chamfers', and that chamfer's own cone FACE raises '2 : InternalValidationError : "
     "status.isOk()' where the bounding EDGE lands"),
    ("pocket_clearing", PROVEN, "pocket", "flat end mill that fits the pocket",
     "ACT 10c8d PocketClear on the hub's flange arc slot with the 6 mm mill, machining time read "
     "in ACT 10c8e; it cut 140.5 s. It needs a SETUP OF ITS OWN because it rest-machines from the "
     "operations before it: behind the census act's sixteen whole-model families the same "
     "operation reads EMPTY whether it is created before or after Adaptive2D (measured both ways, "
     "while Adaptive2D on that same slot cuts 27.6 s either way), and the flange's 8 mm round "
     "pocket answers 'too small to be reached with given ramping constraints' for that cutter"),
    # --- skipped, each on the message the platform answered with --------------------------------
    ("three_plus_two", PROVEN, "orientation -> machiningDirections", "flat end mill",
     "ACT 10c7 ThreePlusTwo on the inclined flat cut into the shaft's plain stretch, machining "
     "time read in ACT 10c8; the flat's own normal becomes the tool axis, and the selection "
     "engages toolAxisMode in the same call"),
    ("blend", SKIPPED, "two or more drive curves - no route", "ball end mill",
     "'Drive Curves: Incorrect number of drive curves. Select two or more drive curves.' - and the "
     "chain kind lands on this operation's machining BOUNDARY instead"),
    ("corner", SKIPPED, "none - the setup's model", "ball end mill",
     "'No valid reference tool nor valid reference stock model' - it is a rest-machining family"),
    ("feature_construction", SKIPPED, "base surface + feature - no route", "-",
     "'Base: No base surface was selected / Feature: No feature was selected' - an additive family"),
    ("inspect_surface", SKIPPED, "inspectSurfacePositions - no route", "probe",
     "no API write lands a point on the surface (ledger INSPECT-1)"),
    ("morph", PROVEN, "chain -> curves (a curve PAIR)", "ball end mill",
     "ACT 10c7 MorphPair across the flange's two rim circles, machining time read in ACT 10c8; it "
     "wants a PAIR, one CurveSelection each - both circles fed to a single selection walk into one "
     "path and the operation reports 'No passes to link'"),
    ("turning_trace", SKIPPED, "chain -> modelContour", "turning general insert",
     "its drive input reads a CadContours2dParameterValue - the curve family - so a chain applies "
     "to it (1 path, 4 segments on a measured box) and clears 'Model Contour: No model contour "
     "selected to machine.'; no beat has yet generated one on a turning insert"),
    # The four second-spindle cycles, each CREATED on the machined turning setup and generated: all
    # four answer with the same error and no toolpath. The machine's own kinematics read one spindle
    # beside that, but the error is what the platform said, so the error is what ships.
    ("turning_stock_transfer", SKIPPED, "-", "turning general insert",
     "generated with 'Toolpath is not supported for the given tool and settings.'"),
    ("bar_pull", SKIPPED, "-", "turning grooving insert",
     "generated with 'Toolpath is not supported for the given tool and settings.'"),
    ("subspindle_grab", SKIPPED, "-", "turning general insert",
     "generated with 'Toolpath is not supported for the given tool and settings.'"),
    ("subspindle_return", SKIPPED, "-", "turning general insert",
     "generated with 'Toolpath is not supported for the given tool and settings.'"),
    # --- the extension families ACT 10c11 drives, behind the machining_extension tier -----------
    ("multi_axis_contour", PROVEN, "chain", "ball end mill",
     "ACT 10c11 MxContour on one edge chain of the hub, machining time read in ACT 10c12 "
     "(machining extension)"),
    ("advanced_swarf", PROVEN, "surfaces -> advancedSwarfSurfaces", "flat end mill",
     "ACT 10c11 AdvSwarf on the hub's flange wall, machining time read in ACT 10c12; the chain "
     "kind is refused naming the 'swarf' surface set this operation carries instead, and a "
     "drafted block wall generates EMPTY ('The tool or surface selections may prevent any area "
     "from being machined.') (machining extension)"),
    ("probe_geometry", PROVEN, "probe", "probe",
     "ACT 10c11 ProbeGeom with a cloned probe on one face, machining time read in ACT 10c12; "
     "a tool that is not a probe is refused naming the type it was handed (machining extension)"),
    # The rotary trio wraps a model about a rotary AXIS, so it rides the hub - a solid of revolution
    # about that very axis - in a setup of its own carrying a 4-axis machine and a WCS bound to the
    # hub's centre. Each row carries the contract the operation was read to hold.
    ("rotary_contour", PROVEN, "none - the setup's model", "ball end mill",
     "ACT 10c13 RotContour on the hub's rotary setup, machining time read in ACT 10c14; the "
     "operation reads axisView_orientation_mode 'axisZ' - the axis its passes wrap about - and "
     "axisView_origin_mode 'jobOrigin', so it turns about the setup's own WCS (machining extension)"),
    ("rotary_pocket", PROVEN, "none - the setup's model", "flat end mill",
     "ACT 10c13 RotPocket on the hub's rotary setup, machining time read in ACT 10c14; the "
     "operation reads axisView_orientation_mode 'axisZ' - the axis its passes wrap about - and "
     "axisView_origin_mode 'jobOrigin', so it turns about the setup's own WCS (machining extension)"),
    ("rotary_finishing", PROVEN, "none - the setup's model", "ball end mill",
     "ACT 10c13 RotFinish on the hub's rotary setup, machining time read in ACT 10c14; the "
     "operation reads axisView_orientation_mode 'axisZ' - the axis its passes wrap about - and "
     "axisView_origin_mode 'jobOrigin', so it turns about the setup's own WCS (machining extension)"),
    ("multi_axis_morph", MEASURED, "surfaces -> driveSurfaces", "ball end mill",
     "driven on the hub with the flange wall as its drive surface, reading 123.6 s of machining "
     "time; it is not a beat because that generation ran past 240 s of polling with the status "
     "read still answering 'generating'. The chain kind lands on this operation, and it then "
     "generates with 'Drive Surfaces: No valid drive surfaces selected.'"),
)
