# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""ACT rows: the CAM competence hub - a turned flange drawn by constraint, and the job it carries.

The world the CAM competence beats address, built through the tools instead of kept as a saved
document. Its three dimensioned sketches read is_fully_constrained true before a feature consumes
them, which is the sketch recipes' own bar; the two setups at the bottom are the names a machining
beat addresses. The hub is authored a metre out along X on the XZ plane, and a chunk holding an XZ
sketch is left exactly where it was written - see verify_layout._place_slots.
"""

import time

import _dump_reader
from verify_acts_cam import (
    _TURNING_TYPE, _TURN_MACHINE, _all_cut, _landed_in_one_call, _launched_on, _offers, _op_named,
    _param_landed, _posted_turning, _reveal, _setup_ready, _setup_row, _turning_stock,
    _types_offered)
from verify_core import (
    EXPORT_DIR, _RECALL, _ctx_get, _datum, _drilled, _extruded, _face_up_at, _fg, _filleted,
    _joint_origin_computed, _made_component, _matched, _measured, _near, _num, _prof, _recall,
    _revolved, _watch)

# THE NAMES THE HUB BUILDS UNDER, in one place for the beats that address it.
HUB_COMP = "Hub"                 # the turned flange - the model both setups machine
HUB_PROFILE = "HubProfile"       # the revolved outline, on the axis plane
HUB_HOLES = "HubHoles"           # the bolt-circle reference sketch on the flange top
HUB_KEYWAY = "HubKeyway"         # the keyway cut into the shaft
HUB_WEDGE = "HubWedge"           # the inclined flat on the shaft, on the 30 deg datum
HUB_TILT = "TiltPlane"
HUB_FLANGE_TOP = "HubFlangeTop"  # the pocket outline plus its drawn label
HUB_FLANGE_POCKET = "HubFlangePocket"
HUB_MILL_SETUP = "MillTop"       # the milling job, on the flange top
HUB_TURN_SETUP = "HubTurn"       # the turned job, on the mill-turn machine
HUB_ROT_SETUP = "HubRotary"      # the 4-axis wrap, about the hub's own axis
HUB_ROT_WCS = "HubRotaryWCS"     # that setup's origin, a Joint Origin at the hub's centre

# The lathe cycles the hub is roughed with, in the order a shop turns them. Named here because the
# reveal, the non-empty oracle and the no-warning read all address the same four.
HUB_TURN_CYCLES = ("TurnFace", "TurnRough", "TurnFinish", "TurnPart")
# What the roughing cycle leaves for the finishing pass, sent and read back through one spelling.
_ROUGH_ALLOWANCE = {"useStockToLeave": "true", "xStockToLeave": "0.5mm", "zStockToLeave": "0.5mm"}
# The rotary families, each wrapped about the hub axis.
HUB_ROT_OPS = ("RotContour", "RotPocket", "RotFinish")

# The dump post the rotary program is read back through, and the tool axis a wrap writes: MEASURED
# over 47011 5D rows of the hub's three rotary operations, every one reads 90 deg off +Z - the
# cutter stands square to the axis its passes turn about, and no 3-axis row is written at all.
_DUMP_POST = "dump.cps"
_ROT_PROGRAM = "4003"
_ROT_AXIS_DEG, _ROT_AXIS_TOL = 90.0, 0.5

# The 4-axis machine the rotary wrap turns about, run-stamped so two overlapping runs never collide
# on one Local-library name. The act takes it back out once the setup holds its own copy.
_ROT_MACHINE = "SweepMach4Axis " + time.strftime("%Y%m%d-%H%M%S")

# The hub's axis in world X, clear of the band the other pinned chunks occupy: FeatureCameo's
# pattern alone reaches x 390 and the scale beats end at x 1347, both far past the coordinates
# their steps are written with. A chunk holding an XZ sketch stays exactly where it is authored.
_HUB_X = 1200.0

# The profile, as the ledger measured it: a flange, a shaft with a groove, a stub with a chamfer.
# Radii across, depths down the shaft - the sketch's +Y is world -Z, so a depth here is a NEGATIVE
# world z (the flange top sits at z 0 and the part ends at z -90).
_FLANGE_R, _FLANGE_T = 40.0, 20.0
_SHAFT_R = 25.0
_GROOVE_R, _GROOVE_TOP, _GROOVE_BOT = 22.0, 45.0, 50.0
_SHAFT_END = 70.0
_STUB_R, _STUB_END = 15.0, 88.0
_CHAMFER_R, _PART_END = 13.0, 90.0
_AXIS_LEN = 100.0
_BOLT_R = 32.0                   # the bolt circle the four counterbored holes sit on
_SECTOR_HALF = 6.0               # the flange pocket's half width, on that same circle

# The closed outline, walked from the axis at the flange top round to the axis at the part end.
_HUB_POINTS = [[_HUB_X, 0.0], [_HUB_X + _FLANGE_R, 0.0], [_HUB_X + _FLANGE_R, _FLANGE_T],
               [_HUB_X + _SHAFT_R, _FLANGE_T], [_HUB_X + _SHAFT_R, _GROOVE_TOP],
               [_HUB_X + _GROOVE_R, _GROOVE_TOP], [_HUB_X + _GROOVE_R, _GROOVE_BOT],
               [_HUB_X + _SHAFT_R, _GROOVE_BOT], [_HUB_X + _SHAFT_R, _SHAFT_END],
               [_HUB_X + _STUB_R, _SHAFT_END], [_HUB_X + _STUB_R, _STUB_END],
               [_HUB_X + _CHAMFER_R, _PART_END], [_HUB_X, _PART_END]]

# Which of the outline's thirteen lines run across and which run down. line:0 is the construction
# axis, so the outline starts at line:1 and closes on line:13.
_HUB_ACROSS = (1, 3, 5, 7, 9, 12)
_HUB_DOWN = (2, 4, 6, 8, 10, 13)
# The radii and the depths, each measured from line:1:start - the vertex on the axis at the flange
# top, which every other vertex is placed against.
_HUB_RADII = (("line:1:end", _FLANGE_R), ("line:4:start", _SHAFT_R), ("line:6:start", _GROOVE_R),
              ("line:10:start", _STUB_R), ("line:11:end", _CHAMFER_R))
_HUB_DEPTHS = (("line:2:end", _FLANGE_T), ("line:4:end", _GROOVE_TOP),
               ("line:6:end", _GROOVE_BOT), ("line:8:end", _SHAFT_END),
               ("line:10:end", _STUB_END), ("line:12:start", _PART_END))

# The keyway, on the same axis plane: an OBROUND 6 mm wide and 16 mm tip to tip, its centre line on
# the shaft wall so the cut reaches 3 mm in, and extruded 3 mm either side of that plane. Drawn as a
# slot, its two ends are half-round at the cutter's own radius rather than square inside corners.
_KEY_R, _KEY_TOP, _KEY_WIDE, _KEY_LONG, _KEY_HALF = 22.0, 52.0, 6.0, 16.0, 3.0
# kind='slot' takes the two CAP CENTRES, so the centre-to-centre span is the tip-to-tip length less
# one full width, and the radius is half of it.
_KEY_CX, _KEY_CY = _HUB_X + _KEY_R + _KEY_WIDE / 2, _KEY_TOP + _KEY_WIDE / 2
_KEY_SPAN = _KEY_LONG - _KEY_WIDE

# The round pocket beside the flange's arc slot, on the same bolt circle: a circle a cutter can
# enter, where a square region leaves inside corners no end mill reaches.
_ROUND_X, _ROUND_Y, _ROUND_R = _HUB_X - 17.0, 27.0, 4.0

# The inclined flat, on the shaft's plain stretch between _FLANGE_T and _GROOVE_TOP - the one run
# carrying neither, and away from the threaded stub. Its outline is drawn in the tilted sketch's own
# coordinates: +X is world Z, so the first number is a depth and the second a radius (see _tilt_v).
_WEDGE_TOP, _WEDGE_BOT = 25.0, 41.0      # where the flat meets the wall, and where it bottoms out
_WEDGE_DEPTH = 7.0                       # how far under the wall the bottom end sits
_WEDGE_OVER = 21.0                       # the outline's top edge, a millimetre clear of the flange
_WEDGE_OUT = 40.0                        # its outboard edge, well clear of the shaft wall
# Half the cut's width, wider than the shaft's own chord at that depth (sqrt(25^2 - 18^2) = 17.4),
# so the flat is bounded by the round wall on both sides rather than by two walls of its own.
_WEDGE_CUT = 20.0
# The outline's two points ON the ramp: the bottom end sits _WEDGE_DEPTH under the wall, and the top
# one runs the same ramp out past the wall, so the flat starts exactly at _WEDGE_TOP.
_WEDGE_R_BOT = _SHAFT_R - _WEDGE_DEPTH
_WEDGE_R_TOP = _SHAFT_R + _WEDGE_DEPTH * (_WEDGE_TOP - _WEDGE_OVER) / (_WEDGE_BOT - _WEDGE_TOP)
# Where the shaft wall the datum is swung about reads its centroid: the groove splits the shaft into
# two walls of one radius, and this is the upper one's middle.
_SHAFT_UPPER_Z = -(_FLANGE_T + _GROOVE_TOP) / 2

# The shop set the hub's job is cut with, in the order the adds land - the order a create row picks
# a cutter by. (from_type, diameter in mm, or None to keep the sample's own).
_HUB_TOOLS = (("face mill", 50.0), ("flat end mill", 10.0), ("ball end mill", 6.0),
              ("chamfer mill", 10.0), ("thread mill", 12.0), ("slot mill", 10.0),
              ("turning general", None), ("turning grooving", None), ("turning threading", None),
              ("flat end mill", 6.0))

# That set is added in ONE call, so every create row picks its cutter by position off the base index
# the add published. One index per cutter, read off the order above rather than typed twice.
_FLAT_AT = [t for t, _d in _HUB_TOOLS].index("flat end mill")
_BALL_AT = [t for t, _d in _HUB_TOOLS].index("ball end mill")
_CHAMFER_AT = [t for t, _d in _HUB_TOOLS].index("chamfer mill")
_SLOT_AT = [t for t, _d in _HUB_TOOLS].index("slot mill")
_TURN_AT = [t for t, _d in _HUB_TOOLS].index("turning general")
_GROOVE_AT = [t for t, _d in _HUB_TOOLS].index("turning grooving")
_THREAD_INSERT_AT = [t for t, _d in _HUB_TOOLS].index("turning threading")
# The small mill, indexed from the END: it is the SECOND flat end mill in the set, and the first is
# what .index() answers with.
_FLAT6_AT = len(_HUB_TOOLS) - 1 - [t for t, _d in reversed(_HUB_TOOLS)].index("flat end mill")


def _fully_constrained(name, constraints, dimensions):
    """sketch_get: the sketch closed every degree of freedom, and the counts it took to do it.
    Constraints outnumbering dimensions is the recipe's own bar; a count that drifts says a row
    landed on geometry this act did not mean."""
    def check(p):
        return _measured(f"'{name}' fully constrained by {constraints} constraints and "
                         f"{dimensions} dimensions",
                         {"is_fully_constrained": p.get("is_fully_constrained"),
                          "constraint_count": p.get("constraint_count"),
                          "dimension_count": p.get("dimension_count"),
                          "profile_count": p.get("profile_count")},
                         p.get("is_fully_constrained") is True
                         and p.get("constraint_count") == constraints
                         and p.get("dimension_count") == dimensions)
    return check


def _profile_frame(name):
    """sketch_create on XZ: the name it landed under, and the frame's own +Y - which runs along
    world -Z, so every depth below is authored positive and the part hangs under the flange top."""
    def check(p):
        y = (p.get("frame") or {}).get("y_world")
        return _measured(f"'{name}' on a frame whose +Y runs along world -Z",
                         {"sketch_name": p.get("sketch_name"), "plane": p.get("plane"),
                          "y_world": y},
                         p.get("sketch_name") == name and isinstance(y, list) and len(y) == 3
                         and _near(y[2], -1.0, 1e-3))
    return check


def _drew(count):
    """sketch_add_geometry: the collection's own delta - what actually landed, not what was asked
    for (a closed_path repeats its first point, so thirteen vertices draw thirteen segments)."""
    def check(p):
        r = (p.get("results") or [{}])[0]
        return _measured(f"{count} curve(s) drawn",
                         {"curves_added": r.get("curves_added"), "label": r.get("label")},
                         r.get("curves_added") == count)
    return check


def _rim_edge(x, y, z, radius, tol=0.1):
    """find_geometry(kind='circular_edge'): the ONE edge found, told apart by its own centre. Two
    edges of this radius bound the flange and 'nearest_to' only ORDERS them, so the centre is what
    says the top one answered - the bottom rim would round the wrong corner."""
    def check(p):
        ms = p.get("matches") or []
        m = ms[0] if ms else {}
        pos = m.get("position")
        return _measured(f"one r{radius} circular edge centred at {[x, y, z]}",
                         {"count": len(ms), "position": pos, "radius": m.get("radius"),
                          "kind": m.get("kind")},
                         len(ms) == 1 and m.get("kind") == "circular_edge"
                         and _near(m.get("radius"), radius, tol)
                         and isinstance(pos, list) and len(pos) == 3
                         and all(_near(v, w, tol) for v, w in zip(pos, (x, y, z))))
    return check


def _slot_spine(p):
    """sketch_get(include_entities) on a slot: three lines of which exactly ONE is construction -
    the centre-to-centre spine the caps sit on, and the only line whose ends ARE the cap centres.
    Which index it landed at is read here rather than assumed: a solid side line is vertical too and
    spans the same length, so a dimension addressed at one lands the slot half a width off."""
    ents = p.get("entities") or []
    lines = [e for e in ents if e.get("type") == "line"]
    spine = [e for e in lines if e.get("construction")]
    return _measured("one construction line of three - the slot's own spine",
                     {"line_count": len(lines), "spine": [e.get("id") for e in spine],
                      "arc_count": len([e for e in ents if e.get("type") == "arc"])},
                     len(lines) == 3 and len(spine) == 1
                     and len([e for e in ents if e.get("type") == "arc"]) == 2)


def _spine_ref(p):
    """That construction line's own id off the same read - the 'line:<n>' the rows below address."""
    return next(e["id"] for e in p["entities"]
                if e.get("type") == "line" and e.get("construction"))


