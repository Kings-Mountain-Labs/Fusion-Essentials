# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""ACT rows: the machined part's parametric skeleton, and the sketch tools on scratch sketches.

Both run before anything is solid - which is the order the work is done in - so these rows read
sketch geometry and user parameters, never bodies.
"""

from verify_core import (
    EXPORT_DIR, SVG96_PATH, SVG_PATH, _datum_plane, _dim_measures, _extruded, _made_component,
    _param_added, _param_favorited, _params_listed, _refused, _svg96_extent, _watch_all)
from verify_layout import _px, _py


# --- ACT 1: PARAMETERS + THE PART'S SKETCHES - the bracket, drawn before anything is solid -----
_SKELETON = [
    # ONE driving length, two independent extents (width, height), and the features derived from
    # the section they are cut into. Each derived parameter's expected value is what the
    # DESIGN evaluates the expression to off the 120 mm driver above it - so the read-back proves
    # Fusion resolved the reference, not that the expression text was accepted (the CAM parameter
    # store accepts a bogus name unevaluated).
    ("param_add", {"name": "PartLen", "expression": "120 mm",
                   "comment": "The driving length: the pocket length and corner radius follow it"},
     _param_added("PartLen", 120), None),
    # The part's OTHER two extents are their own parameters, not ratios of the length: a bracket
    # that is made longer is not made wider and taller with it, and the resize act reads exactly
    # that - one axis follows the driver while the other two hold.
    ("param_add", {"name": "PartWid", "expression": "80 mm"}, _param_added("PartWid", 80), None),
    ("param_add", {"name": "PartHt", "expression": "40 mm"}, _param_added("PartHt", 40), None),
    # Everything the part is FEATURED with derives from the height or the width it is cut into.
    ("param_add", {"name": "StepDrop", "expression": "PartHt / 4",
                   "comment": "How far the low half of the stepped top sits under the high half"},
     _param_added("StepDrop", 10), None),
    ("param_add", {"name": "BoreDia", "expression": "PartHt * 0.3"},
     _param_added("BoreDia", 12), None),
    ("param_add", {"name": "PocketLen", "expression": "PartLen / 4"},
     _param_added("PocketLen", 30), None),
    ("param_add", {"name": "PocketWid", "expression": "PartWid * 0.55"},
     _param_added("PocketWid", 44), None),
    ("param_add", {"name": "PocketDepth", "expression": "PartHt * 0.4"},
     _param_added("PocketDepth", 16), None),
    ("param_add", {"name": "PocketRad", "expression": "PartLen / 15",
                   "comment": "Pocket corner radius - what a cutter has to clear the corner with"},
     _param_added("PocketRad", 8), None),
    ("param_add", {"name": "BossDia", "expression": "PartHt / 2"},
     _param_added("BossDia", 20), None),
    ("param_add", {"name": "MountDia", "expression": "PartHt * 0.15"},
     _param_added("MountDia", 6), None),
    # A RULE, not a ratio: the edge break scales with the section but stops at a floor, so thinning
    # the part thins the break only until it reaches one that can still be cut.
    ("param_add", {"name": "EdgeBreak", "expression": "max(PartHt * 0.075; 2 mm)",
                   "comment": "Break on a handled edge - floored so it stays cuttable"},
     _param_added("EdgeBreak", 3), None),
    ("param_set_favorite", {"name": "PartLen", "favorite": True}, _param_favorited("PartLen"), None),
    ("param_get", {}, _params_listed("PartLen", "PartWid", "PartHt", "StepDrop", "BoreDia",
                                     "PocketLen", "PocketWid", "PocketDepth", "PocketRad",
                                     "BossDia", "MountDia", "EdgeBreak"), None),
    # THE PART, cast as its own component at the ROOT: the vise is built around it and the CAM setup
    # names it, so it is never nested inside anything that could carry it somewhere else.
    ("model_create_component", {"name": "Bracket", "activate": True}, _made_component, None),
    # the SHARED SKELETON on the root: two in-plane axes (construction) + the height as a 3D line.
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    # 'sketch_name' is the name READ BACK off the sketch (Fusion dedupes a taken one, and every step
    # after this names 'Skeleton'), and the frame's normal is computed from the created sketch's own
    # axes - an XY sketch's is the Z axis.
    ("sketch_create", {"plane": "xy", "name": "Skeleton"},
     lambda p: (p["sketch_name"] == "Skeleton" and bool(p["plane"])
                and abs(p["frame"]["normal"][2]) > 0.999), None),
    # 'curves_added' is the LINE collection's own delta, so a single line reads 1 - the floor the
    # composite kinds (rectangle 4, polygon 6, slot 3) are counted against.
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": -70, "y1": 0, "x2": 70, "y2": 0,
                                           "is_construction": True}],
                             "sketch_name": "Skeleton"},
     lambda p: p["results"][0].get("curves_added") == 1, None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 0, "y1": -70, "x2": 0, "y2": 70,
                                           "is_construction": True}],
                             "sketch_name": "Skeleton"}, "ok", None),
    # start/end are the created line's own SketchPoints read back in mm: a z that came back at a
    # different scale is the unit conversion, and a zero one is a line flattened onto the plane.
    ("sketch_add_3d_line", {"x1": 0, "y1": 0, "z1": -70, "x2": 0, "y2": 0, "z2": 70,
                            "sketch_name": "Skeleton"},
     lambda p: (abs(p["start"]["z"] + 70) < 1e-6 and abs(p["end"]["z"] - 70) < 1e-6
                and p["end_is_off_plane"] is True), None),
    # the camera has been fitted to an EMPTY document since the overture - the skeleton is the
    # first thing in the world worth looking at.
    _watch_all(),
    ("sketch_constrain", {"constraints": [{"constraint": "horizontal", "entity_one": "line:0"}],
                          "sketch_name": "Skeleton"}, "ok", None),
    ("sketch_dimension", {"dimensions": [{"dim_type": "distance", "entity_one": "point:0",
                                          "entity_two": "point:1", "value": "PartLen"}],
                          "sketch_name": "Skeleton"}, "ok", None),
    ("sketch_get", {"sketch_name": "Skeleton"}, "ok", None),
    # draw a helper constraint then delete it - the count drop is the read-back.
    ("sketch_delete_entity", {"sketch_name": "Skeleton", "target": "constraint:0"}, "ok", None),
    # THE BRACKET, drawn: the base footprint, the raised half of the stepped top, the pocket, and
    # the boss on the finished top. Every profile's SPANS are dimensioned to parameters, so the
    # resize act walks all of them; what stays authored is where each feature sits on the block -
    # the boss centre and the pocket's corner - which no dimension here pins.
    # 'active_component' is design.activeComponent read AFTER the activate; the handler's mismatch
    # guard is skipped when that read declines, so a null here is an activation nobody confirmed.
    ("design_activate_component", {"occurrence": "Bracket:1"},
     lambda p: bool(p["active_component"]) and p["active_component"] == p["component"], None),
    ("sketch_create", {"plane": "xy", "name": "BracketBody"}, "ok", None),
    # a rectangle is built BY a SketchLines factory, so its four sides are the delta counted.
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": -60, "y1": -40,
                                           "x2": 60, "y2": 40}],
                             "sketch_name": "BracketBody"},
     lambda p: p["results"][0].get("curves_added") == 4, None),
    # EACH SPAN DIMENSIONED ACROSS ONE EDGE'S OWN TWO ENDPOINTS. A bare 'point:N' is refused as an
    # anchor by sketch_dimension's own contract - point:0 is the sketch ORIGIN - and an
    # origin-anchored span drives a CORNER's distance from the origin, not the width of the
    # rectangle. Anchored on the edge, whichever endpoint the solver frees, the span it leaves IS
    # the parameter. line:0 is the first line addTwoPointRectangle draws (the y1 edge, horizontal)
    # and line:1 the next (the x2 edge, vertical).
    ("sketch_dimension", {"dimensions": [{"dim_type": "horizontal_distance",
                                          "entity_one": "line:0:start", "entity_two": "line:0:end",
                                          "value": "PartLen"}],
                          "sketch_name": "BracketBody"}, "ok", None),
    ("sketch_dimension", {"dimensions": [{"dim_type": "vertical_distance",
                                          "entity_one": "line:1:start", "entity_two": "line:1:end",
                                          "value": "PartWid"}],
                          "sketch_name": "BracketBody"}, "ok", None),
    # the step floor: the low half of the stepped top, and the face the pocket is cut down from.
    ("model_construction", {"kind": "plane", "plane": "xy", "offset": "PartHt - StepDrop",
                            "name": "StepFloor"}, _datum_plane("xy"), None),
    ("sketch_create", {"plane": "StepFloor", "name": "BracketStep"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": -10, "y1": -40,
                                           "x2": 60, "y2": 40}],
                             "sketch_name": "BracketStep"}, "ok", None),
    # The raised half's WIDTH follows the part; its LENGTH stays authored on purpose. A driven
    # length changes the block's span but not which end the solver grows it from, and a step whose
    # length moved with it could take the low shelf out from under the holes drilled into it.
    ("sketch_dimension", {"dimensions": [{"dim_type": "vertical_distance",
                                          "entity_one": "line:1:start", "entity_two": "line:1:end",
                                          "value": "PartWid"}],
                          "sketch_name": "BracketStep"}, "ok", None),
    # THE POCKET IS DRAWN ON ITS OWN FLOOR, not on the face it opens through: the cut then runs
    # UPWARD out of solid material, which is the direction that has a body in it. Measured: the
    # same profile on the step floor cutting down answers '3 : No target body found to cut'.
    ("model_construction", {"kind": "plane", "plane": "xy",
                            "offset": "PartHt - StepDrop - PocketDepth", "name": "PocketFloor"},
     _datum_plane("xy"), None),
    ("sketch_create", {"plane": "PocketFloor", "name": "BracketPocket"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": -50, "y1": -22,
                                           "x2": -20, "y2": 22}],
                             "sketch_name": "BracketPocket"}, "ok", None),
    ("sketch_dimension", {"dimensions": [{"dim_type": "horizontal_distance",
                                          "entity_one": "line:0:start", "entity_two": "line:0:end",
                                          "value": "PocketLen"}],
                          "sketch_name": "BracketPocket"}, "ok", None),
    ("sketch_dimension", {"dimensions": [{"dim_type": "vertical_distance",
                                          "entity_one": "line:1:start", "entity_two": "line:1:end",
                                          "value": "PocketWid"}],
                          "sketch_name": "BracketPocket"}, "ok", None),
    # the boss stands on the FINISHED top, so its plane is the part's full height.
    ("model_construction", {"kind": "plane", "plane": "xy", "offset": "PartHt",
                            "name": "TopFloor"}, _datum_plane("xy"), None),
    ("sketch_create", {"plane": "TopFloor", "name": "BracketBoss"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 45, "cy": 0, "radius": 10}],
                             "sketch_name": "BracketBoss"}, "ok", None),
    ("sketch_dimension", {"dimensions": [{"dim_type": "diameter", "entity_one": "circle:0",
                                          "value": "BossDia"}],
                          "sketch_name": "BracketBoss"}, "ok", None),
    # the engraved nameplate, on the high half of the stepped top.
    ("sketch_create", {"plane": "TopFloor", "name": "NamePlate"}, "ok", None),
    ("sketch_set_text", {"text": "FUSION ESSENTIALS", "sketch_name": "NamePlate", "create": True,
                         "height": 5, "x": -5, "y": -32}, "ok", None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    _watch_all(),                       # the whole part is drawn
]

# --- the sketch TOOLS, as their own act: every beat that needs no solid, so the run draws before
# it builds. What is missing from here is only what cannot be sketched in an empty
# document - a dimension MEASURED to a model face, a text path REFUSED a model edge - and those
# few beats stay in _DETAILS, next to the bodies they read.
_SKETCHWORK = [
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    # sketch_edit_curve: one sketch per action, so no edit can perturb the next.
    ("sketch_create", {"plane": "xy", "name": "EditTrim"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 0, "x2": 700, "y2": 0}],
                             "sketch_name": "EditTrim"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 650, "y1": -50,
                                           "x2": 650, "y2": 50}],
                             "sketch_name": "EditTrim"}, "ok", None),
    ("sketch_edit_curve", {"sketch_name": "EditTrim", "action": "trim", "entity_one": "line:0",
                           "x1": 610, "y1": 0},
     lambda p: [r.get("length") for r in p.get("resulting", [])] == [50.0], None),
    ("sketch_create", {"plane": "xy", "name": "EditExt"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 10,
                                           "x2": 630, "y2": 10}],
                             "sketch_name": "EditExt"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 680, "y1": -20,
                                           "x2": 680, "y2": 40}],
                             "sketch_name": "EditExt"}, "ok", None),
    # extend returns an EMPTY collection on success, so the verdict is the curve's own length:
    # 30 mm reaching the crossing line at x=680 makes it 80.
    ("sketch_edit_curve", {"sketch_name": "EditExt", "action": "extend", "entity_one": "line:0",
                           "x1": 628, "y1": 10},
     lambda p: [r.get("length") for r in p.get("resulting", [])] == [80.0], None),
    ("sketch_create", {"plane": "xy", "name": "EditSplit"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 0, "x2": 700, "y2": 0}],
                             "sketch_name": "EditSplit"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 650, "y1": -50,
                                           "x2": 650, "y2": 50}],
                             "sketch_name": "EditSplit"}, "ok", None),
    # both halves are 50 long and must carry DISTINCT ids - the two pieces share one entityToken,
    # so an id resolved by token would report the same curve twice.
    ("sketch_edit_curve", {"sketch_name": "EditSplit", "action": "split", "entity_one": "line:0",
                           "x1": 650, "y1": 0},
     lambda p: [r.get("length") for r in p.get("resulting", [])] == [50.0, 50.0]
     and len({r.get("id") for r in p.get("resulting", [])}) == 2, None),
    ("sketch_create", {"plane": "xy", "name": "EditCorner"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 0, "x2": 660, "y2": 0}],
                             "sketch_name": "EditCorner"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 660, "y1": 0,
                                           "x2": 660, "y2": 40}],
                             "sketch_name": "EditCorner"}, "ok", None),
    ("sketch_edit_curve", {"sketch_name": "EditCorner", "action": "fillet", "entity_one": "line:0",
                           "x1": 655, "y1": 0, "entity_two": "line:1", "x2": 660, "y2": 5,
                           "radius": 10},
     lambda p: abs((p.get("resulting") or [{}])[0].get("length", 0) - 15.708) < 0.01, None),
    ("sketch_create", {"plane": "xy", "name": "EditChamfer"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 0, "x2": 660, "y2": 0}],
                             "sketch_name": "EditChamfer"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 660, "y1": 0,
                                           "x2": 660, "y2": 40}],
                             "sketch_name": "EditChamfer"}, "ok", None),
    ("sketch_edit_curve", {"sketch_name": "EditChamfer", "action": "chamfer",
                           "entity_one": "line:0", "x1": 655, "y1": 0, "entity_two": "line:1",
                           "x2": 660, "y2": 5, "distance": 8},
     lambda p: abs((p.get("resulting") or [{}])[0].get("length", 0) - 11.3137) < 0.01, None),
    ("sketch_create", {"plane": "xy", "name": "EditOffset"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 0, "x2": 700, "y2": 0}],
                             "sketch_name": "EditOffset"}, "ok", None),
    # the direction point picks the side: above the line offsets to +y
    ("sketch_edit_curve", {"sketch_name": "EditOffset", "action": "offset", "entity_one": "line:0",
                           "x1": 650, "y1": 20, "distance": 15},
     lambda p: p.get("curve_count_after") == 2, None),
    ("sketch_edit_curve", {"sketch_name": "EditOffset", "action": "chamfer", "entity_one": "line:0",
                           "x1": 650, "y1": 0, "entity_two": "line:1", "x2": 650, "y2": 15,
                           "distance": 5}, "refused", None),
    # sketch_move / sketch_copy: a transform is verified by COORDINATES, never by the API's bool -
    # Sketch.move returns true for an entity a constraint held still. A two-line chain on its own
    # clear band carries every beat below.
    ("sketch_create", {"plane": "xy", "name": "XformSrc"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 500,
                                           "x2": 650, "y2": 500}],
                             "sketch_name": "XformSrc"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 650, "y1": 500,
                                           "x2": 650, "y2": 540}],
                             "sketch_name": "XformSrc"}, "ok", None),
    # the copied collection counts the copied ENDPOINTS as well as the curves - 6 entities for a
    # two-line chain - so the target's own curve count is the honest read-back, and the new refs
    # are identity-matched against the target's collections.
    ("sketch_copy", {"sketch_name": "XformSrc", "entities": "line:0,line:1", "dx": 100},
     lambda p: p.get("curve_count_after", 0) - p.get("curve_count_before", 0) == 2
     and p.get("new_curves") == ["line:2", "line:3"]
     and p.get("returned_entity_count") == 6, None),
    # WHERE they landed: a count-only check passes a copy dropped on top of the original, so the
    # copy is read back at its offset position (x 600 -> 700).
    ("sketch_get", {"sketch_name": "XformSrc", "include_entities": True},
     lambda p: any(abs((e.get("start") or {}).get("x", 0) - _px("XformSrc", 700)) < 0.01
                   for e in (p.get("entities") or []) if e.get("type") == "line"), None),
    # move the ORIGINAL: line:0 runs 600->650 at y=500 and must land at 620->670, y=530.
    ("sketch_move", {"sketch_name": "XformSrc", "entities": "line:0", "dx": 20, "dy": 30},
     lambda p: p.get("moved_entities") == ["line:0"] and not p.get("unmoved_entities"), None),
    ("sketch_get", {"sketch_name": "XformSrc", "include_entities": True},
     lambda p: any(abs((e.get("start") or {}).get("x", 0) - _px("XformSrc", 620)) < 0.01
                   and abs((e.get("start") or {}).get("y", 0) - _py("XformSrc", 530)) < 0.01
                   for e in (p.get("entities") or []) if e.get("type") == "line"), None),
    # 180 deg about the line's OWN midpoint: the bounding box is identical afterwards and only the
    # endpoints swap, so the move is seen by the endpoint fingerprint and by nothing coarser.
    ("sketch_move", {"sketch_name": "XformSrc", "entities": "line:0", "rotation_deg": 180,
                     "center_x": 645, "center_y": 530},
     lambda p: p.get("moved_entities") == ["line:0"], None),
    # a cross-sketch copy lands in the TARGET, whose count rises from zero.
    ("sketch_create", {"plane": "xy", "name": "XformDst"}, "ok", None),
    # ONE curve across into an empty target - and the note states the id rule that holds for a COPY:
    # an added curve APPENDS, so the ids already in use keep their entities. Removing a curve is what
    # RENUMBERS (sketch_edit_curve's rule), and saying so here would be wrong for this call.
    ("sketch_copy", {"sketch_name": "XformSrc", "target_sketch": "XformDst",
                     "entities": "line:1", "dx": 0, "dy": -60},
     lambda p: p.get("target_sketch") == "XformDst" and p.get("curve_count_before") == 0
     and p.get("curve_count_after") == 1
     and "APPENDS" in (p.get("note") or "") and "RENUMBER" not in (p.get("note") or ""), None),
    # a mirror asked for as a negative scale, and a transform that asks for nothing: neither runs.
    ("sketch_move", {"sketch_name": "XformDst", "entities": "line:0", "scale_factor": -1},
     "refused", None),
    ("sketch_move", {"sketch_name": "XformDst", "entities": "line:0"}, "refused", None),
    # the same copy inside a COMPONENT sketch - the occurrence-proxy seam. Sketch.copy hands back
    # assembly-context proxies whose own tokens are NOT the landed curves', so a ref can only be
    # named through each proxy's native entity: an empty 'new_curves' beside a count that rose is
    # the seam breaking, and 'new_curves_complete' appears only when refs went missing.
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "CopyComp", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "CompCopyS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1400, "y1": 0,
                                           "x2": 1450, "y2": 0}],
                             "sketch_name": "CompCopyS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1450, "y1": 0,
                                           "x2": 1450, "y2": 40}],
                             "sketch_name": "CompCopyS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1450, "y1": 40,
                                           "x2": 1400, "y2": 40}],
                             "sketch_name": "CompCopyS"}, "ok", None),
    ("sketch_copy", {"sketch_name": "CompCopyS", "entities": "line:0,line:1,line:2", "dy": 60},
     lambda p: p.get("new_curves") == ["line:3", "line:4", "line:5"]
     and p.get("curve_count_after", 0) - p.get("curve_count_before", 0) == 3
     and "new_curves_complete" not in p, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    # autoConstrain takes the whole sketch: a loose rectangle flips is_fully_constrained to true,
    # and the added counts are read off the sketch's own collections.
    # THE CONSTRAINT BENCH - one scratch sketch carrying a spread of constraint kinds, several of
    # them taking TWO operands, each verified by the sketch's own constrained-state read rather than
    # by the call returning ok. Geometric constraints are what make a sketch a MODEL rather than a
    # picture, and a surface that only ever ran 'auto' has never shown that.
    ("sketch_create", {"plane": "xy", "name": "ConBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 480,
                                           "x2": 680, "y2": 486}],
                             "sketch_name": "ConBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 500,
                                           "x2": 676, "y2": 512}],
                             "sketch_name": "ConBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 700, "y1": 480,
                                           "x2": 706, "y2": 530}],
                             "sketch_name": "ConBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 640, "cy": 545, "radius": 14}],
                             "sketch_name": "ConBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 676, "cy": 545, "radius": 9}],
                             "sketch_name": "ConBench"}, "ok", None),
    # HORIZONTAL takes one operand; the rest below each take two.
    ("sketch_constrain", {"constraints": [{"constraint": "horizontal", "entity_one": "line:0"}],
                          "sketch_name": "ConBench"},
     lambda p: p["results"][0].get("applied") == "horizontal", None),
    ("sketch_constrain", {"constraints": [{"constraint": "parallel", "entity_one": "line:1",
                                           "entity_two": "line:0"}],
                          "sketch_name": "ConBench"},
     lambda p: p["results"][0].get("applied") == "parallel", None),
    ("sketch_constrain", {"constraints": [{"constraint": "perpendicular", "entity_one": "line:2",
                                           "entity_two": "line:0"}],
                          "sketch_name": "ConBench"},
     lambda p: p["results"][0].get("applied") == "perpendicular", None),
    ("sketch_constrain", {"constraints": [{"constraint": "equal", "entity_one": "circle:0",
                                           "entity_two": "circle:1"}],
                          "sketch_name": "ConBench"},
     lambda p: p["results"][0].get("applied") == "equal", None),
    # SYMMETRY takes three: the pair, and the line they mirror across.
    ("sketch_constrain", {"constraints": [{"constraint": "symmetry", "entity_one": "circle:0",
                                           "entity_two": "circle:1",
                                           "symmetry_line": "line:2"}],
                          "sketch_name": "ConBench"},
     lambda p: p["results"][0].get("applied") == "symmetry"
     and p["results"][0].get("symmetry_line") == "line:2", None),
    # MIDPOINT puts a line's own end at the middle of another - a point-and-curve pair.
    ("sketch_constrain", {"constraints": [{"constraint": "midpoint", "entity_one": "line:1:start",
                                           "entity_two": "line:0"}],
                          "sketch_name": "ConBench"},
     lambda p: p["results"][0].get("applied") == "midpoint", None),
    # the state read is the verdict: the sketch is MORE constrained than it was, and the tool's
    # own count of what it added is what says so - a call that returned ok and added nothing is a
    # silent no-op, which is the failure this beat exists to catch.
    ("sketch_get", {"sketch_name": "ConBench"},
     lambda p: p.get("constraint_count", 0) >= 6, None),
    # a constraint the geometry cannot satisfy is REFUSED by name, not silently dropped.
    ("sketch_constrain", {"constraints": [{"constraint": "tangent", "entity_one": "line:0",
                                           "entity_two": "line:1"}],
                          "sketch_name": "ConBench"}, "refused", None),
    # THE SECOND BENCH: the constraint kinds the first has no geometry for. Deliberately its own
    # sketch - every curve on ConBench is already pinned by the constraints above, and stacking more
    # onto them refuses as over-constrained instead of proving anything. Two ordering facts drive
    # the refs: a fresh sketch owns its ORIGIN as point:0, so the first drawn point is point:1; and
    # the kinds that CREATE curves run in sketches of their own, below, because a new curve renumbers
    # every ref written after it.
    ("sketch_create", {"plane": "xy", "name": "ConBench2"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 860, "y1": 480,
                                           "x2": 930, "y2": 486}],
                             "sketch_name": "ConBench2"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 945, "y1": 489,
                                           "x2": 1000, "y2": 495}],
                             "sketch_name": "ConBench2"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 860, "y1": 505,
                                           "x2": 866, "y2": 560}],
                             "sketch_name": "ConBench2"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 880, "y1": 505,
                                           "x2": 920, "y2": 511}],
                             "sketch_name": "ConBench2"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 880, "y1": 590,
                                           "x2": 920, "y2": 602}],
                             "sketch_name": "ConBench2"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 940, "y1": 505,
                                           "x2": 980, "y2": 545}],
                             "sketch_name": "ConBench2"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 880, "cy": 660, "radius": 20}],
                             "sketch_name": "ConBench2"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 884, "cy": 664, "radius": 10}],
                             "sketch_name": "ConBench2"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "spline",
                                           "points": [[920, 602], [950, 616], [980, 600]]}],
                             "sketch_name": "ConBench2"}, "ok", None),
    ("sketch_constrain", {"constraints": [{"constraint": "vertical", "entity_one": "line:2"}],
                          "sketch_name": "ConBench2"},
     lambda p: p["results"][0].get("applied") == "vertical", None),
    ("sketch_constrain", {"constraints": [{"constraint": "collinear", "entity_one": "line:1",
                                           "entity_two": "line:0"}],
                          "sketch_name": "ConBench2"},
     lambda p: p["results"][0].get("applied") == "collinear", None),
    ("sketch_constrain", {"constraints": [{"constraint": "concentric", "entity_one": "circle:1",
                                           "entity_two": "circle:0"}],
                          "sketch_name": "ConBench2"},
     lambda p: p["results"][0].get("applied") == "concentric", None),
    # the spline continues the line it was drawn from, sharing that endpoint, and 'smooth' makes the
    # join curvature-continuous - the one kind that needs a spline on at least one side.
    ("sketch_constrain", {"constraints": [{"constraint": "smooth", "entity_one": "spline:0",
                                           "entity_two": "line:4"}],
                          "sketch_name": "ConBench2"},
     lambda p: p["results"][0].get("applied") == "smooth", None),
    # the square's four lines told they ARE a polygon - equal lengths and equal angles in one
    # constraint instead of six.
    ("sketch_constrain", {"constraints": [{"constraint": "polygon",
                                           "entities": "line:5,line:6,line:7,line:8"}],
                          "sketch_name": "ConBench2"},
     lambda p: p["results"][0].get("applied") == "polygon", None),
    # fix pins a curve where it sits; unfix releases the same one, so the pair is checkable as a
    # pair - a 'fix' that silently did nothing leaves nothing for 'unfix' to find.
    ("sketch_constrain", {"constraints": [{"constraint": "fix", "entity_one": "line:3"}],
                          "sketch_name": "ConBench2"},
     lambda p: p["results"][0].get("applied") == "fix", None),
    ("sketch_constrain", {"constraints": [{"constraint": "unfix", "entity_one": "line:3"}],
                          "sketch_name": "ConBench2"},
     lambda p: p["results"][0].get("applied") == "unfix", None),
    # THE POINT-PAIR KINDS, on four free stubs of their own. They take POINTS, and a bare 'point:N'
    # counts every point the sketch owns - a line's own endpoints included - so 'point:1' on a
    # sketch holding curves is the first line's start, not the first point drawn. Anchored refs name
    # the endpoint directly, which is the whole reason the anchor form exists.
    ("sketch_create", {"plane": "xy", "name": "PtBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1060, "y1": 300,
                                           "x2": 1090, "y2": 306}],
                             "sketch_name": "PtBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1110, "y1": 296,
                                           "x2": 1140, "y2": 304}],
                             "sketch_name": "PtBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1060, "y1": 340,
                                           "x2": 1090, "y2": 348}],
                             "sketch_name": "PtBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1066, "y1": 380,
                                           "x2": 1096, "y2": 388}],
                             "sketch_name": "PtBench"}, "ok", None),
    # two stubs made LEVEL by their start points, and two made PLUMB by theirs - no curve is
    # constrained either time, which is what separates these from plain horizontal/vertical.
    ("sketch_constrain", {"constraints": [{"constraint": "horizontal_points",
                                           "entity_one": "line:0:start",
                                           "entity_two": "line:1:start"}],
                          "sketch_name": "PtBench"},
     lambda p: p["results"][0].get("applied") == "horizontal_points", None),
    ("sketch_constrain", {"constraints": [{"constraint": "vertical_points",
                                           "entity_one": "line:2:start",
                                           "entity_two": "line:3:start"}],
                          "sketch_name": "PtBench"},
     lambda p: p["results"][0].get("applied") == "vertical_points", None),
    # THE CREATOR KINDS. Each gets its own sketch: an offset lands NEW curves, so a second one
    # written against the same sketch would be aimed at refs the first has already renumbered. The
    # verdict is 'created_count' - a constraint that returned ok and drew nothing is the silent
    # no-op these rows exist to catch.
    ("sketch_create", {"plane": "xy", "name": "OffOne"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 860, "y1": 730,
                                           "x2": 940, "y2": 730}],
                             "sketch_name": "OffOne"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 940, "y1": 730,
                                           "x2": 940, "y2": 790}],
                             "sketch_name": "OffOne"}, "ok", None),
    ("sketch_constrain", {"constraints": [{"constraint": "offset", "entities": "line:0,line:1",
                                           "distance": 8}],
                          "sketch_name": "OffOne"},
     lambda p: p["results"][0].get("applied") == "offset"
     and p["results"][0].get("created_count", 0) >= 2, None),
    ("sketch_create", {"plane": "xy", "name": "OffTwo"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 960, "y1": 730,
                                           "x2": 1040, "y2": 730}],
                             "sketch_name": "OffTwo"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1040, "y1": 730,
                                           "x2": 1040, "y2": 790}],
                             "sketch_name": "OffTwo"}, "ok", None),
    # the two-sided form draws the offset on BOTH sides of the source, so it lands twice the curves.
    ("sketch_constrain", {"constraints": [{"constraint": "offset_two_sides",
                                           "entities": "line:0,line:1", "distance": 8}],
                          "sketch_name": "OffTwo"},
     lambda p: p["results"][0].get("applied") == "offset_two_sides"
     and p["results"][0].get("created_count", 0) >= 4, None),
    ("sketch_create", {"plane": "xy", "name": "CircPat"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "point", "cx": 900, "cy": 880}],
                             "sketch_name": "CircPat"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 940, "cy": 880, "radius": 6}],
                             "sketch_name": "CircPat"}, "ok", None),
    # six around the drawn centre point: five NEW circles, the original not counted.
    ("sketch_constrain", {"constraints": [{"constraint": "circular_pattern",
                                           "entities": "circle:0", "entity_one": "point:1",
                                           "quantity": 6, "angle": 360}],
                          "sketch_name": "CircPat"},
     lambda p: p["results"][0].get("applied") == "circular_pattern"
     and p["results"][0].get("created_count") == 5, None),
    # THE DIMENSION BENCH: the sizing kinds the story's own sketches never need. Each one names a
    # different pair of operand types, so each needs its own geometry - a circle pair sharing a
    # centre, a line and a circle for the tangent measure, and an ellipse for the two radius kinds.
    # Every row reads the LANDED value back: a dimension that attached to the wrong entity still
    # returns ok, and only its measured number says so.
    ("sketch_create", {"plane": "xy", "name": "DimBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1060, "y1": 480,
                                           "x2": 1120, "y2": 500}],
                             "sketch_name": "DimBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 1090, "cy": 560, "radius": 24}],
                             "sketch_name": "DimBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 1090, "cy": 560, "radius": 12}],
                             "sketch_name": "DimBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1140, "y1": 530,
                                           "x2": 1140, "y2": 590}],
                             "sketch_name": "DimBench"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "ellipse", "cx": 1090, "cy": 650,
                                           "radius": 30, "minor": 16}],
                             "sketch_name": "DimBench"}, "ok", None),
    # the horizontal component of a slanted line's own span: 60 mm across, not its 63.2 mm length.
    ("sketch_dimension", {"dimensions": [{"dim_type": "horizontal_distance",
                                          "entity_one": "line:0:start",
                                          "entity_two": "line:0:end"}],
                          "sketch_name": "DimBench"},
     _dim_measures(60.0), None),
    ("sketch_dimension", {"dimensions": [{"dim_type": "diameter", "entity_one": "circle:0"}],
                          "sketch_name": "DimBench"}, _dim_measures(48.0), None),
    # the gap between two circles on one centre: 24 mm outer less 12 mm inner.
    ("sketch_dimension", {"dimensions": [{"dim_type": "concentric_circle",
                                          "entity_one": "circle:0", "entity_two": "circle:1"}],
                          "sketch_name": "DimBench"},
     _dim_measures(12.0), None),
    # line to the NEAR tangent of the circle: the line stands at x 1140, the circle's near side at
    # 1090+24, so 26 mm.
    ("sketch_dimension", {"dimensions": [{"dim_type": "tangent_distance", "entity_one": "line:1",
                                          "entity_two": "circle:0"}],
                          "sketch_name": "DimBench"},
     _dim_measures(26.0), None),
    ("sketch_dimension", {"dimensions": [{"dim_type": "ellipse_major_radius",
                                          "entity_one": "ellipse:0"}],
                          "sketch_name": "DimBench"}, _dim_measures(30.0), None),
    ("sketch_dimension", {"dimensions": [{"dim_type": "ellipse_minor_radius",
                                          "entity_one": "ellipse:0"}],
                          "sketch_name": "DimBench"}, _dim_measures(16.0), None),
    ("sketch_create", {"plane": "xy", "name": "AutoCon"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 600, "y1": 560,
                                           "x2": 700, "y2": 600}],
                             "sketch_name": "AutoCon"}, "ok", None),
    ("sketch_get", {"sketch_name": "AutoCon"},
     lambda p: p.get("is_fully_constrained") is False, None),
    ("sketch_constrain", {"constraints": [{"constraint": "auto"}], "sketch_name": "AutoCon"},
     lambda p: p["results"][0].get("added_constraints", 0)
     + p["results"][0].get("added_dimensions", 0) > 0
     and p["results"][0].get("is_fully_constrained") is True, None),
    # a re-run on the constrained sketch adds nothing and is a clean no-op, not an error.
    ("sketch_constrain", {"constraints": [{"constraint": "auto"}], "sketch_name": "AutoCon"},
     lambda p: p["results"][0].get("added_dimensions") == 0
     and p["results"][0].get("added_constraints") == 0
     and p["results"][0].get("is_fully_constrained") is True, None),
    # the SECOND solver option, on a sketch of its own - option1 above is the default and the one
    # the payload names, so a request for the other has to come back named as the other or the knob
    # was dropped on the way in.
    ("sketch_create", {"plane": "xy", "name": "AutoCon2"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 720, "y1": 560,
                                           "x2": 820, "y2": 600}],
                             "sketch_name": "AutoCon2"}, "ok", None),
    ("sketch_constrain", {"constraints": [{"constraint": "auto", "result_option": "option2"}],
                          "sketch_name": "AutoCon2"},
     lambda p: p["results"][0].get("result_option_requested") == "option2"
     and p["results"][0].get("added_constraints", 0)
     + p["results"][0].get("added_dimensions", 0) > 0
     and p["results"][0].get("is_fully_constrained") is True, None),
    # rectangular_pattern with distance_type='extent': 'distance' is the pattern's TOTAL span, so
    # three instances 90 mm across sit at x 600 / 645 / 690 - the landed centre is the verdict, and
    # a spacing read in centimetres would put the last one at 609.
    ("sketch_create", {"plane": "xy", "name": "PatExtent"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 600, "cy": 640, "radius": 5}],
                             "sketch_name": "PatExtent"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 660,
                                           "x2": 700, "y2": 660}],
                             "sketch_name": "PatExtent"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 660,
                                           "x2": 600, "y2": 760}],
                             "sketch_name": "PatExtent"}, "ok", None),
    ("sketch_constrain", {"constraints": [{"constraint": "rectangular_pattern",
                                           "entities": "circle:0", "entity_one": "line:0",
                                           "entity_two": "line:1", "quantity": 3, "distance": 90,
                                           "quantity_two": 1, "distance_two": 10,
                                           "distance_type": "extent"}],
                          "sketch_name": "PatExtent"},
     lambda p: p["results"][0].get("distance_type") == "extent"
     and p["results"][0].get("created_count", 0) == 2, None),
    ("sketch_get", {"sketch_name": "PatExtent", "include_entities": True},
     lambda p: abs(max((e.get("center") or {}).get("x", 0) for e in (p.get("entities") or [])
                       if e.get("type") == "circle") - _px("PatExtent", 690.0)) < 0.01, None),
    # per-instance suppression: a 3x2 pattern of one circle has 5 SUPPRESSIBLE instances (the
    # original does not count), and the flags are read back off the CREATED constraint. A suppressed
    # instance draws no curve, so 5 instances less 2 suppressed is 3 new curves - the count and the
    # landed flags together are what an echoed payload cannot fake.
    ("sketch_create", {"plane": "xy", "name": "PatSupp"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 600, "cy": 800, "radius": 4}],
                             "sketch_name": "PatSupp"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 820,
                                           "x2": 700, "y2": 820}],
                             "sketch_name": "PatSupp"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 600, "y1": 820,
                                           "x2": 600, "y2": 920}],
                             "sketch_name": "PatSupp"}, "ok", None),
    ("sketch_constrain", {"constraints": [{"constraint": "rectangular_pattern",
                                           "entities": "circle:0", "entity_one": "line:0",
                                           "entity_two": "line:1", "quantity": 3,
                                           "quantity_two": 2, "distance": 20, "distance_two": 20,
                                           "suppressed": [False, True, False, True, False]}],
                          "sketch_name": "PatSupp"},
     lambda p: p["results"][0].get("suppressed_applied") == [False, True, False, True, False]
     and p["results"][0].get("created_count") == 3, None),
    # the N-1 length IS the input's contract: 6 flags for a 3x2 counts the original, and the guard
    # refuses it naming expected against got, before anything is created.
    ("sketch_constrain", {"constraints": [{"constraint": "rectangular_pattern",
                                           "entities": "circle:0", "entity_one": "line:0",
                                           "entity_two": "line:1", "quantity": 3,
                                           "quantity_two": 2, "distance": 20, "distance_two": 20,
                                           "suppressed": [False] * 6}],
                          "sketch_name": "PatSupp"}, "refused", None),
    # a knob whose input object exists on ONE constraint only is refused elsewhere, never dropped.
    ("sketch_constrain", {"constraints": [{"constraint": "horizontal", "entity_one": "line:0",
                                           "dimension_strategy": "chain"}],
                          "sketch_name": "PatSupp"}, "refused", None),
    # autoConstrain's four dimensioning-strategy setters are UNAVAILABLE on this build - the input
    # object refuses the assignment ("This API is not currently available") - so the request is
    # refused NAMING the knob that would not take, before autoConstrain runs and before anything is
    # constrained. Plain constraint='auto' still solves (the beats above), and this beat is the one
    # that fails loudly if a strategy is ever quietly dropped instead.
    ("sketch_create", {"plane": "xy", "name": "AutoStrat"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 740, "y1": 800,
                                           "x2": 840, "y2": 850}],
                             "sketch_name": "AutoStrat"}, "ok", None),
    ("sketch_constrain", {"constraints": [{"constraint": "auto",
                                           "dimension_strategy": "baseline",
                                           "linear_diameter_dims": "avoid"}],
                          "sketch_name": "AutoStrat"}, "refused", None),
    # sketch_insert_svg: art from local disk into a FRESH sketch. importSVG ignores the file's own
    # width/height and viewBox - 1 SVG user unit lands as 1/96 inch times 'scale' - so the 36-unit
    # rectangle measures ~36 mm at scale=3.7795 and nothing else can produce that width. The art is
    # placed AT the sketch origin, so this empty-before sketch's own box IS the art's size.
    ("sketch_create", {"plane": "xy", "name": "SvgTarget"}, "ok", None),
    ("sketch_insert_svg", {"file_path": SVG_PATH, "sketch_name": "SvgTarget", "scale": 3.7795},
     lambda p: p.get("curves_added", 0) > 0 and p.get("sketch") == "SvgTarget"
     and 30 < (p.get("sketch_extent") or {}).get("width", 0) < 42, None),
    # SK-5's closure, through the TOOL: the 96-user-unit square at scale 1 is exactly one inch, and
    # the art lands Y-DOWN from the sketch origin - so this empty-before sketch measures 25.4 mm
    # square with its min y at -25.4. A y-up landing (or any scale drift) moves that number.
    ("sketch_create", {"plane": "xy", "name": "Svg96"}, "ok", None),
    ("sketch_insert_svg", {"file_path": SVG96_PATH, "sketch_name": "Svg96", "scale": 1},
     _svg96_extent, None),
    # importSVG RAISES on a path that is not a file and that raise rolls back the whole surrounding
    # transaction, so the miss is named before Fusion is touched.
    ("sketch_insert_svg", {"file_path": EXPORT_DIR + "/no_such_logo.svg",
                           "sketch_name": "SvgTarget"}, "refused", None),
    # Wave-3 sketch surface: the new curve kinds + dimension types, each asserting a read-back
    # the payload could not echo (the degree clamp, the wedge rule, the offset rotation, and the
    # API's own self-naming parallelism raises - all measured contracts).
    ("sketch_create", {"plane": "xy", "name": "W3Curves"}, "ok", None),
    # degree 5 over 3 control points: the API silently CLAMPS to n-1, and the payload publishes
    # the BUILT degree read off the spline - 2 here is a live read-back, not an echo of the 5.
    ("sketch_add_geometry", {"geometry": [{"kind": "cv_spline",
                                           "points": [[740, 0], [760, 20], [780, 0]],
                                           "degree": 5}],
                             "sketch_name": "W3Curves"},
     lambda p: p["results"][0].get("degree") == 2, None),
    ("sketch_add_geometry", {"geometry": [{"kind": "cv_spline",
                                           "points": [[740, -40], [750, -20], [760, -40],
                                                      [770, -20], [780, -40], [790, -20]],
                                           "degree": 5}],
                             "sketch_name": "W3Curves"},
     lambda p: p["results"][0].get("degree") == 5, None),
    # 'minor' omitted: the label reports the EFFECTIVE minor radius (major/2), never None.
    ("sketch_add_geometry", {"geometry": [{"kind": "ellipse", "cx": 830, "cy": 0, "radius": 20}],
                             "sketch_name": "W3Curves"},
     lambda p: "minor=10" in (p["results"][0].get("label") or ""), None),
    # conic closed by its chord forms a profile that extrudes - this proves the note's modelling
    # claim end to end.
    ("sketch_create", {"plane": "xy", "name": "W3Conic"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "conic", "x1": 740, "y1": 60,
                                           "x2": 780, "y2": 60, "cx": 760, "cy": 90,
                                           "rho": 0.6}],
                             "sketch_name": "W3Conic"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 740, "y1": 60,
                                           "x2": 780, "y2": 60}],
                             "sketch_name": "W3Conic"}, "ok", None),
    ("model_extrude", {"sketch_name": "W3Conic", "profile_index": 0, "distance": 5}, _extruded, None),
    ("sketch_create", {"plane": "xy", "name": "W3Earc"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "elliptical_arc", "cx": 840, "cy": 80,
                                           "radius": 30, "minor": 15, "sweep_deg": 180}],
                             "sketch_name": "W3Earc"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 870, "y1": 80,
                                           "x2": 810, "y2": 80}],
                             "sketch_name": "W3Earc"}, "ok", None),
    ("model_extrude", {"sketch_name": "W3Earc", "profile_index": 0, "distance": 5}, _extruded, None),
    # the coincident TRAP, on the success path where the caller who meant "centre this here" is:
    # addCoincident(point, circle) succeeds and lands the point ON the rim, so the note has to say
    # so - and it must NOT say so when the operand was a POINT, where the point really is centred.
    ("sketch_create", {"plane": "xy", "name": "CoincTrap"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 1500, "cy": 0, "radius": 20}],
                             "sketch_name": "CoincTrap"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "point", "cx": 1560, "cy": 0}],
                             "sketch_name": "CoincTrap"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "point", "cx": 1560, "cy": 30}],
                             "sketch_name": "CoincTrap"}, "ok", None),
    ("sketch_constrain", {"constraints": [{"constraint": "coincident", "entity_one": "point:2",
                                           "entity_two": "circle:0"}],
                          "sketch_name": "CoincTrap"},
     lambda p: "ON that curve" in (p["results"][0].get("note") or ""), None),
    ("sketch_constrain", {"constraints": [{"constraint": "coincident", "entity_one": "point:3",
                                           "entity_two": "point:1"}],
                          "sketch_name": "CoincTrap"},
     lambda p: "ON that curve" not in (p["results"][0].get("note") or ""), None),
    # sketch_set_text's PATH layouts: one scratch sketch holding a line and a closed circle, then
    # text laid ALONG each and FITTED to the line. 'definition_type' is the created text's own
    # objectType and 'mode_verified' says whether it matches the mode asked for, so a text that
    # landed in another layout cannot pass as this one.
    ("sketch_create", {"plane": "xy", "name": "TextPaths"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1200, "y1": 0,
                                           "x2": 1300, "y2": 0}],
                             "sketch_name": "TextPaths"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 1250, "cy": 60, "radius": 25}],
                             "sketch_name": "TextPaths"}, "ok", None),
    ("sketch_set_text", {"text": "ALONG", "sketch_name": "TextPaths", "create": True,
                         "mode": "along_path", "path": "line:0", "height": 5},
     lambda p: p.get("mode_verified") is True
     and str(p.get("definition_type") or "").endswith("AlongPathTextDefinition"), None),
    # a CLOSED circle wraps the text right around the hole it marks - the headline path case.
    ("sketch_set_text", {"text": "M8 CLEARANCE", "sketch_name": "TextPaths", "create": True,
                         "mode": "along_path", "path": "circle:0", "align": "center", "height": 4},
     lambda p: p.get("mode_verified") is True, None),
    # fit_on_path spaces the characters over the whole path itself, so the payload carries neither
    # 'align' nor 'character_spacing' - its definition object has no slot for either.
    ("sketch_set_text", {"text": "FIT", "sketch_name": "TextPaths", "create": True,
                         "mode": "fit_on_path", "path": "line:0", "height": 5},
     lambda p: str(p.get("definition_type") or "").endswith("FitOnPathTextDefintion")
     and "align" not in p and "character_spacing" not in p, None),
    # cross-mode inputs: each is refused BY NAME rather than silently dropped, and nothing is created.
    ("sketch_set_text", {"text": "X", "sketch_name": "TextPaths", "create": True,
                         "mode": "fit_on_path", "path": "line:0", "align": "center"}, "refused", None),
    ("sketch_set_text", {"text": "X", "sketch_name": "TextPaths", "create": True,
                         "mode": "along_path", "path": "line:0", "x": 10}, "refused", None),
    ("sketch_set_text", {"text": "X", "sketch_name": "TextPaths", "create": True,
                         "mode": "multi_line", "path": "line:0"}, "refused", None),
    # the layout inputs shape NEW text only: passing one to an EDIT is refused, never ignored.
    ("sketch_set_text", {"text": "FIT", "sketch_name": "TextPaths", "angle_deg": 15},
     "refused", None),
    ("sketch_create", {"plane": "xy", "name": "TextBinding"}, "ok", None),
    ("param_add", {"name": "SweepLabel", "unit": "Text", "expression": "'ALPHA'"},
     lambda p: p.get("parameter", {}).get("value") == "ALPHA", None),
    ("sketch_set_text", {"sketch_name": "TextBinding", "text": "seed", "create": True,
                         "x": 1200, "y": 120, "height": 5, "font_name": "Arial"},
     lambda p: p.get("sketch_text_count") == 1, None),
    ("sketch_set_text", {"sketch_name": "TextBinding", "index": 0, "parameter": "SweepLabel"},
     lambda p: p.get("changed_count") == 1
     and p["changed"][0].get("expression") == "SweepLabel", None),
    ("sketch_get", {"sketch_name": "TextBinding", "include_entities": True},
     lambda p: [(e.get("text"), e.get("text_expression"))
                for e in p.get("entities", []) if e.get("type") == "text"]
     == [("ALPHA", "SweepLabel")], None),
    ("param_set", {"name": "SweepLabel", "expression": "'BETA'"},
     lambda p: p.get("after", {}).get("value") == "BETA", None),
    ("sketch_get", {"sketch_name": "TextBinding", "include_entities": True},
     lambda p: [(e.get("text"), e.get("text_expression"))
                for e in p.get("entities", []) if e.get("type") == "text"]
     == [("BETA", "SweepLabel")], None),
    ("sketch_set_text", {"sketch_name": "TextBinding", "index": 0, "text": ""},
     lambda p: p.get("changed_count") == 1 and p["changed"][0].get("after") == "", None),
    ("sketch_get", {"sketch_name": "TextBinding", "include_entities": True},
     lambda p: [(e.get("text"), e.get("text_expression"))
                for e in p.get("entities", []) if e.get("type") == "text"]
     == [("", None)], None),
    ("sketch_set_text", {"sketch_name": "TextBinding", "index": 0, "text": "   "},
     lambda p: p.get("changed_count") == 1 and p["changed"][0].get("after") == "   ", None),
    ("sketch_get", {"sketch_name": "TextBinding", "include_entities": True},
     lambda p: [(e.get("text"), e.get("text_expression"))
                for e in p.get("entities", []) if e.get("type") == "text"]
     == [("   ", None)], None),
    ("sketch_set_text", {"sketch_name": "TextBinding", "index": 0,
                         "text": "", "parameter": "SweepLabel"},
     lambda p: p.get("changed_count") == 1
     and p["changed"][0].get("expression") == "SweepLabel", None),
    ("sketch_get", {"sketch_name": "TextBinding", "include_entities": True},
     lambda p: [(e.get("text"), e.get("text_expression"))
                for e in p.get("entities", []) if e.get("type") == "text"]
     == [("BETA", "SweepLabel")], None),
    ("sketch_set_text", {"sketch_name": "TextBinding", "index": 0,
                         "text": "WRONG", "parameter": "SweepLabel"},
     _refused("two different string sources"), None),
    ("sketch_get", {"sketch_name": "TextBinding", "include_entities": True},
     lambda p: [(e.get("text"), e.get("text_expression"))
                for e in p.get("entities", []) if e.get("type") == "text"]
     == [("BETA", "SweepLabel")], None),
    # FONT: the one input that reaches the API twice - onto the INPUT before a create, onto the
    # SketchText itself on an edit. No API lists or validates the legal names, so Fusion's own
    # "invalid input font name" raise IS the whole check, and each refusal hands that sentence on
    # with the name it was given. Its own scratch sketch carries the whole run.
    ("sketch_create", {"plane": "xy", "name": "FontProbe"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1200, "y1": 200,
                                           "x2": 1300, "y2": 200}],
                             "sketch_name": "FontProbe"}, "ok", None),
    # the font is read back off the LANDED text, not off the input, so 'font' here is what the
    # created text reports - and nothing sits in 'requested', which is where an unread value goes.
    ("sketch_set_text", {"text": "FONT", "sketch_name": "FontProbe", "create": True,
                         "x": 1200, "y": 160, "height": 6, "font_name": "Arial"},
     lambda p: p.get("font") == "Arial" and p.get("sketch_text_count") == 1
     and "requested" not in p, None),
    # the same font through a setAs* PLACEMENT: it is applied to the input before the placement
    # call and survives it, which is what makes the along-path text report it too.
    ("sketch_set_text", {"text": "ALONGFONT", "sketch_name": "FontProbe", "create": True,
                         "mode": "along_path", "path": "line:0", "height": 5,
                         "font_name": "Arial"},
     lambda p: p.get("font") == "Arial" and p.get("sketch_text_count") == 2, None),
    # a font this machine does not carry: the create raises at add() and the refusal names it.
    ("sketch_set_text", {"text": "NOFONT", "sketch_name": "FontProbe", "create": True,
                         "x": 1200, "y": 140, "height": 6,
                         "font_name": "ZzNoSuchFont_MCP_Probe"}, "refused", None),
    # font names are CASE-SENSITIVE at the API, so 'arial' is as unknown as any other miss.
    ("sketch_set_text", {"text": "NOFONT", "sketch_name": "FontProbe", "create": True,
                         "x": 1200, "y": 140, "height": 6, "font_name": "arial"}, "refused", None),
    # the count is the proof the two refusals created nothing: this is the THIRD text in the
    # sketch. It carries the no-font regression too - omit 'font_name' and no 'font' key is
    # published at all, on the payload or on a record.
    ("sketch_set_text", {"text": "NOFONTKEY", "sketch_name": "FontProbe", "create": True,
                         "x": 1200, "y": 120, "height": 6},
     lambda p: p.get("sketch_text_count") == 3 and "font" not in p, None),
    # EDITING one text: the font goes on FIRST and each changed record carries the font that text
    # reports back beside the string that landed.
    ("sketch_set_text", {"text": "FONT2", "sketch_name": "FontProbe", "index": 0,
                         "font_name": "Arial"},
     lambda p: p["changed"][0].get("font") == "Arial"
     and p["changed"][0].get("after") == "FONT2", None),
    # the unknown name on an EDIT: because the font is applied ahead of the string, the refusal
    # leaves this text's string untouched as well.
    ("sketch_set_text", {"text": "FONT3", "sketch_name": "FontProbe", "index": 0,
                         "font_name": "ZzNoSuchFont_MCP_Probe"}, "refused", None),
    # the next edit answers normally, and its 'before' is what proves the refused call wrote
    # nothing - the string is still the one the successful edit left.
    ("sketch_set_text", {"text": "FONT4", "sketch_name": "FontProbe", "index": 0},
     lambda p: p["changed"][0].get("before") == "FONT2"
     and p["changed"][0].get("after") == "FONT4" and "font" not in p["changed"][0], None),
    # sketch text is deleted by the SAME index sketch_set_text edits by: one text in its own sketch,
    # deleted as 'text:0'. The deleted string and the collection count read back off the sketch are
    # the verdict - a delete that removed nothing is an error, never a false ok.
    ("sketch_create", {"plane": "xy", "name": "TextDel"}, "ok", None),
    ("sketch_set_text", {"text": "SCRAP", "sketch_name": "TextDel", "create": True,
                         "x": 1200, "y": 100, "height": 5}, "ok", None),
    # SketchTexts.add APPENDS, so the SECOND text is 'text:1' - and deleting that index has to take
    # the second one, never the first. The deleted STRING is what separates the two.
    ("sketch_set_text", {"text": "SCRAP2", "sketch_name": "TextDel", "create": True,
                         "x": 1200, "y": 80, "height": 5}, "ok", None),
    # THE EDIT-PATH RESIZE, before those deletes and leaving their inputs untouched: 'height' on an
    # EDIT writes SketchText.heightParameter and the glyph geometry follows it. The same string goes
    # back in, so the string and the count the deletes below read are exactly what they were. The
    # tool REFUSES a resize whose value landed while the box stayed put, so a green step here is
    # itself the geometry-followed proof - what the predicate reads is the numbers it published.
    ("sketch_set_text", {"text": "SCRAP2", "sketch_name": "TextDel", "index": 1, "height": 3},
     lambda p: p.get("changed_count") == 1 and (lambda c:
         c.get("height") == 3.0 and c.get("height_before") == 5.0
         and c.get("before") == "SCRAP2" and c.get("after") == "SCRAP2"
         and isinstance(c.get("measured_width"), (int, float)) and c["measured_width"] > 0
         and isinstance(c.get("measured_height"), (int, float)) and c["measured_height"] > 0
     )(p["changed"][0]), None),
    ("sketch_delete_entity", {"sketch_name": "TextDel", "target": "text:1"},
     lambda p: p.get("text") == "SCRAP2" and p.get("texts_before") == 2
     and p.get("texts_after") == 1, None),
    ("sketch_delete_entity", {"sketch_name": "TextDel", "target": "text:0"},
     lambda p: p.get("text") == "SCRAP" and p.get("texts_before") == 1
     and p.get("texts_after") == 0, None),
    # the emptied sketch has no text at that index any more - the refusal names the index and count.
    ("sketch_delete_entity", {"sketch_name": "TextDel", "target": "text:0"}, "refused", None),
    # THE TEXT READ-BACK (the S6 gap): sketch_get's X-ray lists each SketchText at its text:<i>
    # address with the string, the FONT (fontName is API-readable), the height in display units,
    # and a sketch-space bounding box. FontProbe's final state pins all three record shapes at
    # once: text:0 was edited to FONT4 (its Arial ride-along from the FONT2 edit stays), text:1 is
    # the along-path ALONGFONT, text:2 was created with NO font and reads font None.
    ("sketch_get", {"sketch_name": "FontProbe"},
     lambda p: (p.get("counts") or {}).get("texts") == 3 and "entities" not in p, None),
    # text:2 was created with NO font_name and still reads a real font (measured: the platform
    # gives every text the app default) - so 'font' is a non-empty string on all three records.
    ("sketch_get", {"sketch_name": "FontProbe", "include_entities": True},
     lambda p: (lambda t: [r["id"] for r in t] == ["text:0", "text:1", "text:2"]
                and t[0].get("text") == "FONT4" and t[0].get("font") == "Arial"
                and isinstance(t[0].get("height"), (int, float)) and t[0]["height"] > 0
                and t[1].get("text") == "ALONGFONT"
                and t[2].get("text") == "NOFONTKEY"
                and isinstance(t[2].get("font"), str) and t[2]["font"]
                and "min" in (t[0].get("bounding_box") or {}))
     ([e for e in p.get("entities", []) if e.get("type") == "text"]), None),
    # A WRONG DIMENSION COMES OUT: 'dimension:<i>' indexes sketchDimensions in the order the X-ray
    # lists them, and the parameter name captured before the mutation says WHICH one went. The two
    # lines are DIFFERENT lengths so the survivor's expression convicts a wrong-index delete.
    ("sketch_create", {"plane": "xy", "name": "DimDel"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1300, "y1": 100,
                                           "x2": 1360, "y2": 100}],
                             "sketch_name": "DimDel"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": 1300, "y1": 120,
                                           "x2": 1340, "y2": 120}],
                             "sketch_name": "DimDel"}, "ok", None),
    ("sketch_dimension", {"dimensions": [{"dim_type": "distance", "entity_one": "line:0",
                                          "value": "60 mm"}],
                          "sketch_name": "DimDel"}, "ok", None),
    ("sketch_dimension", {"dimensions": [{"dim_type": "distance", "entity_one": "line:1",
                                          "value": "40 mm"}],
                          "sketch_name": "DimDel"}, "ok", None),
    ("sketch_get", {"sketch_name": "DimDel", "include_entities": True},
     lambda p: p.get("dimension_count") == 2, None),
    ("sketch_delete_entity", {"sketch_name": "DimDel", "target": "dimension:1"},
     lambda p: p.get("dimensions_before") == 2 and p.get("dimensions_after") == 1
     and isinstance(p.get("parameter"), str) and bool(p["parameter"]), None),
    ("sketch_get", {"sketch_name": "DimDel", "include_entities": True},
     lambda p: p.get("dimension_count") == 1
     and "60" in (p["dimensions"][0].get("expression") or ""), None),
    # the emptied index refuses naming the count - never a false ok on a dimension that is gone.
    ("sketch_delete_entity", {"sketch_name": "DimDel", "target": "dimension:1"}, "refused", None),
    # THE SLOT FAMILY, one scratch sketch per shape in a clear band so every count is absolute.
    # 'radius' is the HALF width throughout (the label carries the full width), each tailed
    # constructor takes its tail POSITIONALLY, and the ladders differ per kind - which is what the
    # cross-kind refusals below pin.
    ("sketch_create", {"plane": "xy", "name": "SlotA"}, "ok", None),
    # a three-point arc slot is built entirely out of SketchArcs - five of them, and its closed
    # outline forms a profile. The only dimension it can create is the width one.
    ("sketch_add_geometry", {"geometry": [{"kind": "three_point_arc_slot", "x1": 1700, "y1": 900,
                                           "x2": 1800, "y2": 900, "cx": 1750, "cy": 930,
                                           "radius": 5, "create_width_dimension": True}],
                             "sketch_name": "SlotA"},
     lambda p: (lambda r: r.get("curves_added") == 5 and r["sketch"]["arc_count"] == 5
                and r["sketch"]["profile_count"] >= 1
                and "three_point_arc_slot" in (r.get("label") or "")
                and "w=10" in (r.get("label") or ""))(p["results"][0]), None),
    # its arc is fixed by its three points, so it has no radius or angle to dimension - the flag
    # that belongs to the centre-point kind is refused by name and points there.
    ("sketch_add_geometry", {"geometry": [{"kind": "three_point_arc_slot", "x1": 1700, "y1": 900,
                                           "x2": 1800, "y2": 900, "cx": 1750, "cy": 930,
                                           "radius": 5, "create_angle_dimension": True}],
                             "sketch_name": "SlotA"}, "refused", None),
    # the centre-point arc slot in its four-argument form: centre, start, end, width.
    ("sketch_create", {"plane": "xy", "name": "SlotB"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "center_point_arc_slot", "cx": 1700,
                                           "cy": 1000, "x1": 1750, "y1": 1000, "x2": 1700,
                                           "y2": 1050, "radius": 5}],
                             "sketch_name": "SlotB"},
     lambda p: p["results"][0].get("curves_added") == 5, None),
    # the full ladder: a supplied arc_radius overrides the centre-to-start distance and the angle
    # takes a unit-bearing expression, then each of the three flags gates its OWN dimension - so
    # width + angle asked for and radius declined must land exactly two dimensions.
    ("sketch_create", {"plane": "xy", "name": "SlotC"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "center_point_arc_slot", "cx": 1700,
                                           "cy": 1100, "x1": 1750, "y1": 1100, "x2": 1700,
                                           "y2": 1150, "radius": 5, "arc_radius": 30,
                                           "angle_deg": 45, "create_width_dimension": True,
                                           "create_radius_dimension": False,
                                           "create_angle_dimension": True}],
                             "sketch_name": "SlotC"},
     lambda p: p["results"][0].get("curves_added") == 5, None),
    ("sketch_get", {"sketch_name": "SlotC", "include_entities": True},
     lambda p: p.get("dimension_count") == 2
     and any("diameter" in (d.get("type") or "") for d in p["dimensions"])
     and any("angular" in (d.get("type") or "") for d in p["dimensions"])
     and not any("radial" in (d.get("type") or "") for d in p["dimensions"]), None),
    # an OVERALL slot measures tip to tip: its two points are the outer extremes, so the cap arc
    # centres land inset by the half width - 60 mm tip to tip from centres 52 mm apart.
    ("sketch_create", {"plane": "xy", "name": "SlotD"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "overall_slot", "x1": 1700, "y1": 1200,
                                           "x2": 1760, "y2": 1200, "radius": 4}],
                             "sketch_name": "SlotD"},
     lambda p: p["results"][0].get("curves_added") == 3
     and "w=8" in (p["results"][0].get("label") or ""), None),
    ("sketch_get", {"sketch_name": "SlotD", "include_entities": True},
     lambda p: sorted(round(e["center"]["x"], 3) for e in p["entities"] if e["type"] == "arc")
     == [_px("SlotD", 1704.0), _px("SlotD", 1756.0)], None),
    # the length and the angle are VALUES, not flags: passing either creates its own dimension and
    # adds the fourth line, while the width dimension stays gated on its flag.
    ("sketch_create", {"plane": "xy", "name": "SlotE"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "overall_slot", "x1": 1700, "y1": 1300,
                                           "x2": 1760, "y2": 1300, "radius": 4,
                                           "slot_length": 40, "angle_deg": 30,
                                           "create_width_dimension": True}],
                             "sketch_name": "SlotE"},
     lambda p: p["results"][0].get("curves_added") == 4, None),
    ("sketch_get", {"sketch_name": "SlotE", "include_entities": True},
     lambda p: p.get("dimension_count") == 3
     and any("diameter" in (d.get("type") or "") for d in p["dimensions"])
     and any("linear" in (d.get("type") or "") and abs((d.get("value") or 0) - 40.0) < 1e-3
             and d.get("value_units") == "mm" for d in p["dimensions"])
     # the ANGLE's value is published in DEGREES beside its own 'deg' unit key - the parameter's
     # own read is radians, which would land 0.5236 here against a "30 deg" expression.
     and any("angular" in (d.get("type") or "") and "30" in (d.get("expression") or "")
             and abs((d.get("value") or 0) - 30.0) < 1e-2 and d.get("value_units") == "deg"
             for d in p["dimensions"]), None),
    # the flag ALONE, with no tail: three lines and exactly the one dimension it asked for.
    ("sketch_create", {"plane": "xy", "name": "SlotF"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "overall_slot", "x1": 1700, "y1": 1400,
                                           "x2": 1760, "y2": 1400, "radius": 4,
                                           "create_width_dimension": True}],
                             "sketch_name": "SlotF"},
     lambda p: p["results"][0].get("curves_added") == 3, None),
    ("sketch_get", {"sketch_name": "SlotF", "include_entities": True},
     lambda p: p.get("dimension_count") == 1, None),
    # a CENTRE-point slot's length is the HALF length, centre to cap centre, and it is forwarded
    # unhalved - so a cap centre lands exactly on the second point and the dimension reads 25.
    ("sketch_create", {"plane": "xy", "name": "SlotG"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "center_point_slot", "x1": 1700, "y1": 1500,
                                           "x2": 1725, "y2": 1500, "radius": 3,
                                           "slot_length": 25}],
                             "sketch_name": "SlotG"},
     lambda p: "half_len=25" in (p["results"][0].get("label") or ""), None),
    ("sketch_get", {"sketch_name": "SlotG", "include_entities": True},
     lambda p: any(abs(e["center"]["x"] - _px("SlotG", 1725)) < 1e-3
                   and abs(e["center"]["y"] - _py("SlotG", 1500)) < 1e-3
                   for e in p["entities"] if e["type"] == "arc")
     and any("linear" in (d.get("type") or "") and abs((d.get("value") or 0) - 25.0) < 1e-3
             for d in p["dimensions"]), None),
    # an angle with no length has nothing to sit on: refused naming both.
    ("sketch_add_geometry", {"geometry": [{"kind": "overall_slot", "x1": 1700, "y1": 1400,
                                           "x2": 1760, "y2": 1400, "radius": 4,
                                           "angle_deg": 30}],
                             "sketch_name": "SlotF"}, "refused", None),
    # the linear kinds have no angle FLAG - the angular dimension comes from angle_deg itself.
    ("sketch_add_geometry", {"geometry": [{"kind": "overall_slot", "x1": 1700, "y1": 1400,
                                           "x2": 1760, "y2": 1400, "radius": 4,
                                           "slot_length": 40,
                                           "create_angle_dimension": True}],
                             "sketch_name": "SlotF"}, "refused", None),
    # each cross-kind input is refused pointing at the kind that DOES carry it: arc_radius belongs
    # to the centre-point ARC slot, slot_length to the straight ones - and the three-point arc slot
    # has no radius argument at all, so its refusal must not offer arc_radius as the remedy.
    ("sketch_add_geometry", {"geometry": [{"kind": "center_point_slot", "x1": 1700, "y1": 1500,
                                           "x2": 1725, "y2": 1500, "radius": 3,
                                           "arc_radius": 30}],
                             "sketch_name": "SlotG"}, "refused", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "center_point_arc_slot", "cx": 1700,
                                           "cy": 1000, "x1": 1750, "y1": 1000, "x2": 1700,
                                           "y2": 1050, "radius": 5, "slot_length": 40}],
                             "sketch_name": "SlotB"}, "refused", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "three_point_arc_slot", "x1": 1700, "y1": 900,
                                           "x2": 1800, "y2": 900, "cx": 1750, "cy": 930,
                                           "radius": 5, "slot_length": 40}],
                             "sketch_name": "SlotA"}, "refused", None),
    # the legacy centre-to-centre slot takes two centres and a width and nothing else: a tail would
    # be dropped, so it is refused - and the bare call still draws.
    ("sketch_create", {"plane": "xy", "name": "SlotH"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "slot", "x1": 1700, "y1": 1600,
                                           "x2": 1760, "y2": 1600, "radius": 4,
                                           "slot_length": 40}],
                             "sketch_name": "SlotH"}, "refused", None),
    # the legacy form's own census: addCenterToCenterSlot lands 5 curves - 2 solid lines, 1
    # CONSTRUCTION line (the centre-to-centre one) and 2 arc caps - and 'curves_added' counts the
    # LINE collection's delta, so it reads 3. The note has to say which 5, because the number alone
    # reads like a 3-curve slot.
    ("sketch_add_geometry", {"geometry": [{"kind": "slot", "x1": 1700, "y1": 1600,
                                           "x2": 1760, "y2": 1600, "radius": 4}],
                             "sketch_name": "SlotH"},
     lambda p: (lambda r: r.get("curves_added") == 3
                and "2 solid SketchLines" in (r.get("note") or "")
                and "1 CONSTRUCTION SketchLine" in (r.get("note") or "")
                and "2 SketchArc end caps" in (r.get("note") or ""))(p["results"][0]), None),
    # the independent read: three lines of which EXACTLY ONE is construction, plus the two arc caps.
    ("sketch_get", {"sketch_name": "SlotH", "include_entities": True},
     lambda p: len([e for e in p["entities"] if e["type"] == "line"]) == 3
     and len([e for e in p["entities"] if e["type"] == "line" and e.get("construction")]) == 1
     and len([e for e in p["entities"] if e["type"] == "arc"]) == 2, None),
    # a POLYGON is built by the same SketchLines factory, so its side count is the delta.
    ("sketch_create", {"plane": "xy", "name": "PolyHex"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "polygon", "cx": 1700, "cy": 1700,
                                           "radius": 20, "sides": 6}],
                             "sketch_name": "PolyHex"},
     lambda p: p["results"][0].get("curves_added") == 6, None),
]
