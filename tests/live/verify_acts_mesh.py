# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""ACT rows: the surface-prep, mesh and arrange cameos.

Families with no home on the mechanism ride scratch fixtures in the SAME document - surface bodies
built, trimmed and stitched, a mesh round-tripped through export and insert, and the arrange solver
nesting last, because it restructures what it nests.
"""

from verify_core import (
    EXPORT_DIR, _RECALL, _arranged, _base_feature_closed, _base_feature_open, _box, _ctx_get,
    _datum_plane, _drilled, _dwell, _extent_measured, _extruded, _fg, _fgn, _holder_computed,
    _made_component, _measured, _mesh_round_trip, _num, _rebuilt, _recall, _refused, _repair_no_op,
    _split_bodies, _stitched, _unstitched, _watch)


# --- the CAMEO acts: surface-prep, mesh, and CAM families ride scratch fixtures in the SAME doc -
# These families have no natural home on the mechanism itself, so the spec places them as cameos.

# ACT 5: MACHINING PREP - surfaces, sheet ops, split/stitch/arrange/base-feature, holder read.
_MACHINING = [
    # the surface/split/stitch cameos live on a GRID (y=200 row, plus a z-lifted revolve) so each
    # builds in clear space a viewer can see, never on top of the part or another cameo.
    ("model_create_component", {"name": "SRev", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xz", "name": "SRevS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 10, "y1": 60,
                                           "x2": 10, "y2": 90}],
                             "sketch_name": "SRevS"}, "ok", None),
    # 'is_solid' is isSolid read off the created body: a profile that closed into a SOLID, and a
    # body whose flag would not read at all (named in 'unverified'), both return ok.
    ("surface_revolve", {"sketch_name": "SRevS", "axis": "z", "angle_deg": 360},
     lambda p: p["is_solid"] is False and bool(p["result_bodies"]) and "unverified" not in p, None),
    # a CLOSED revolved sphere surface encloses one cell; surface_fill must seal it to a SOLID at
    # the enclosed volume (r=6mm -> 904.78 mm3) MEASURED off the result, never predicted.
    ("model_create_component", {"name": "FillDemo", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xz", "name": "FillProf"}, "ok", None),
    # the arc's endpoints must sit ON the revolve axis (x=0) or the revolved surface is an open
    # tube enclosing nothing, and surface_fill refuses it (live-measured).
    ("sketch_add_geometry", {"geometry": [{"kind": "arc", "cx": 0, "cy": 150, "x1": 0, "y1": 156,
                                           "sweep_deg": 180}],
                             "sketch_name": "FillProf"}, "ok", None),
    ("surface_revolve", {"sketch_name": "FillProf", "axis": "z", "angle_deg": 360}, "ok", None),
    # the closed sphere sheet encloses exactly ONE cell, so index 1 is one past the end: refused
    # NAMING the index and the range that exists, never clamped onto a neighbouring cell. The parse
    # happens before any cell is kept, so the sheet is untouched and the fill below is still its
    # first feature.
    ("surface_fill", {"tools": ["FillDemo"], "operation": "new", "cells": [1]},
     _refused("does not exist", "0..0"), None),
    ("surface_fill", {"tools": ["FillDemo"], "operation": "new"},
     lambda p: p.get("filled") is True and p.get("all_solid") is True
     and abs(p.get("result_volume", 0) - 904.78) < 10
     and p.get("tools_unclassified", 0) == 0, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("find_geometry", {"target": "SRev", "kind": "cylinder_face", "max_results": 1}, "ok", _fg("srev_face")),
    ("surface_thicken", lambda c: {"faces": [_ctx_get(c, "srev_face", "surface face")], "thickness": 2}, "ok", None),
    ("model_create_component", {"name": "Surf", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "SurfS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 200, "y1": 200,
                                           "x2": 240, "y2": 230}],
                             "sketch_name": "SurfS"}, "ok", None),
    ("surface_extrude", {"sketch_name": "SurfS", "distance": 15}, "ok", None),
    ("find_geometry", {"target": "Surf", "kind": "planar_face", "max_results": 1}, "ok", _fg("surf_face")),
    # 'faces_offset' is counted off the CREATED surface, so it disagrees with the one face requested
    # when chaining widened the selection - and reads null (in 'unverified') when nothing was read.
    ("surface_offset", lambda c: {"faces": [_ctx_get(c, "surf_face", "surface face")], "distance": 3},
     lambda p: (p["faces_offset"] == p["faces_requested"] == 1 and bool(p["result_bodies"])
                and "unverified" not in p), None),
    ("surface_offset", lambda c: {"faces": [_ctx_get(c, "surf_face", "surface face")], "distance": 0}, "ok", None),
    # THREE edges of ONE body, not one: edge.body hands back a fresh proxy per read, so a
    # body-set walk keyed on object identity counts this single body three times and refuses a
    # legal call as multi-body. A single-edge beat cannot catch that - one edge never disagrees
    # with itself. nearest_to pins the pick to the TOP rim (z=15): three of that rim's four edges
    # connect at endpoints into one open chain, while an arbitrary pick mixes top and bottom rims
    # into a disconnected set the platform rejects as an invalid extend input.
    ("find_geometry", {"target": "Surf", "kind": "line_edge", "nearest_to": [220, 215, 15], "max_results": 3}, "ok", _fgn("surf_edges")),
    ("surface_extend", lambda c: {"edges": _ctx_get(c, "surf_edges", "surface edges"), "distance": 2},
     # no extend_alignment given: the key is ABSENT from the payload, so nothing was written and
     # the API's own default stands - an echoed key here would be a claim about an unwritten value.
     lambda p: p.get("body_count", 1) == 1 and "extend_alignment" not in p, None),
    ("find_geometry", {"target": "Surf", "kind": "planar_face", "max_results": 1}, "ok", _fg("surf_body")),
    # the flip is invisible in every other read, so the row asserts the tool's own isParamReversed
    # read-back: 'reversed_confirmed' is false whenever the after-count does not match the flip.
    ("surface_reverse_normal", lambda c: {"bodies": [_ctx_get(c, "surf_body", "surface body")]},
     lambda p: p.get("reversed_confirmed") is True and p.get("faces_total", 0) > 0, None),
    # extend_alignment + thicken_type, on their own sheet so the read-backs never ride on edges an
    # earlier extend already moved. Both properties are written through set_verified, so a value the
    # platform drops comes back as an error rather than an echoed success. A rectangle extruded as a
    # surface gives a four-walled sheet, which is the convex corner set 'rounded' needs to mean
    # anything - a single flat face has no corner to round.
    ("model_create_component", {"name": "SAlign", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "SAlignS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 700, "y1": 200,
                                           "x2": 740, "y2": 230}],
                             "sketch_name": "SAlignS"}, "ok", None),
    ("surface_extrude", {"sketch_name": "SAlignS", "distance": 15}, "ok", None),
    ("find_geometry", {"target": "SAlign", "kind": "line_edge", "nearest_to": [720, 215, 15],
                       "max_results": 3}, "ok", _fgn("salign_edges")),
    ("surface_extend", lambda c: {"edges": _ctx_get(c, "salign_edges", "aligned sheet edges"),
                                  "distance": 2, "extend_alignment": "align_edges"},
     lambda p: p.get("extend_alignment") == "align_edges", None),
    ("find_geometry", {"target": "SAlign", "kind": "planar_face", "max_results": 1}, "ok",
     _fg("salign_face")),
    ("surface_thicken", lambda c: {"faces": [_ctx_get(c, "salign_face", "aligned sheet face")],
                                   "thickness": 1, "thicken_type": "rounded"},
     lambda p: p.get("thicken_type") == "rounded", None),
    # back to the component that was active before this cameo, so the ones after it nest as before.
    ("design_activate_component", {"occurrence": "Surf:1"}, "ok", None),
    ("model_create_component", {"name": "SDel", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "SD1"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 300, "y1": 200,
                                           "x2": 320, "y2": 220}],
                             "sketch_name": "SD1"}, "ok", None),
    ("model_extrude", {"sketch_name": "SD1", "profile_index": 0, "distance": 10}, _extruded, None),
    ("find_geometry", {"target": "SDel", "kind": "planar_face", "nearest_to": [310, 210, 10], "max_results": 1}, "ok", _fg("sdel_top")),
    # one face off a six-faced box: 'faces_delta' is measured across the result bodies' own counts,
    # and a delete that RAISED the count or ate a whole body is published with a warning, not an error.
    ("surface_delete_face", lambda c: {"faces": [_ctx_get(c, "sdel_top", "top face")], "heal": False},
     lambda p: p["faces_delta"] == -1 and p["bodies_consumed"] == 0, None),
    ("find_geometry", {"target": "SDel", "kind": "line_edge", "nearest_to": [310, 210, 10], "max_results": 4}, "ok", _fgn("sdel_rim")),
    ("surface_patch", lambda c: {"boundary": _ctx_get(c, "sdel_rim", "rim edges")}, "ok", None),
    # a patch with operation 'new' leaves the opened body untouched, so the SAME rim carries the two
    # option beats below. 'continuity' is written through set_verified, so a value the platform
    # dropped is an error - the payload cannot echo a continuity the patch is not running.
    ("surface_patch", lambda c: {"boundary": _ctx_get(c, "sdel_rim", "rim edges"),
                                 "continuity": "tangent"},
     lambda p: p.get("continuity") == "tangent", None),
    # an interior RAIL the patch surface must pass through: a sheet standing in the opening, whose
    # top edge crosses it end to end with both ends landing on the rim. 'interior_rail_count' is the
    # count PatchFeatureInput.interiorRailsAndPoints reads back, not the number of handles passed.
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "PatchRail", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "PatchRailS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 300, "y1": 210,
                                           "x2": 320, "y2": 210}],
                             "sketch_name": "PatchRailS"}, "ok", None),
    ("surface_extrude", {"sketch_name": "PatchRailS", "distance": 10}, "ok", None),
    ("find_geometry", {"target": "PatchRail", "kind": "line_edge", "nearest_to": [310, 210, 10],
                       "max_results": 1}, "ok", _fg("patch_rail")),
    ("design_activate_component", {"occurrence": "SDel:1"}, "ok", None),
    ("surface_patch", lambda c: {"boundary": _ctx_get(c, "sdel_rim", "rim edges"),
                                 "interior_rails": [_ctx_get(c, "patch_rail", "the interior rail")]},
     lambda p: p.get("interior_rail_count") == 1, None),
    # rails fit ONE patch surface, so pairing them with the multi-loop 'boundaries' is refused
    # BEFORE any patch runs - every loop would otherwise be handed the same rails.
    ("surface_patch", lambda c: {"boundaries": [_ctx_get(c, "sdel_rim", "rim edges")],
                                 "interior_rails": [_ctx_get(c, "patch_rail", "the interior rail")]},
     "refused", None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("sketch_create", {"plane": "xy", "name": "Proj"}, "ok", None),
    ("find_geometry", {"target": "SDel", "kind": "planar_face", "nearest_to": [310, 210, 0], "max_results": 1}, "ok", _fg("proj_face")),
    ("sketch_project", lambda c: {"entities": [_ctx_get(c, "proj_face", "project face")], "sketch_name": "Proj"}, "ok", None),
    # surface_trim + surface_untrim on an intersecting-sheet topology. Staged in clear space (X=600)
    # so no other body's surface intersects the sheet - only its own cutter divides it, keeping the
    # trim deterministic (a coincident surface adds phantom cells and the trim keeps the wrong one).
    ("model_create_component", {"name": "SHole", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "SH1"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 0,
                                           "x2": 640, "y2": 0}],
                             "sketch_name": "SH1"}, "ok", None),
    ("surface_extrude", {"sketch_name": "SH1", "distance": 40}, "ok", None),
    ("sketch_create", {"plane": "xz", "name": "SH2"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 620, "cy": -20, "radius": 5}],
                             "sketch_name": "SH2"}, "ok", None),
    ("surface_extrude", {"sketch_name": "SH2", "distance": 10, "symmetric": True}, "ok", None),
    ("find_geometry", {"target": "SHole", "kind": "planar_face", "nearest_to": [620, 0, 20], "max_results": 1}, "ok", _fg("sh_sheet")),
    ("find_geometry", {"target": "SHole", "kind": "cylinder_face", "nearest_to": [620, 0, 20], "max_results": 1}, "ok", _fg("sh_cutter")),
    # the cell bookkeeping is the read-back: at least one cell REMOVED (a trim that removed none
    # kept the whole sheet) and a kept area the phantom-cell gate could measure.
    ("surface_trim", lambda c: {"surface": _ctx_get(c, "sh_sheet", "sheet face"), "trim_tool": _ctx_get(c, "sh_cutter", "cylinder cutter")},
     lambda p: len(p.get("cells_removed") or []) >= 1 and (p.get("kept_area") or 0) > 0
     and bool(p.get("result_bodies")), None),
    ("find_geometry", {"target": "SHole", "kind": "planar_face", "nearest_to": [620, 0, 20], "max_results": 1}, "ok", _fg("sh_trimmed")),
    # removing the hole loop FILLS it, so the created faces' area sum is the read-back that the
    # extent grew; an untrim that created nothing leaves area_after at or below area_before.
    ("surface_untrim", lambda c: {"faces": [_ctx_get(c, "sh_trimmed", "trimmed sheet face")], "loop_type": "internal"},
     lambda p: p.get("extent_grew") is True and p.get("faces_created", 0) >= 1
     and p["area_after"] > p["area_before"], None),
    # RULED surfaces off a rim edge. A line extruded as a surface gives a vertical sheet whose top
    # rim (z=30) is the seed every ruled beat leaves from; each type builds a DIFFERENT surface off
    # that same edge, so the payload's own ruled_type/direction is what separates them.
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "Ruled", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "RuledS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 900, "y1": 0,
                                           "x2": 940, "y2": 0}],
                             "sketch_name": "RuledS"}, "ok", None),
    ("surface_extrude", {"sketch_name": "RuledS", "distance": 30}, "ok", None),
    ("find_geometry", {"target": "Ruled", "kind": "line_edge", "nearest_to": [920, 0, 30],
                       "max_results": 1}, "ok", _fg("ruled_edge")),
    # tangent continues the parent face's own plane past the rim, landing ONE new open body.
    ("surface_create_ruled", lambda c: {"edges": [_ctx_get(c, "ruled_edge", "the rim edge")],
                                        "distance": 15, "ruled_type": "tangent"},
     lambda p: p.get("is_solid") is False and len(p.get("result_bodies", [])) == 1
     and p.get("ruled_type") == "tangent", None),
    # normal stands perpendicular to that same face - a different surface off the same seed edge.
    ("surface_create_ruled", lambda c: {"edges": [_ctx_get(c, "ruled_edge", "the rim edge")],
                                        "distance": 15, "ruled_type": "normal"},
     lambda p: p.get("is_solid") is False and p.get("ruled_type") == "normal", None),
    # direction sweeps along an ENTITY: a world axis resolves to the component's origin axis, which
    # is what createInput consumes (a direction VECTOR cannot be handed to it).
    ("surface_create_ruled", lambda c: {"edges": [_ctx_get(c, "ruled_edge", "the rim edge")],
                                        "distance": 15, "ruled_type": "direction", "direction": "z"},
     lambda p: p.get("direction") == "z-axis" and p.get("is_solid") is False, None),
    # angle_deg reads back in DEGREES off the feature's own ModelParameter (radians at the API).
    ("surface_create_ruled", lambda c: {"edges": [_ctx_get(c, "ruled_edge", "the rim edge")],
                                        "distance": 15, "angle_deg": 20},
     lambda p: abs(p.get("angle_deg", 0) - 20) < 1e-6, None),
    # a direction entity with a type that ignores it is refused: the payload could otherwise claim
    # tangent while a Direction surface was built.
    ("surface_create_ruled", lambda c: {"edges": [_ctx_get(c, "ruled_edge", "the rim edge")],
                                        "distance": 15, "ruled_type": "tangent", "direction": "z"},
     "refused", None),
    # the Direction type with no entity: Fusion itself refuses to build the input.
    ("surface_create_ruled", lambda c: {"edges": [_ctx_get(c, "ruled_edge", "the rim edge")],
                                        "distance": 15, "ruled_type": "direction"}, "refused", None),
    # ruling off a SOLID box edge - the draft-check case. The feature's body collection holds the
    # box AND the new sheet, so a handler publishing that collection raw would report the box as
    # created and read is_solid=true off it: the new sheet alone is the result.
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "RuledSolid", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "RuledSolidS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 980, "y1": 200,
                                           "x2": 1020, "y2": 240}],
                             "sketch_name": "RuledSolidS"}, "ok", None),
    ("model_extrude", {"sketch_name": "RuledSolidS", "profile_index": 0, "distance": 20}, _extruded, None),
    ("find_geometry", {"target": "RuledSolid", "kind": "line_edge", "nearest_to": [1000, 200, 20],
                       "max_results": 1}, "ok", _fg("ruled_solid_edge")),
    ("surface_create_ruled", lambda c: {"edges": [_ctx_get(c, "ruled_solid_edge", "a box top edge")],
                                        "distance": 15, "ruled_type": "tangent"},
     lambda p: p.get("is_solid") is False and len(p.get("result_bodies", [])) == 1
     and "Body1" not in p.get("result_bodies", []), None),
    # back to the component that was active before this cameo, so the ones after it nest as before.
    ("design_activate_component", {"occurrence": "SHole:1"}, "ok", None),
    # split / unstitch / stitch / base-feature / arrange / compute-holder.
    ("model_create_component", {"name": "Spl", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "Sp1"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 400, "y1": 200,
                                           "x2": 440, "y2": 240}],
                             "sketch_name": "Sp1"}, "ok", None),
    ("model_extrude", {"sketch_name": "Sp1", "profile_index": 0, "distance": 20}, _extruded, None),
    ("model_construction", {"kind": "plane", "plane": "xz", "offset": 220, "name": "SplMid"},
     _datum_plane("xz"), None),
    ("find_geometry", {"target": "Spl", "kind": "planar_face", "nearest_to": [420, 220, 20], "max_results": 1}, "ok", _fg("spl_body")),
    ("model_split", lambda c: {"split": "body", "target": _ctx_get(c, "spl_body", "split body"), "split_plane": "SplMid"}, _split_bodies, None),
    ("model_create_component", {"name": "Stc", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "St1"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 500, "y1": 200,
                                           "x2": 520, "y2": 220}],
                             "sketch_name": "St1"}, "ok", None),
    ("model_extrude", {"sketch_name": "St1", "profile_index": 0, "distance": 10}, _extruded, None),
    ("find_geometry", {"target": "Stc", "kind": "planar_face", "nearest_to": [510, 210, 10], "max_results": 1}, "ok", _fg("stc_body")),
    ("model_unstitch", lambda c: {"target": _ctx_get(c, "stc_body", "unstitch body"), "chain": False}, _unstitched, None),
    ("find_geometry", {"target": "Stc", "kind": "planar_face", "nearest_to": [510, 210, 0], "max_results": 1}, "ok", _fg("stc_f1")),
    ("find_geometry", {"target": "Stc", "kind": "planar_face", "nearest_to": [500, 210, 5], "max_results": 1}, "ok", _fg("stc_f2")),
    ("model_stitch", lambda c: {"bodies": [_ctx_get(c, "stc_f1", "stitch a"), _ctx_get(c, "stc_f2", "stitch b")]}, _stitched, None),
    ("model_base_feature", {"action": "start", "base_feature": "BF1"}, _base_feature_open, None),
    ("model_base_feature", {"action": "finish", "base_feature": "BF1"}, _base_feature_closed, None),
] + [
    # compute_holder needs a body + a cyl-face axis + a planar end-datum.
    ("model_create_component", {"name": "HolderPart", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "HP1"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 600, "y1": 200,
                                           "x2": 640, "y2": 220}],
                             "sketch_name": "HP1"}, "ok", None),
    ("model_extrude", {"sketch_name": "HP1", "profile_index": 0, "distance": 10}, _extruded, None),
    ("find_geometry", {"target": "HolderPart", "kind": "planar_face", "nearest_to": [620, 210, 10], "max_results": 1}, "ok", _fg("hp_top")),
    # hole points ride the face's LOCAL frame = the model origin projected onto the face, so
    # on-pad coordinates are the world x,y (same measured fact as the FeatureCameo hole).
    ("model_hole", lambda c: {"face": _ctx_get(c, "hp_top", "holder top"), "hole_type": "simple", "diameter": "4 mm", "extent": "blind", "depth": "8 mm", "points": [[605, 205, 0]]}, _drilled(1), None),
    ("find_geometry", {"target": "HolderPart", "kind": "planar_face", "max_results": 1}, "ok", _fg("hp_body")),
    ("find_geometry", {"target": "HolderPart", "kind": "cylinder_face", "radius": 2, "max_results": 1}, "ok", _fg("hp_axis")),
    ("find_geometry", {"target": "HolderPart", "kind": "planar_face", "nearest_to": [620, 210, 10], "max_results": 1}, "ok", _fg("hp_datum")),
    ("model_compute_holder", lambda c: {"body": _ctx_get(c, "hp_body", "holder body"), "axis": _ctx_get(c, "hp_axis", "holder axis"), "end_datum": _ctx_get(c, "hp_datum", "holder datum")}, _holder_computed, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
]

# ACT 7b: NESTING - model_arrange as a FUNCTION of its boundary. It runs after the
# parametric resize on purpose: the solver restructures the parts it nests under new
# Envelope occurrences, and that is not a thing to hand to an act that recomputes the
# whole assembly.
_NESTING = _box("ArrP1", ox=200, oy=350) + [
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    # THREE DIFFERENT shapes to nest, not two of the same box: a square pad, a long bar and a disc.
    # A nest that only ever sees one footprint proves nothing about the solver.
    ("model_create_component", {"name": "ArrP2", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "ArrP2S"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 260, "y1": 350,
                                           "x2": 320, "y2": 368}],
                             "sketch_name": "ArrP2S"}, "ok", None),
    ("model_extrude", {"sketch_name": "ArrP2S", "profile_index": 0, "distance": 10}, _extruded, None),
    ("model_create_component", {"name": "ArrP3", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "ArrP3S"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 350, "cy": 359, "radius": 16}],
                             "sketch_name": "ArrP3S"}, "ok", None),
    ("model_extrude", {"sketch_name": "ArrP3S", "profile_index": 0, "distance": 10}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    # a SECOND bar, so the nest carries repeats as well as variety - four shapes from three
    # components. The instance number is Fusion's to pick, so it is read back, never predicted.
    ("design_add_instance", {"component": "ArrP2", "x": 0, "y": -35, "units": "mm"},
     lambda p: p.get("created") is True and str(p.get("full_path", "")).startswith("ArrP2:"),
     ("arr_bar2", lambda p: p["full_path"])),
    # The boundary is a HEXAGON, not a rectangle: a true-shape nest against a slanted wall is the
    # case a box boundary cannot show. Its six lines are line:0..line:5, which is what the reshape
    # below scales.
    ("sketch_create", {"plane": "xy", "name": "ArrB"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "polygon", "cx": 300, "cy": 500,
                                           "radius": 120, "sides": 6}],
                             "sketch_name": "ArrB"},
     lambda p: p["results"][0].get("curves_added") == 6, None),
    _watch(["ArrB", "ArrP1:1", "ArrP2:1", "ArrP3:1"]),
    # TRUE-SHAPE, because the boundary is a hexagon: the rectangular solver nests bounding boxes and
    # refuses a non-rectangular envelope outright (ARRANGE_ERROR_ENVELOPE_INVALIDRECTANGULAR), which
    # is exactly what a slanted wall is for. The solver places COPIES under an Envelope occurrence
    # and leaves the named inputs where they were, so the nested part is picked out of what the call
    # PUBLISHED - and it is that copy the reshape below is measured on.
    ("model_arrange", lambda c: {"boundary_sketch": "ArrB",
                                 "shapes": ["ArrP1:1", "ArrP2:1",
                                            _ctx_get(c, "arr_bar2", "the second bar"), "ArrP3:1"],
                                 "solver": "true_shape", "spacing": 5},
     _arranged(4), ("nest_disc", lambda p: next(o for o in p["new_occurrences"]
                                                if "+ArrP3:" in o))),
    ("model_inspect", lambda c: {"target": _ctx_get(c, "nest_disc", "the nested disc")},
     _extent_measured, ("nest_y0", _recall("nest_y0", lambda p: p["center"]["y"]))),
    _dwell(2.0),
    # RESHAPE the boundary: the Arrange feature RECOMPUTES off its boundary sketch, so the nest is a
    # FUNCTION of the envelope rather than a one-time placement. Solving again would not show this -
    # a second identical arrange stacks another coincident copy set (measured, and the tool says so).
    # The proof is the nested disc having MOVED, read back off its own bounding box.
    ("sketch_move", {"sketch_name": "ArrB",
                     "entities": "line:0,line:1,line:2,line:3,line:4,line:5",
                     "scale_factor": 0.65, "center_x": 300, "center_y": 500},
     lambda p: len(p.get("moved_entities") or []) == 6 and not p.get("unmoved_entities"), None),
    ("model_inspect", lambda c: {"target": _ctx_get(c, "nest_disc", "the nested disc")},
     lambda p: _measured("the nest re-solved off the smaller boundary",
                         {"y_before": _RECALL.get("nest_y0"),
                          "y_now": p.get("center", {}).get("y")},
                         abs(p["center"]["y"] - _RECALL["nest_y0"]) > 1.0), None),
    _dwell(2.0),
]


# ACT 7: MESH - a scratch solid becomes a mesh, then the mesh family works it (one mesh per op).
_MESH = [
    ("model_create_component", {"name": "Msh", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "MshS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 200, "y1": 300,
                                           "x2": 220, "y2": 320}],
                             "sketch_name": "MshS"}, "ok", None),
    # FRAME BEFORE THE FIRST BODY, on the sketch that is about to become one. The automatic camera
    # row lands on a chunk's first body, which means the body appears while the camera is still on
    # whatever the previous act was doing - and this act's sketch was drawn back in the sketch phase,
    # so nothing has brought the camera here since. Framing the sketch first is what makes the mesh
    # source appear IN shot instead of somewhere off screen.
    _watch("MshS"),
    ("model_extrude", {"sketch_name": "MshS", "profile_index": 0, "distance": 10}, _extruded, None),
    # THE ONE FRAME THE WHOLE FAMILY PLAYS IN. Every mesh below is cast from a body inside Msh, and
    # none of those steps makes a sketch or a component, so the framing pass adds no row of its own
    # and nothing moves the camera off this shot.
    _watch("Msh:1"),
    ("find_geometry", {"target": "Msh", "kind": "planar_face", "nearest_to": [210, 310, 10], "max_results": 1}, "ok", _fg("msh_body")),
    ("model_construction", {"kind": "plane", "plane": "xy", "offset": 5, "name": "MshMid"},
     _datum_plane("xy"), None),
    ("sketch_create", {"plane": "xy", "name": "MshCyl"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 260, "cy": 360,
                                           "radius": 15}],
                             "sketch_name": "MshCyl"}, "ok", None),
    ("model_extrude", {"sketch_name": "MshCyl", "profile_index": 0, "distance": 20}, _extruded, None),
    ("find_geometry", {"target": "Msh", "kind": "cylinder_face", "nearest_to": [260, 360, 10], "max_results": 1}, "ok", _fg("cyl_body")),
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "cyl_body", "cyl body"), "name": "MRED", "quality": "high"}, "ok", None),
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "msh_body", "box body"), "name": "MA", "quality": "low"}, "ok", None),
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "msh_body", "box body"), "name": "MC", "quality": "low"}, "ok", None),
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "msh_body", "box body"), "name": "MD", "quality": "low"}, "ok", None),
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "msh_body", "box body"), "name": "ME", "quality": "low"}, "ok", None),
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "msh_body", "box body"), "name": "MF", "quality": "low"}, "ok", None),
    ("mesh_get", {"target": "Msh"}, "ok", None),
    # MA is a box cast to mesh, measured to segment 1 -> 6 groups under 'fast', so THIS row's
    # generation must move the count - a payload reporting no movement here is a generation that
    # did nothing. (The tool passes no verdict of its own: one flat region segments into one group.)
    ("mesh_generate_face_groups", {"mesh": "MA", "method": "fast"},
     lambda p: (p["generated"] is True and p["changed"] is True
                and p["face_group_count"] > p["face_group_count_before"]), None),
    # the converted bodies are the component's BRep census differenced across the add; a row with no
    # handle is a body the next tool cannot address, and 'design_mode' is read before the scope opens.
    ("mesh_to_brep", {"mesh": "MA", "method": "faceted", "operation": "base_feature"},
     lambda p: (bool(p["brep_bodies"]) and all(b["handle"] for b in p["brep_bodies"])
                and p["design_mode"] == "parametric"), None),
    # both triangle counts are read off the model; 'reduced_pct' is published only where both read,
    # so an after-count that would not read is caught here rather than passing as a reduce.
    ("mesh_reduce", {"mesh": "MRED", "target": "proportion", "value": 50},
     lambda p: (p["after"]["triangle_count"] < p["before"]["triangle_count"]
                and (p.get("reduced_pct") or 0) > 0), None),
    # 'density' is set-then-read-back off the input (a build that drops it refuses), and the
    # before/after triangle counts are read off the model - 'changed' is null when either count
    # could not be read at all, which is a remesh nothing was measured about.
    ("mesh_remesh", {"mesh": "MC", "density": 1},
     lambda p: p.get("density_applied") == 1 and (p["after"]["triangle_count"] or 0) > 0
     and p.get("changed") is not None, None),
    # the cut's own effect evidence, mirrored into the receipt: the triangle count moved, or (a
    # fill that replaces as many triangles as it removed) the mesh's area/volume did.
    ("mesh_plane_cut", {"mesh": "MD", "plane": "MshMid", "cut_type": "trim"},
     lambda p: p.get("fill") == "minimal" and p.get("triangles_before") and p.get("triangles_after")
     and (p["triangles_after"] != p["triangles_before"]
          or p.get("volume_after_cm3") != p.get("volume_before_cm3")), None),
    # a parametric mesh write reports the MODE it ran in and the base feature it opened to run
    # there: the scope is what a parametric design requires, and the payload names both.
    ("mesh_combine", {"target": "ME", "tools": ["MF"], "operation": "join"},
     lambda p: p.get("design_mode") == "parametric" and bool(p.get("base_feature")), None),
    # a pristine scratch mesh deleted with the survivor check: the payload's own claim is the
    # re-scan. A mesh another feature already transformed (e.g. the plane-cut MD) carries a
    # different lineage; this beat exercises the plain-delete contract.
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "msh_body", "box body"), "name": "MDEL",
                                "quality": "low"}, "ok", None),
    # 'deleted_via' names the branch the design's own mode chose - a parametric delete leaves an
    # undoable MeshRemoveFeature - and 'remaining_meshes' is the design-wide re-scan behind it.
    ("mesh_delete", {"mesh": "MDEL"},
     lambda p: (p["deleted"] == "MDEL" and p["deleted_via"] == "meshRemoveFeatures"
                and isinstance(p["remaining_meshes"], int)), None),
    # execute() answers true while writing nothing, so the file's own size is the read-back - and
    # it is the same file the mesh_insert beat below re-imports.
    ("mesh_export", {"target": "MA", "file_path": EXPORT_DIR + "/eval_mesh", "format": "stl"},
     lambda p: (p.get("size_bytes") or 0) > 0 and p.get("file_exists") is True, None),
    # THE RE-IMPORT GETS ITS OWN COMPONENT, and the mesh act goes back to Msh afterwards.
    # mesh_insert does NOT land the mesh where the file's own geometry sits: measured on the live
    # document, a mesh exported from (770,875) came back at (30,34) - near the world origin, a metre
    # from every other body in Msh. Inside Msh that one stray body stretched the component's
    # bounding box to 815 x 916 mm, so every camera row framing 'Msh:1' fitted THAT instead of the
    # 75 mm of mesh work, and the whole mesh act was watched from the far zoom.
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "MshIn", "activate": True}, _made_component, None),
    # units='mm' because that is what the FILE holds: the mesh_export beat above named no unit, and
    # mesh_export's stl_units default is mm. Stated rather than left to mesh_insert's own default,
    # so the round trip below pins mesh_export's default alone - reading it back at mesh_insert's
    # default too would pass on any pair of defaults that happen to match. Importing the file
    # at the wrong unit divides every coordinate by 25.4, and a mesh a fortieth of its size lands a
    # fortieth of its distance from the origin too - measured, near the world origin and inside
    # whatever is parked there, not out on the field where it was exported from. The size read-back
    # below is what makes that a failure instead of a surprise.
    # the row is the imported body read back: the name it answers to (a dedupe leaves the model_inspect
    # below naming a mesh that is not this one) and a triangle count off the mesh itself.
    ("mesh_insert", {"file_path": EXPORT_DIR + "/eval_mesh.stl", "name": "MshIns",
                     "units": "mm"},
     lambda p: (len(p["bodies"]) == 1 and p["bodies"][0]["name"] == "MshIns"
                and (p["bodies"][0]["triangle_count"] or 0) > 0
                and p.get("name_applied") is True
                and "rename_warning" not in p), None),
    # the round trip measured END TO END: the re-imported mesh is the size of the mesh that was
    # written - measured 74.99 x 74.99 x 20.0 on a finished run. A size, not a position: the layout
    # moves the bench, and 25.4 is the only thing this is looking for.
    ("model_inspect", {"target": "MshIns"}, _mesh_round_trip(75.0, 20.0), None),
    ("design_activate_component", {"occurrence": "Msh:1"}, "ok", None),
    # repair on a HEALTHY mesh: nothing of that kind to fix is an honest success, not a failure, and
    # the payload must say so rather than claim a repair. Then a rebuild, whose density is read back
    # off the feature's own parameter - the request is never echoed.
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "msh_body", "box body"), "name": "MFIX",
                                "quality": "low"}, "ok", None),
    ("mesh_repair", {"mesh": "MFIX", "repair_type": "one_touch_fix"},
     lambda p: p.get("repaired") is True and p.get("watertight") is True, None),
    ("mesh_repair", {"mesh": "MFIX", "repair_type": "rebuild", "rebuild_method": "fast",
                     "density": 32},
     lambda p: p.get("density") == 32.0 and "density_unverified" not in p, None),
    # the rest of the rebuild vocabulary on the same mesh - each method re-triangulates it a
    # different way, and each row reads its own density back off the feature's ModelParameter rather
    # than echoing the request, so a method that quietly fell back to another is visible.
    ("mesh_repair", {"mesh": "MFIX", "repair_type": "rebuild",
                     "rebuild_method": "preserve_sharp_edges", "density": 40},
     _rebuilt("preserve_sharp_edges", 40.0), None),
    # 'offset' is accepted by the accurate method ALONE - it is the deviation that method solves to.
    ("mesh_repair", {"mesh": "MFIX", "repair_type": "rebuild", "rebuild_method": "accurate",
                     "density": 48, "offset": 0.2}, _rebuilt("accurate", 48.0), None),
    ("mesh_repair", {"mesh": "MFIX", "repair_type": "rebuild", "rebuild_method": "blocky",
                     "density": 24}, _rebuilt("blocky", 24.0), None),
    ("mesh_repair", {"mesh": "MFIX", "repair_type": "rebuild", "rebuild_method": "adaptive",
                     "density": 32}, _rebuilt("adaptive", 32.0), None),
    ("mesh_repair", {"mesh": "MFIX", "repair_type": "rebuild",
                     "rebuild_method": "adaptive_preserve_sharp_edges", "density": 32},
     _rebuilt("adaptive_preserve_sharp_edges", 32.0), None),
    # the offset/method pairing, refused rather than dropped, on the method it does not belong to.
    ("mesh_repair", {"mesh": "MFIX", "repair_type": "rebuild", "rebuild_method": "blocky",
                     "density": 24, "offset": 0.2}, "refused", None),
    # 'wrap' shrink-wraps the mesh closed. It is not a rebuild, so it takes none of the rebuild
    # knobs - handing it one is refused by name.
    ("mesh_repair", {"mesh": "MFIX", "repair_type": "wrap"},
     lambda p: p.get("repaired") is True and p.get("repair_type") == "wrap", None),
    ("mesh_repair", {"mesh": "MFIX", "repair_type": "close_holes", "density": 32}, "refused", None),
    # A repair that finds nothing of its kind is an honest success, and the payload has to say so
    # rather than claim a repair - 'changed' empty is that statement. A mesh straight out of
    # save_as_mesh is NOT that fixture: the first stitch_and_remove on it measurably moves the
    # counts (it has duplicate vertices to weld), so the no-op case is the SECOND call, once the
    # first has done the welding. That also makes the beat an idempotence check.
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "msh_body", "box body"), "name": "MSTITCH",
                                "quality": "low"}, "ok", None),
    ("mesh_repair", {"mesh": "MSTITCH", "repair_type": "stitch_and_remove"},
     lambda p: p.get("repaired") is True, None),
    ("mesh_repair", {"mesh": "MSTITCH", "repair_type": "stitch_and_remove"}, _repair_no_op, None),
    # mesh_shell hollows the SAME body in place and re-triangulates it: the payload's before/after
    # counts and the volume DROP are the verdict, and the thickness is read off the feature.
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "msh_body", "box body"), "name": "MSHL",
                                "quality": "low"}, "ok", None),
    # 'hollowed' needs BOTH a volume drop and a body that still reads watertight - a shell that lost
    # the closure reports the same drop for the opposite reason, so the flag pair is the verdict.
    ("mesh_shell", {"mesh": "MSHL", "thickness": 2, "units": "mm"},
     lambda p: p.get("hollowed") is True and abs(p.get("thickness", 0) - 2.0) < 1e-6
     and p.get("watertight") is True
     and p.get("volume_change", 0) < 0 and "volume" in (p.get("changed") or []), None),
    ("mesh_shell", {"mesh": "MSHL", "thickness": -2}, "refused", None),
    # a thickness thicker than half the thinnest wall does NOT quietly cut through: the platform
    # refuses the shell outright ([F50] - measured on a closed cube), and the tool hands that
    # compute failure on by name instead of reporting a hollow that never happened. The box is
    # 10 mm through its thinnest axis, so 6 mm is past the half-wall.
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "msh_body", "box body"), "name": "MSHL2",
                                "quality": "low"}, "ok", None),
    ("mesh_shell", {"mesh": "MSHL2", "thickness": 6, "units": "mm"},
     _refused("MESH_FAILED_HOLLOW"), None),
    # mesh_smooth: the node coordinates move. nodes_moved > 0 is the gate - the counts holding
    # still is measured on a 12-triangle box only, so it is NOT asserted here.
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "cyl_body", "cyl body"), "name": "MSMO",
                                "quality": "high"}, "ok", None),
    ("mesh_smooth", {"mesh": "MSMO", "smoothness": 0.05},
     lambda p: p.get("nodes_moved", 0) > 0 and abs(p.get("smoothness", 0) - 0.05) < 1e-6, None),
    ("mesh_smooth", {"mesh": "MSMO", "smoothness": 1.5}, "refused", None),
    # A 'merge' combine of two DISJOINT meshes yields ONE body holding TWO shells, which the
    # separate then takes apart: measured 24 triangles in, two 12-triangle pieces out.
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "msh_body", "box body"), "name": "MSEPA",
                                "quality": "low"}, "ok", None),
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "cyl_body", "cyl body"), "name": "MSEPB",
                                "quality": "low"}, "ok", None),
    ("mesh_combine", {"target": "MSEPA", "tools": ["MSEPB"], "operation": "merge"}, "ok", None),
    ("mesh_separate", {"mesh": "MSEPA"},
     lambda p: p.get("piece_count", 0) >= 2 and len(p.get("pieces") or []) >= 2, None),
    # mesh_reverse_normal: the signed volume changes sign; is_closed / is_oriented do not move.
    ("save_as_mesh", lambda c: {"body": _ctx_get(c, "msh_body", "box body"), "name": "MREV",
                                "quality": "low"}, "ok", None),
    ("mesh_reverse_normal", {"mesh": "MREV"},
     lambda p: p.get("reversed") is True
     and (p.get("volume_sign_flipped") is True or p.get("normals_negated") is True), None),
    # MeshBody.name IS settable ([F37]): the rename lands on the mesh kind, and a FRESH fetch of the
    # component's meshes - not the wrapper the write held - is what proves it stuck.
    ("design_set_name", {"target": "MREV", "new_name": "MeshRenamed"},
     lambda p: p.get("kind") == "mesh" and p.get("name") == "MeshRenamed"
     and p.get("previous_name") == "MREV", None),
    ("mesh_get", {"target": "Msh", "max_results": 100},
     lambda p: any(m.get("name") == "MeshRenamed" for m in (p.get("meshes") or [])), None),
    # THE QUALIFIED ADDRESS, on a component this document places ONCE. Fusion names the first body
    # of every component 'Body1', so the bare name reaches several of them and is refused with the
    # candidate list; the '<occurrence>:<body>' spelling that refusal offers picks Msh's own.
    ("save_as_mesh", {"body": "Body1", "name": "MQUAL", "quality": "low"},
     _refused("is ambiguous - it names",
              "qualified '<occurrence-or-component>:<body>' names"), None),
    ("save_as_mesh", {"body": "Msh:1:Body1", "name": "MQUAL", "quality": "low"},
     lambda p: _measured("the qualified source address reached Msh's own body",
                         {"component": p.get("component"), "source_body": p.get("source_body"),
                          "name": p.get("name"), "triangle_count": p.get("triangle_count")},
                         p.get("component") == "Msh" and p.get("source_body") == "Body1"
                         and p.get("name") == "MQUAL" and _num(p.get("triangle_count"))
                         and p["triangle_count"] > 0), None),
    # ...and the MESH under the same spelling. A mesh belongs to the COMPONENT rather than to a
    # placement, so the address is answered through the placed component - and the facts read back
    # are the mesh's own, not the source solid's.
    ("model_inspect", {"target": "Msh:1:MQUAL"},
     lambda p: _measured("the mesh's own facts under its '<occurrence>:<mesh>' address",
                         {"kind": p.get("kind"), "name": p.get("name"),
                          "triangle_count": p.get("triangle_count"), "volume": p.get("volume"),
                          "is_closed": p.get("is_closed")},
                         p.get("kind") == "mesh" and p.get("name") == "MQUAL"
                         and _num(p.get("triangle_count")) and p["triangle_count"] > 0), None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
]