def _wall_at(radius, z, tol=0.5):
    """find_geometry(kind='cylinder_face'): the ONE wall of that radius, told apart by where its
    centroid sits down the hub's axis - the groove splits the shaft into two walls of one radius,
    so a query that landed on the lower one reads a different height here."""
    def check(p):
        ms = p.get("matches") or []
        m = ms[0] if ms else {}
        pos = m.get("position") or [0, 0, 999]
        return _measured(f"one r{radius} cylinder wall centred at z {z:g} mm",
                         {"count": len(ms), "kind": m.get("kind"), "radius": m.get("radius"),
                          "position": m.get("position")},
                         len(ms) == 1 and m.get("kind") == "cylinder_face"
                         and _near(m.get("radius"), radius, tol) and _near(pos[2], z, tol))
    return check


def _tilt_frame(p):
    """sketch_create on the 30 deg datum: the frame it landed with, judged on the three readings the
    wedge's own coordinates stand on. Its +X runs along world Z while the origin and +Y contribute
    NOTHING to Z, so a wedge point's first coordinate IS its world z - which is why those are
    written as depths; and the normal is the XZ plane's own, swung 30 degrees about the shaft axis."""
    f = p.get("frame") or {}
    o, x, y, n = f.get("origin_mm"), f.get("x_world"), f.get("y_world"), f.get("normal")
    triple = [v for v in (o, x, y, n) if isinstance(v, list) and len(v) == 3]
    return _measured("the tilt sketch maps its +X onto world Z alone, normal swung 30 deg off XZ",
                     {"origin_mm": o, "x_world": x, "y_world": y, "normal": n},
                     len(triple) == 4 and _near(x[2], 1.0, 1e-3)
                     and _near(o[2], 0.0, 1e-3) and _near(y[2], 0.0, 1e-3)
                     and _near(n[0], -0.5, 1e-3) and _near(n[1], 0.866025, 1e-3))


def _tilt_v(p):
    """Where the hub's axis sits along the tilted sketch's own +Y, computed from the frame that
    sketch landed with. The datum's parametric origin is a long way from the part, so the wedge's
    coordinates are measured from here rather than written as literals."""
    f = p["frame"]
    o, y = f["origin_mm"], f["y_world"]
    return sum((a - b) * c for a, b, c in zip((_HUB_X, 0.0, 0.0), o, y))


def _hub_box(p):
    """model_inspect on the hub: the turned envelope the revolve left - the flange's own diameter
    across both axes and the part's length down Z. Every cut below it takes material from INSIDE
    that envelope, so this reads the same after each one."""
    return _measured(f"hub bbox {2 * _FLANGE_R} x {2 * _FLANGE_R} x {_PART_END} mm",
                     {"x": p.get("x"), "y": p.get("y"), "z": p.get("z"),
                      "center": p.get("center"), "units": p.get("units")},
                     _near(p.get("x"), 2 * _FLANGE_R, 0.5) and _near(p.get("y"), 2 * _FLANGE_R, 0.5)
                     and _near(p.get("z"), _PART_END, 0.5))


def _threaded(designation, length):
    """model_thread: the call-out and the extent read back off the feature, with 'internal' derived
    from the face's own out-of-material normal rather than echoed - a stub reads external."""
    def check(p):
        return _measured(f"a {designation} external thread {length} mm long",
                         {"designation": p.get("designation"), "length": p.get("length"),
                          "internal": p.get("internal"), "location": p.get("location"),
                          "thread_type": p.get("thread_type")},
                         p.get("designation") == designation and p.get("length") == length
                         and p.get("internal") is False and bool(p.get("thread_type")))
    return check


def _labelled(text, height):
    """sketch_set_text(create=True): the count re-read off sketchTexts, and the width the created
    text's own boundingBox measured - which is what says a string landed in the sketch rather than
    a call returning ok. Nothing cuts this text; it is drawn on the flange-top outline sketch."""
    def check(p):
        return _measured(f"'{text}' drawn {height} mm high in the flange-top sketch",
                         {"created": p.get("created"), "text": p.get("text"),
                          "sketch_text_count": p.get("sketch_text_count"),
                          "measured_width": p.get("measured_width")},
                         p.get("created") is True and p.get("text") == text
                         and p.get("sketch_text_count") == 1
                         and _num(p.get("measured_width")) and p["measured_width"] > 0)
    return check


def _pocket_cut(p):
    """model_extrude(profile_index='all') on the flange top: BOTH regions cut, and neither one
    inside the other - an 'enclosed_profile_indices' key would mean the circle landed in the
    sector's own band, where its cut leaves nothing to see."""
    return _measured("the sector and the round pocket cut as two separate regions",
                     {"profiles_extruded": p.get("profiles_extruded"),
                      "profile_index": p.get("profile_index"),
                      "enclosed_profile_indices": p.get("enclosed_profile_indices"),
                      "result_bodies": p.get("result_bodies")},
                     p.get("profiles_extruded") == 2 and "enclosed_profile_indices" not in p
                     and bool(p.get("result_bodies")))


def _hub_tools_landed(p):
    """cam_edit_tools(action='list'): the cutters this act added, read back off the library in the
    order they were handed in - the order is the contract a create row selects a cutter by, so it
    is asserted as the slice starting where the library's own count said the first one landed."""
    base = _RECALL.get("hub_tool_base")
    want = list(_HUB_TOOLS)
    rows = p.get("tools") or []
    got = [(r.get("type"), r.get("diameter_mm"))
           for r in rows[base:base + len(want)]] if base is not None else []
    ok_ = len(got) == len(want) and all(
        g[0] == w[0] and (w[1] is None or _near(g[1], w[1], 0.01)) for g, w in zip(got, want))
    return _measured(f"{len(want)} hub cutters at index {base}, in the order they were added",
                     {"tool_count": p.get("tool_count"), "base": base, "landed": got}, ok_)


def _z_down(wcs, down):
    """One setup's own +Z, off the row's wcs block: 'down' asks for a frame whose Z runs along world
    -Z, which on this part is the end opposite the flange top."""
    z = (wcs or {}).get("z_world") or []
    return len(z) == 3 and _near(z[2], -1.0 if down else 1.0, 1e-3)


def _hub_setups(p):
    """cam_get's setups slice: the hub's job in the order a machinist runs it. The LATHE setup comes
    first, on the mill-turn machine and the turning WCS, with its own +Z running along world -Z -
    the flange in the chuck, the shaft and stub toward the tool. The milling setup comes second, its
    +Z the other way and its stock the shape the lathe left, carrying no machine (its blocker names
    that) on the stock-point WCS a top job is set from."""
    rows = [s for s in (p.get("setups") or [])
            if s.get("name") in (HUB_MILL_SETUP, HUB_TURN_SETUP)]
    by = {s.get("name"): s for s in rows}
    mill, turn = by.get(HUB_MILL_SETUP) or {}, by.get(HUB_TURN_SETUP) or {}
    mwcs, twcs = mill.get("wcs") or {}, turn.get("wcs") or {}
    return _measured(f"'{HUB_TURN_SETUP}' turns first, then '{HUB_MILL_SETUP}' mills its rest stock",
                     {"order": [s.get("name") for s in rows], "mill": mill, "turn": turn},
                     [s.get("name") for s in rows] == [HUB_TURN_SETUP, HUB_MILL_SETUP]
                     and turn.get("operation_type") == "TurningOperation"
                     and turn.get("machine") == _TURN_MACHINE
                     and turn.get("stock_mode") == "fixed_cylinder"
                     and twcs.get("origin_mode") == "turningOrigin"
                     and twcs.get("orientation_mode") == "axesXZ"
                     and _z_down(twcs, True)
                     and mill.get("operation_type") == "MillingOperation"
                     and mill.get("machine") is None
                     and mill.get("blocked_by") == ["no_machine_selected"]
                     and mill.get("stock_mode") == "previous_setup"
                     and mwcs.get("origin_mode") == "stockPoint"
                     and mwcs.get("orientation_mode") == "modelOrientation"
                     and _z_down(mwcs, False)
                     and mill.get("selected_models") == [HUB_COMP + ":1"]
                     and turn.get("selected_models") == [HUB_COMP + ":1"])


def _stock_mode_set(mode, was):
    """cam_edit_setup(stock_mode=...): the mode read BACK off Setup.stockMode after the write,
    beside the one the setup carried before it - a swallowed assignment errors in the tool."""
    def check(p):
        return _measured(f"stock mode '{mode}' (was '{was}')",
                         {"stock_mode_set": p.get("stock_mode_set"),
                          "was_stock_mode": p.get("was_stock_mode")},
                         p.get("stock_mode_set") == mode and p.get("was_stock_mode") == was)
    return check


def _turned_clean(setup):
    """cam_get_status once the act boundary has certified the lathe job: every cycle finished with NO
    warning and none of them cutting air. A solid of revolution turned from a cylinder of stock is
    the geometry these cycles are for, so a warning row here is a beat to fix, not a state to
    report."""
    def check(p):
        counts = p.get("counts") or {}
        return _measured(f"'{setup}' turned clean - no warning, no empty toolpath",
                         {"completed": p.get("completed"),
                          "operations_with_warnings": p.get("operations_with_warnings"),
                          "counts": counts, "empty_toolpaths": p.get("empty_toolpaths")},
                         p.get("completed") is True
                         and p.get("operations_with_warnings") == []
                         and counts.get("with_warnings") == 0
                         and p.get("empty_toolpaths") == [])
    return check


def _machine_built(name, template):
    """cam_create_machine: the machine re-resolved through the query cam_edit_setup assigns from,
    with the template it was built from and the library it landed in read back."""
    def check(p):
        return _measured(f"'{name}' built from the {template} template in the Local library",
                         {"created": p.get("created"), "name": p.get("name"),
                          "template": p.get("template"), "location": p.get("location"),
                          "model": p.get("model")},
                         p.get("created") is True and p.get("name") == name
                         and p.get("template") == template and p.get("location") == "local")
    return check


def _machine_deleted(name):
    """cam_delete_machine: the asset gone from a re-walk of the Local library's own assets, which is
    the whole claim - the setup keeps the copy Setup.machine took, and reads it after this."""
    def check(p):
        return _measured(f"'{name}' taken back out of the Local library",
                         {"deleted": p.get("deleted"), "machine": p.get("machine"),
                          "resolves_after_delete": p.get("resolves_after_delete"),
                          "local_assets_remaining": p.get("local_assets_remaining")},
                         p.get("deleted") is True and p.get("machine") == name
                         and p.get("resolves_after_delete") is False)
    return check


def _rotary_contract(operation):
    """cam_get(include=['parameters'], operation=...) on a rotary family: the 4-axis contract the
    operation carries. 'axisView_orientation_mode' is the axis the passes wrap about - axisZ is the
    hub's own axis, the line its profile was revolved around - and 'axisView_origin_mode' says the
    wrap turns about the SETUP's WCS, which this act bound to a Joint Origin at the hub's centre."""
    def check(p):
        params = p.get("parameters") or {}
        rows = [r for section in (params.get("sections") or {}).values() for r in section]
        by = {r.get("name"): r.get("expression") for r in rows}
        got = {k: by.get(k) for k in ("axisView_orientation_mode", "axisView_origin_mode",
                                      "useCone", "boundaryMode")}
        return _measured(f"'{operation}' wraps about the setup's own Z axis",
                         dict(got, parameter_count=params.get("parameter_count")),
                         params.get("operation") == operation
                         and "axisz" in str(got["axisView_orientation_mode"] or "").lower()
                         and "joborigin" in str(got["axisView_origin_mode"] or "").lower())
    return check


def _dumped_rotary(setup, program):
    """cam_post of the rotary wrap through the dump post: every motion row is a 5D one carrying a
    tool axis, and every one of those axes stands SQUARE to the axis the passes wrap about. A wrap
    that had collapsed onto a 3-axis job reads axis_rows 0 here, and one turning about the wrong
    axis reads angles away from 90 - neither of which the machining-time oracle can see."""
    def check(p):
        files = p.get("files") or []
        dump = _dump_reader.read_dump(files[0]["file_path"]) if files else None
        square, band = (_dump_reader.axis_band(dump, _ROT_AXIS_DEG, _ROT_AXIS_TOL) if dump
                        else (False, {}))
        return _measured(
            f"'{setup}' posted through {_DUMP_POST}, every tool axis square to the wrap axis",
            {"posted": p.get("posted"), "post_config": p.get("post_config"), "files": files,
             "strategy": dump and dump.strategy, "band": band},
            p.get("posted") is True and p.get("program_name") == program
            and p.get("file_count") == len(files) and len(files) == 1
            and _num(files[0].get("size_bytes")) and files[0]["size_bytes"] > 0
            and square)
    return check


def _setup_created(name, operation_type):
    """cam_create_setup: the name read back off the created Setup, its operation type, and a count
    of zero operations - a setup that arrives holding some is not the one this row made."""
    def check(p):
        return _measured(f"'{name}' created as a {operation_type} setup on the hub",
                         {"created": p.get("created"), "setup_name": p.get("setup_name"),
                          "operation_type": p.get("operation_type"), "models": p.get("models"),
                          "operation_count": p.get("operation_count")},
                         p.get("created") is True and p.get("setup_name") == name
                         and p.get("operation_type") == operation_type
                         and p.get("models") == [HUB_COMP + ":1"]
                         and p.get("operation_count") == 0)
    return check


def _one_line(sketch, constraint, lines):
    """One single-entity geometric-constraint row per line index."""
    return [("sketch_constrain", {"constraints": [{"constraint": constraint,
                                                   "entity_one": f"line:{i}"}],
                                  "sketch_name": sketch}, "ok", None) for i in lines]


def _spans(sketch, dim_type, anchor, rows):
    """One dimension row per (entity ref, value), each measured from the same anchor point."""
    return [("sketch_dimension", {"dimensions": [{"dim_type": dim_type, "entity_one": anchor,
                                                  "entity_two": ref,
                                                  "value": f"{value:g} mm"}],
                                  "sketch_name": sketch}, "ok", None) for ref, value in rows]


# ACT 8b: THE HUB - the competence world, drawn through the sketch recipes and turned solid.
_HUB = (
    [
        ("model_create_component", {"name": HUB_COMP, "activate": True}, _made_component, None),
        # THE TURNED OUTLINE, on the plane the part is symmetric about. The construction line is
        # the axis the revolve turns about; the outline closes on it, and the recipe's own shape
        # follows - constraints carry the rectilinear intent, dimensions carry the sizes.
        ("sketch_create", {"plane": "xz", "name": HUB_PROFILE}, _profile_frame(HUB_PROFILE), None),
        ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": _HUB_X, "y1": 0.0,
                                               "x2": _HUB_X, "y2": _AXIS_LEN,
                                               "is_construction": True}],
                                 "sketch_name": HUB_PROFILE}, _drew(1), None),
        # a closed_path ends its last segment on the first point's own SketchPoint (the seam is
        # welded), so the outline arrives with no free vertex at the seam.
        ("sketch_add_geometry", {"geometry": [{"kind": "closed_path", "points": _HUB_POINTS}],
                                 "sketch_name": HUB_PROFILE}, _drew(13), None),
    ]
    + _one_line(HUB_PROFILE, "horizontal", _HUB_ACROSS)
    + _one_line(HUB_PROFILE, "vertical", _HUB_DOWN)
    + [
        # the shaft is ONE diameter above and below the groove, said as a relation rather than as
        # the same number typed twice.
        ("sketch_constrain", {"constraints": [{"constraint": "collinear", "entity_one": "line:4",
                                               "entity_two": "line:8"}],
                              "sketch_name": HUB_PROFILE}, "ok", None),
        # the flange top sits ON the sketch's own X axis: a relation to the origin, not a zero
        # dimension, which is what puts the part's datum face at world z 0.
        ("sketch_constrain", {"constraints": [{"constraint": "horizontal_points",
                                               "entity_one": "point:0",
                                               "entity_two": "line:1:start"}],
                              "sketch_name": HUB_PROFILE}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "coincident",
                                               "entity_one": "line:0:start",
                                               "entity_two": "line:1:start"}],
                              "sketch_name": HUB_PROFILE}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "vertical", "entity_one": "line:0"}],
                              "sketch_name": HUB_PROFILE}, "ok", None),
        # the one typed coordinate the sketch carries: where the hub's axis stands in the field.
        ("sketch_dimension", {"dimensions": [{"dim_type": "horizontal_distance",
                                              "entity_one": "point:0",
                                              "entity_two": "line:1:start",
                                              "value": f"{_HUB_X:g} mm"}],
                              "sketch_name": HUB_PROFILE}, "ok", None),
    ]
    + _spans(HUB_PROFILE, "horizontal_distance", "line:1:start", _HUB_RADII)
    + _spans(HUB_PROFILE, "vertical_distance", "line:1:start", _HUB_DEPTHS)
    + [
        ("sketch_dimension", {"dimensions": [{"dim_type": "vertical_distance",
                                              "entity_one": "line:0:start",
                                              "entity_two": "line:0:end",
                                              "value": f"{_AXIS_LEN:g} mm"}],
                              "sketch_name": HUB_PROFILE}, "ok", None),
        # THE PROOF, before anything consumes the profile: every freedom closed, and the region the
        # revolve turns handed on as a handle rather than as a guessed index.
        ("sketch_get", {"sketch_name": HUB_PROFILE}, _fully_constrained(HUB_PROFILE, 16, 13),
         _prof("hub_profile")),
        ("model_revolve", lambda c: {"sketch_name": HUB_PROFILE,
                                     "profile_index": _ctx_get(c, "hub_profile",
                                                               "the hub's outline"),
                                     "axis": "line:0", "angle_deg": 360}, _revolved, None),
        # the framing pass has been watching a flat sketch from the front; from here the subject is
        # a solid, and every cut below lands inside this one frame.
        _watch(HUB_COMP + ":1"),
        # THE BOLT CIRCLE, on the flange top plane. The annulus and the four positions are drawn as
        # the job's own reference: the drill below is aimed at the same coordinates, and this sketch
        # is what says they are a symmetric pattern rather than four typed points.
        ("sketch_create", {"plane": "xy", "name": HUB_HOLES}, "ok", None),
        ("sketch_add_geometry", {"geometry": [{"kind": "point", "cx": _HUB_X + _BOLT_R,
                                               "cy": 0.0}],
                                 "sketch_name": HUB_HOLES}, _drew(1), None),
        ("sketch_add_geometry", {"geometry": [{"kind": "point", "cx": _HUB_X - _BOLT_R,
                                               "cy": 0.0}],
                                 "sketch_name": HUB_HOLES}, _drew(1), None),
        ("sketch_add_geometry", {"geometry": [{"kind": "point", "cx": _HUB_X, "cy": _BOLT_R}],
                                 "sketch_name": HUB_HOLES}, _drew(1), None),
        ("sketch_add_geometry", {"geometry": [{"kind": "point", "cx": _HUB_X, "cy": -_BOLT_R}],
                                 "sketch_name": HUB_HOLES}, _drew(1), None),
        ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": _HUB_X - _FLANGE_R,
                                               "y1": 0.0, "x2": _HUB_X + _FLANGE_R, "y2": 0.0,
                                               "is_construction": True}],
                                 "sketch_name": HUB_HOLES}, _drew(1), None),
        ("sketch_add_geometry", {"geometry": [{"kind": "line", "x1": _HUB_X, "y1": -_FLANGE_R,
                                               "x2": _HUB_X, "y2": _FLANGE_R,
                                               "is_construction": True}],
                                 "sketch_name": HUB_HOLES}, _drew(1), None),
        ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": _HUB_X, "cy": 0.0,
                                               "radius": _FLANGE_R, "is_construction": True}],
                                 "sketch_name": HUB_HOLES}, _drew(1), None),
        ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": _HUB_X, "cy": 0.0,
                                               "radius": _SHAFT_R, "is_construction": True}],
                                 "sketch_name": HUB_HOLES}, _drew(1), None),
        ("sketch_constrain", {"constraints": [{"constraint": "horizontal",
                                               "entity_one": "line:0"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "vertical", "entity_one": "line:1"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "midpoint",
                                               "entity_one": "circle:0:center",
                                               "entity_two": "line:0"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "midpoint",
                                               "entity_one": "circle:0:center",
                                               "entity_two": "line:1"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "equal", "entity_one": "line:0",
                                               "entity_two": "line:1"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "concentric",
                                               "entity_one": "circle:1",
                                               "entity_two": "circle:0"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "horizontal_points",
                                               "entity_one": "point:0",
                                               "entity_two": "circle:0:center"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "coincident",
                                               "entity_one": "point:1",
                                               "entity_two": "line:0"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "coincident",
                                               "entity_one": "point:2",
                                               "entity_two": "line:0"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "coincident",
                                               "entity_one": "point:3",
                                               "entity_two": "line:1"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "coincident",
                                               "entity_one": "point:4",
                                               "entity_two": "line:1"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        # each opposite pair mirrors about the other axis, so ONE distance drives two holes.
        ("sketch_constrain", {"constraints": [{"constraint": "symmetry", "entity_one": "point:1",
                                               "entity_two": "point:2",
                                               "symmetry_line": "line:1"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_constrain", {"constraints": [{"constraint": "symmetry", "entity_one": "point:3",
                                               "entity_two": "point:4",
                                               "symmetry_line": "line:0"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_dimension", {"dimensions": [{"dim_type": "horizontal_distance",
                                              "entity_one": "point:0",
                                              "entity_two": "circle:0:center",
                                              "value": f"{_HUB_X:g} mm"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_dimension", {"dimensions": [{"dim_type": "horizontal_distance",
                                              "entity_one": "line:0:start",
                                              "entity_two": "line:0:end",
                                              "value": f"{2 * _FLANGE_R:g} mm"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_dimension", {"dimensions": [{"dim_type": "diameter", "entity_one": "circle:0",
                                              "value": f"{2 * _FLANGE_R:g} mm"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_dimension", {"dimensions": [{"dim_type": "diameter", "entity_one": "circle:1",
                                              "value": f"{2 * _SHAFT_R:g} mm"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_dimension", {"dimensions": [{"dim_type": "horizontal_distance",
                                              "entity_one": "circle:0:center",
                                              "entity_two": "point:1",
                                              "value": f"{_BOLT_R:g} mm"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_dimension", {"dimensions": [{"dim_type": "vertical_distance",
                                              "entity_one": "circle:0:center",
                                              "entity_two": "point:3",
                                              "value": f"{_BOLT_R:g} mm"}],
                              "sketch_name": HUB_HOLES}, "ok", None),
        ("sketch_get", {"sketch_name": HUB_HOLES}, _fully_constrained(HUB_HOLES, 13, 6), None),
        # the flange top, MEASURED before it is drilled: one planar face at the axis facing +Z,
        # which is what separates it from the flange's underside at the same centroid in x and y.
        ("find_geometry", {"target": HUB_COMP, "kind": "planar_face",
                           "nearest_to": [_HUB_X, 0, 0], "max_results": 1},
         _face_up_at(_HUB_X, 0, 0), _fg("hub_top")),
        ("model_hole", lambda c: {"hole_type": "counterbore",
                                  "face": _ctx_get(c, "hub_top", "the flange top"),
                                  "points": [[_HUB_X + _BOLT_R, 0, 0], [_HUB_X - _BOLT_R, 0, 0],
                                             [_HUB_X, _BOLT_R, 0], [_HUB_X, -_BOLT_R, 0]],
                                  "points_space": "world", "diameter": "6.6 mm",
                                  "cbore_diameter": "11 mm", "cbore_depth": "6.5 mm",
                                  "extent": "through", "tip_angle": "118 deg"},
         _drilled(4), None),
        ("find_geometry", {"target": HUB_COMP, "kind": "circular_edge", "radius": _FLANGE_R,
                           "nearest_to": [_HUB_X, 0, 0], "max_results": 1},
         _rim_edge(_HUB_X, 0, 0, _FLANGE_R), _fg("hub_rim")),
        ("model_fillet", lambda c: {"edges": [_ctx_get(c, "hub_rim", "the flange rim")],
                                    "radius": 1.5}, _filleted, None),
        # THE KEYWAY, on the same axis plane the outline is drawn on. A slot arrives carrying the
        # four tangents and the parallel that hold its shape, so what is left to close is where its
        # centre line stands, how long it runs and how wide the caps are.
        ("sketch_create", {"plane": "xz", "name": HUB_KEYWAY}, "ok", None),
        ("sketch_add_geometry", {"geometry": [{"kind": "slot", "x1": _KEY_CX, "y1": _KEY_CY,
                                               "x2": _KEY_CX, "y2": _KEY_CY + _KEY_SPAN,
                                               "radius": _KEY_WIDE / 2}],
                                 "sketch_name": HUB_KEYWAY}, _drew(3), None),
        # WHICH line is the spine, read off the sketch rather than counted on: every row below
        # addresses the index this read hands back.
        ("sketch_get", {"sketch_name": HUB_KEYWAY, "include_entities": True}, _slot_spine,
         ("key_spine", _recall("key_spine", _spine_ref))),
        ("sketch_constrain",
         lambda c: {"constraints": [{"constraint": "vertical",
                                     "entity_one": _ctx_get(c, "key_spine", "the slot's spine")}],
                    "sketch_name": HUB_KEYWAY}, "ok", None),
        ("sketch_dimension",
         lambda c: {"dimensions": [{
             "dim_type": "horizontal_distance", "entity_one": "point:0",
             "entity_two": _ctx_get(c, "key_spine", "the slot's spine") + ":start",
             "value": f"{_KEY_CX:g} mm"}],
             "sketch_name": HUB_KEYWAY}, "ok", None),
        ("sketch_dimension",
         lambda c: {"dimensions": [{
             "dim_type": "vertical_distance", "entity_one": "point:0",
             "entity_two": _ctx_get(c, "key_spine", "the slot's spine") + ":start",
             "value": f"{_KEY_CY:g} mm"}],
             "sketch_name": HUB_KEYWAY}, "ok", None),
        ("sketch_dimension",
         lambda c: {"dimensions": [{
             "dim_type": "vertical_distance",
             "entity_one": _ctx_get(c, "key_spine", "the slot's spine") + ":start",
             "entity_two": _ctx_get(c, "key_spine", "the slot's spine") + ":end",
             "value": f"{_KEY_SPAN:g} mm"}],
             "sketch_name": HUB_KEYWAY}, "ok", None),
        ("sketch_dimension", {"dimensions": [{"dim_type": "radius", "entity_one": "arc:0",
                                              "value": f"{_KEY_WIDE / 2:g} mm"}],
                              "sketch_name": HUB_KEYWAY}, "ok", None),
        ("sketch_get", {"sketch_name": HUB_KEYWAY}, _fully_constrained(HUB_KEYWAY, 6, 4), None),
        ("model_extrude", {"sketch_name": HUB_KEYWAY, "profile_index": 0, "distance": _KEY_HALF,
                           "symmetric": True, "operation": "cut"}, _extruded, None),
        # the cut stayed inside the turned envelope: a keyway takes material out of the shaft, so
        # the part's own extent is what it was before the slot was cut.
        ("model_inspect", {"target": HUB_COMP + ":1"}, _hub_box, None),
        # THE INCLINED FLAT, on the SHAFT. The datum swings the axis plane 30 degrees about the
        # shaft's OWN axis, and the wedge sits in the band between the flange underside and the
        # groove - the stub is left a clean threaded cylinder, which the r15 read below states.
        ("find_geometry", {"target": HUB_COMP, "kind": "cylinder_face", "radius": _SHAFT_R,
                           "nearest_to": [_HUB_X, 0, _SHAFT_UPPER_Z], "max_results": 1},
         _wall_at(_SHAFT_R, _SHAFT_UPPER_Z), _fg("hub_shaft")),
        ("model_construction", lambda c: {"kind": "plane", "mode": "at_angle_on_face",
                                          "face": _ctx_get(c, "hub_shaft", "the shaft wall"),
                                          "plane": "xz", "angle": 30, "name": HUB_TILT},
         _datum("plane"), None),
        ("sketch_create", {"plane": HUB_TILT, "name": HUB_WEDGE}, _tilt_frame,
         ("hub_tilt_v", _recall("hub_tilt_v", _tilt_v))),
        ("sketch_add_geometry",
         lambda c: {"geometry": [{
             "kind": "closed_path",
             "points": [[-_WEDGE_OVER,
                         _ctx_get(c, "hub_tilt_v", "the axis in tilt coords") + _WEDGE_R_TOP],
                        [-_WEDGE_BOT,
                         _ctx_get(c, "hub_tilt_v", "the axis in tilt coords") + _WEDGE_R_BOT],
                        [-_WEDGE_BOT,
                         _ctx_get(c, "hub_tilt_v", "the axis in tilt coords") + _WEDGE_OUT],
                        [-_WEDGE_OVER,
                         _ctx_get(c, "hub_tilt_v", "the axis in tilt coords") + _WEDGE_OUT]]}],
             "sketch_name": HUB_WEDGE},
         _drew(4), None),
        ("model_extrude", {"sketch_name": HUB_WEDGE, "profile_index": 0, "distance": _WEDGE_CUT,
                           "symmetric": True, "operation": "cut"}, _extruded, None),
        ("model_inspect", {"target": HUB_COMP + ":1"}, _hub_box, None),
        # THE FLANGE POCKET, drawn twice over as the shop drawing has it: the outline plus its
        # label, then the sketch that is actually cut.
        ("sketch_create", {"plane": "xy", "name": HUB_FLANGE_TOP}, "ok", None),
        ("sketch_add_geometry", {"geometry": [{"kind": "center_point_arc_slot", "cx": _HUB_X,
                                               "cy": 0.0, "x1": _HUB_X + _BOLT_R, "y1": 0.0,
                                               "x2": _HUB_X, "y2": _BOLT_R,
                                               "radius": _SECTOR_HALF,
                                               "is_construction": True}],
                                 "sketch_name": HUB_FLANGE_TOP}, _drew(5), None),
        ("sketch_set_text", {"text": "HUB", "sketch_name": HUB_FLANGE_TOP, "create": True,
                             "height": 3.5, "x": _HUB_X + 27, "y": 17, "angle_deg": 122},
         _labelled("HUB", 3.5), None),
        ("sketch_create", {"plane": "xy", "name": HUB_FLANGE_POCKET}, "ok", None),
        ("sketch_add_geometry", {"geometry": [{"kind": "center_point_arc_slot", "cx": _HUB_X,
                                               "cy": 0.0, "x1": _HUB_X + _BOLT_R, "y1": 0.0,
                                               "x2": _HUB_X, "y2": _BOLT_R,
                                               "radius": _SECTOR_HALF}],
                                 "sketch_name": HUB_FLANGE_POCKET}, _drew(5), None),
        # the round pocket sits in the quadrant the sector does NOT sweep, so both regions cut
        # something a viewer can see.
        ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": _ROUND_X, "cy": _ROUND_Y,
                                               "radius": _ROUND_R}],
                                 "sketch_name": HUB_FLANGE_POCKET}, _drew(1), None),
        ("model_extrude", {"sketch_name": HUB_FLANGE_POCKET, "profile_index": "all",
                           "distance": -4, "operation": "cut"}, _pocket_cut, None),
        ("model_inspect", {"target": HUB_COMP + ":1"}, _hub_box, None),
        # THE STUB, read across the WHOLE hub: exactly one cylinder face of its radius, which is
        # what says nothing has been cut across it - a notch would split that wall into two.
        ("find_geometry", {"target": HUB_COMP, "kind": "cylinder_face", "radius": _STUB_R,
                           "max_results": 4}, _matched(1, "cylinder_face"), _fg("hub_thread_face")),
        ("model_thread", lambda c: {"faces": [_ctx_get(c, "hub_thread_face", "the stub wall")],
                                    "designation": "M30x2", "length": 15, "location": "low"},
         _threaded("M30x2", 15.0), None),
        ("model_inspect", {"target": HUB_COMP + ":1"}, _hub_box, None),
        # the whole design's timeline, read once the last hub feature has landed: nothing in it
        # computed into an error or a warning.
        ("design_get", {}, lambda p: p["timeline_healthy"] is True, None),
        ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ]
)


# ACT 10c4: the hub's own CAM job - the shop's tool set and the two setups, created where the
# document library's own count says the first of them lands.
_HUB_JOB = [
    _watch(HUB_COMP + ":1"),
    # the from_type vocabulary, read before the adds: a spelling this installation does not carry
    # reds HERE, naming it, instead of inside the add that used it.
    ("cam_edit_tools", {"action": "list_types", "scope": "document"},
     _types_offered(*[t for t, _d in _HUB_TOOLS]), None),
    # the base index the adds below land at: the count of the cutters the earlier CAM acts left in
    # this document's library, which is what every create row below selects its tool against.
    ("cam_edit_tools", {"action": "list", "scope": "document"},
     lambda p: _num(p.get("tool_count")) and p["tool_count"] >= 1,
     ("hub_tool_base", _recall("hub_tool_base", lambda p: p["tool_count"]))),
    ("cam_edit_tools", {"action": "add", "scope": "document",
                        "add_tools": [dict({"from_type": t},
                                           **({"diameter": f"{d:g} mm"} if d else {}))
                                      for t, d in _HUB_TOOLS]},
     lambda p: p.get("added") == len(_HUB_TOOLS)
     and p.get("tool_count") == _RECALL.get("hub_tool_base") + len(_HUB_TOOLS), None),
    ("cam_edit_tools", {"action": "list", "scope": "document"}, _hub_tools_landed, None),
    # THE LATHE JOB FIRST, because that is the order the part is made in: the hub is turned from a
    # cylinder of stock before anything is milled into it. A turning setup of the other
    # operation_type, read back off the Setup itself as the API's own enum name, and carrying no
    # machine yet - which is the blocker the setups slice publishes for it.
    ("cam_create_setup", {"models": [HUB_COMP + ":1"], "name": HUB_TURN_SETUP,
                          "operation_type": "turning"},
     _setup_created(HUB_TURN_SETUP, "turning"), None),
    ("cam_get", {},
     _setup_row(HUB_TURN_SETUP, ["no_machine_selected"], operation_type=_TURNING_TYPE), None),
    # the mill-turn machine, assigned through the strip the library machines with a simulation model
    # need - and the same setups read again, with the blocker gone.
    ("cam_edit_setup", {"setup": HUB_TURN_SETUP, "machine": _TURN_MACHINE,
                        "machine_strip_simulation": True},
     lambda p: p.get("machine_set") == _TURN_MACHINE, None),
    ("cam_get", {}, _setup_row(HUB_TURN_SETUP, []), None),
    # WHICH END THE TOOL COMES AT. Created plain, this setup's own +Z runs along world +Z - the
    # same end the milling job works from, with the stub in the chuck. Flipping it swings the frame
    # onto world -Z, so the flange sits in the chuck and the shaft and stub face the tool.
    ("cam_edit_setup", {"setup": HUB_TURN_SETUP,
                        "parameters": {"wcs_orientation_flipZ": "true"}},
     _param_landed("wcs_orientation_flipZ", "true"), None),
    # and the zero a turner sets: the MODEL's own front face rather than the stock's, which in that
    # flipped frame is the stub end.
    ("cam_edit_setup", {"setup": HUB_TURN_SETUP,
                        "parameters": {"wcs_origin_turning": "'model front'"}},
     _param_landed("wcs_origin_turning", "model front"), None),
    # the turning setup's OWN parameters: the stock mode a turned job is cut from and the origin its
    # WCS is measured from, read off the setup rather than off the call that made it.
    ("cam_get", {"include": ["parameters"], "setup": HUB_TURN_SETUP},
     _turning_stock(HUB_TURN_SETUP), None),
    ("cam_get", {"include": ["strategies"], "setup": HUB_TURN_SETUP},
     _offers(HUB_TURN_SETUP, "turning_face", "turning_profile_roughing",
             "turning_profile_finishing", "turning_part"), None),
    # THE ROUGHING CYCLES, in the order a shop turns them: face the end, rough the profile, finish
    # it, then part the piece off with the grooving insert. None is given a selection - the poll
    # after this act is what says each of them generated something to cut.
    ("cam_create_operation",
     lambda c: {"setup": HUB_TURN_SETUP, "strategy": "turning_face", "name": HUB_TURN_CYCLES[0],
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _TURN_AT,
                "generate": False},
     _op_named(HUB_TURN_SETUP, "turning_face", HUB_TURN_CYCLES[0]), None),
    ("cam_create_operation",
     lambda c: {"setup": HUB_TURN_SETUP, "strategy": "turning_profile_roughing",
                "name": HUB_TURN_CYCLES[1], "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _TURN_AT,
                "generate": False},
     _op_named(HUB_TURN_SETUP, "turning_profile_roughing", HUB_TURN_CYCLES[1]), None),
    ("cam_create_operation",
     lambda c: {"setup": HUB_TURN_SETUP, "strategy": "turning_profile_finishing",
                "name": HUB_TURN_CYCLES[2], "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _TURN_AT,
                "generate": False},
     _op_named(HUB_TURN_SETUP, "turning_profile_finishing", HUB_TURN_CYCLES[2]), None),
    # THE ALLOWANCE THE FINISHING PASS TAKES, on the cycle that pass follows, all three in ONE call.
    # MEASURED: TurnRough ships useStockToLeave true, so nothing here is gated; TurnFinish ships it
    # false with its two allowance rows behind it - the unlocked_here shape, pinned on the deburr.
    ("cam_edit_operation", {"operation": HUB_TURN_CYCLES[1], "parameters": _ROUGH_ALLOWANCE},
     _landed_in_one_call(_ROUGH_ALLOWANCE), None),
    # MEASURED on this hub: the finishing pass generates carrying "Lead-Out has been modified due to
    # a gouge with the remaining stock" while its exit move is on, and no warning once it is off -
    # which is the reading ACT 10c4b's no-warning row stands on. It is the last cycle on that face.
    ("cam_edit_operation", {"operation": HUB_TURN_CYCLES[2], "parameters": {"doLeadOut": "false"}},
     _param_landed("doLeadOut", "false"), None),
    ("cam_create_operation",
     lambda c: {"setup": HUB_TURN_SETUP, "strategy": "turning_part", "name": HUB_TURN_CYCLES[3],
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _GROOVE_AT,
                "generate": False},
     _op_named(HUB_TURN_SETUP, "turning_part", HUB_TURN_CYCLES[3]), None),
    # THE MILLING JOB SECOND, on what the lathe leaves: its stock is the preceding setup's rest,
    # which is what makes the two one process rather than two jobs on one model.
    ("cam_create_setup", {"models": [HUB_COMP + ":1"], "name": HUB_MILL_SETUP},
     _setup_created(HUB_MILL_SETUP, "milling"), None),
    ("cam_edit_setup", {"setup": HUB_MILL_SETUP, "stock_mode": "previous_setup"},
     _stock_mode_set("previous_setup", "relative_box"), None),
    ("cam_get", {}, _hub_setups, None),
    # launched behind every write this act makes, and certified by the act boundary's poll.
    ("cam_generate", {"target": HUB_TURN_SETUP, "skip_valid": False},
     _launched_on(HUB_TURN_SETUP), None),
]


# ACT 10c4b: the turned part read, once the boundary poll has certified that generation - the hub
# framed once, each cycle shown alone in that frame, then the reads the lathe job stands on and the
# NC the shipped turning post writes from it.
_HUB_TURNED_READ = [
    _watch(HUB_COMP + ":1"),
] + _reveal(list(HUB_TURN_CYCLES)) + [
    ("cam_get_status", {"target": HUB_TURN_SETUP}, _turned_clean(HUB_TURN_SETUP), None),
    ("cam_get", {"include": ["time"], "setup": HUB_TURN_SETUP},
     _all_cut(HUB_TURN_SETUP, len(HUB_TURN_CYCLES), names=list(HUB_TURN_CYCLES)), None),
    # the readiness verdict is what the machine assignment bought - a setup carrying a blocker is
    # refused that wording.
    ("cam_get", {"include": ["operations"], "setup": HUB_TURN_SETUP},
     _setup_ready(HUB_TURN_SETUP, len(HUB_TURN_CYCLES)), None),
    # THE LATHE PROGRAM, posted from the post library this installation SHIPS - the scope that
    # reaches a turning post.
    ("cam_post", {"scope": HUB_TURN_SETUP, "post": "fanuc turning", "post_scope": "fusion",
                  "output_folder": EXPORT_DIR + "/nc", "program_name": "3001"},
     _posted_turning(HUB_TURN_SETUP, "3001"), None),
]


# ACT 10c13: THE ROTARY WRAP, on the hub's own axis. A 4-axis machine and a setup whose WCS sits on
# the hub's centre are what a rotary claim stands on: the passes wrap about that axis, so a part
# whose shape IS a revolve about it is the geometry these three families are for.
_HUB_ROTARY = [
    _watch(HUB_COMP + ":1"),
    ("cam_create_machine", {"name": _ROT_MACHINE, "template": "generic_4_axis",
                            "vendor": "Fusion-Essentials"},
     _machine_built(_ROT_MACHINE, "generic_4_axis"), None),
    # the hub's own centre: the layout pass deals the hub a cell a metre out in the field, so the
    # rotary axis runs through a Joint Origin there rather than through the world origin.
    ("joint_create_origin", {"anchor": "bbox_center", "bbox_target": HUB_COMP + ":1",
                             "orient_axis": "z", "name": HUB_ROT_WCS},
     _joint_origin_computed(HUB_ROT_WCS), None),
    ("cam_create_setup", {"models": [HUB_COMP + ":1"], "name": HUB_ROT_SETUP},
     _setup_created(HUB_ROT_SETUP, "milling"), None),
    ("cam_edit_setup", {"setup": HUB_ROT_SETUP, "wcs": {"origin": HUB_ROT_WCS}},
     lambda p: bool(p["wcs_set"]["origin"]["bound_entities"]), None),
    ("cam_edit_setup", {"setup": HUB_ROT_SETUP, "machine": _ROT_MACHINE},
     lambda p: p.get("machine_set") == _ROT_MACHINE, None),
    # Setup.machine takes a COPY, so the library asset has done its job - the run leaves the Local
    # library as it found it, and the setup below still reads the machine.
    ("cam_delete_machine", {"name": _ROT_MACHINE, "confirm_name": _ROT_MACHINE},
     _machine_deleted(_ROT_MACHINE), None),
    ("cam_get", {}, _setup_row(HUB_ROT_SETUP, []), None),
    ("cam_get", {"include": ["strategies"], "setup": HUB_ROT_SETUP},
     _offers(HUB_ROT_SETUP, "rotary_contour", "rotary_pocket", "rotary_finishing"), None),
    ("cam_create_operation",
     lambda c: {"setup": HUB_ROT_SETUP, "strategy": "rotary_contour", "name": HUB_ROT_OPS[0],
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _BALL_AT,
                "generate": False},
     _op_named(HUB_ROT_SETUP, "rotary_contour", HUB_ROT_OPS[0]), None),
    ("cam_create_operation",
     lambda c: {"setup": HUB_ROT_SETUP, "strategy": "rotary_pocket", "name": HUB_ROT_OPS[1],
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _FLAT_AT,
                "generate": False},
     _op_named(HUB_ROT_SETUP, "rotary_pocket", HUB_ROT_OPS[1]), None),
    ("cam_create_operation",
     lambda c: {"setup": HUB_ROT_SETUP, "strategy": "rotary_finishing", "name": HUB_ROT_OPS[2],
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _BALL_AT,
                "generate": False},
     _op_named(HUB_ROT_SETUP, "rotary_finishing", HUB_ROT_OPS[2]), None),
    # THE 4-AXIS CONTRACT, read off the operation itself: which axis the passes wrap about and what
    # they turn around. Nothing here writes it - the read is the record.
    ("cam_get", {"include": ["parameters"], "setup": HUB_ROT_SETUP, "operation": HUB_ROT_OPS[0]},
     _rotary_contract(HUB_ROT_OPS[0]), None),
    ("cam_generate", {"target": HUB_ROT_SETUP, "skip_valid": False},
     _launched_on(HUB_ROT_SETUP), None),
]


# ACT 10c14: the rotary families read, behind their own boundary poll - the same reveal, the
# non-empty oracle over the three, then WHERE the wrap put the tool axis.
_HUB_ROTARY_READ = [
    _watch(HUB_COMP + ":1"),
] + _reveal(list(HUB_ROT_OPS)) + [
    ("cam_get", {"include": ["time"], "setup": HUB_ROT_SETUP},
     _all_cut(HUB_ROT_SETUP, len(HUB_ROT_OPS), names=list(HUB_ROT_OPS)), None),
    # THE ROTARY PROGRAM. Measured, 'brother speedio' and 'brother' both refuse it - "requires a
    # machine configuration for 5-axis simultaneous toolpath" - so the dump post is what carries the
    # axis rows this beat judges.
    ("cam_post", {"scope": HUB_ROT_SETUP, "post": _DUMP_POST, "post_scope": "fusion",
                  "output_folder": EXPORT_DIR + "/dump", "program_name": _ROT_PROGRAM},
     _dumped_rotary(HUB_ROT_SETUP, _ROT_PROGRAM), None),
]
