# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""ACT rows: WHERE the toolpath cut - the hub's flange wall read back out of Autodesk's dump post.

Every other CAM beat proves a toolpath EXISTS. These post one 2D contour on the hub's flange wall
through dump.cps, read the .dmp with _dump_reader, and judge the motion rows against the stock box
the dump states and a floor measured off the model - in the dump's own millimetres. The extension
act's simultaneous job rides the same post behind the machining_extension tier, where the tool axis
is what there is to judge - `_MX_DUMP`, which runs as ACT 10c6b, behind that act's own poll."""

import _dump_reader
from verify_acts_cam import MACHINING_EXTENSION, _MX_SETUP, _launched_on, _op_named, _selected
from verify_acts_hub import (
    HUB_COMP, HUB_MILL_SETUP, _FLANGE_R, _FLANGE_T, _HUB_TOOLS, _HUB_X, _rim_edge)
from verify_core import EXPORT_DIR, _RECALL, _ctx_get, _measured, _near, _needs, _num, _recall, _watch

# The dump post resolves by FILE name out of the library this installation ships, never by the
# 'Dumper' description it carries.
_DUMP_POST = "dump.cps"
_DUMP_DIR = EXPORT_DIR + "/dump"
_DUMP_OP = "FlangeWall"          # the 2D contour whose posted dump the verdicts below judge
_DUMP_PROGRAM = "4001"
_MX_PROGRAM = "4002"

# The hub's cutter set is added in one call, so an operation picks its cutter by position off the
# base index that call published.
_FLAT_MILL_AT = [t for t, _d in _HUB_TOOLS].index("flat end mill")

# What a caller compares before it judges any distance the dump carries.
_MM = {"operation:metric": 1, "operation:tool_unit": "millimeters"}

# The event kinds this reader takes no row off, measured on a posted 2D contour. The arcs are not
# among them: a contour round the flange's round wall posts five of them, and the verdicts below
# judge those beside the linear moves.
_UNREAD_3AX = ["onClose", "onFeedMode", "onMovement", "onOpen", "onSection", "onSectionEnd"]

# The floor is compared in the band the two reads' own decimals leave, not to the bit.
_FLOOR_TOL = 0.01

# The one excursion this contour's rows make from the stock box: the feed-down starts 1.0 mm over
# its top face, and nothing else leaves it.
_FEED_IN_SLACK = 1.0

# The tilt this simultaneous job reads, rounded up: its tool axis follows a wall drafted 12 deg off
# vertical and reads 78 deg off +Z; no read here hands back a machine tilt range to judge against.
_MX_TILT_LIMIT = 80.0

# The row kinds a dump can carry, for the census a beat reports.
_KINDS = ("linear5d", "rapid5d", "linear", "rapid", "circular")


def _dumped(setup, program):
    """cam_post through the dump post: the file it wrote, re-read by _dump_reader row by row.

    The contour this judges runs a ROUND wall, so the file has to carry arcs as well as linear
    moves, and the centre is read off every one of them - a file with no arc in it is a different
    cut from the one these verdicts were measured on."""
    def check(p):
        files = p.get("files") or []
        dump = _dump_reader.read_dump(files[0]["file_path"]) if files else None
        rows = dump.rows if dump else []
        cuts = [r for r in rows if r["kind"] == "linear"]
        arcs = [r for r in rows if r["kind"] == "circular"]
        moves = [r for r in rows if r["kind"] == "rapid"]
        return _measured(
            f"'{setup}' posted through {_DUMP_POST}, its onLinear/onCircular/onRapid events read "
            "as rows",
            {"posted": p.get("posted"), "post_config": p.get("post_config"),
             "files": files, "strategy": dump and dump.strategy,
             "linear_rows": len(cuts), "circular_rows": len(arcs), "rapid_rows": len(moves),
             "skipped": dump and dump.skipped,
             "first_cut": cuts[0] if cuts else None, "first_arc": arcs[0] if arcs else None,
             "first_move": moves[0] if moves else None},
            p.get("posted") is True and p.get("scope") == "setup"
            and p.get("post_scope") == "fusion" and p.get("program_name") == program
            and _DUMP_POST in str(p.get("post_config") or "")
            and p.get("file_count") == len(files) and len(files) == 1
            and _num(files[0].get("size_bytes")) and files[0]["size_bytes"] > 0
            and dump is not None and dump.strategy == "contour2d"
            and sorted(dump.skipped) == _UNREAD_3AX
            and bool(cuts) and bool(moves) and bool(arcs)
            and all(_num(r["feed"]) and not _dump_reader._has_axis(r) for r in cuts + arcs)
            and all(r["feed"] is None and not _dump_reader._has_axis(r) for r in moves)
            # the centre is what an arc carries that a linear move does not - read here so a row
            # that landed the endpoint alone cannot pass as an arc.
            and all(_num(r.get("cx")) and _num(r.get("cy")) and _num(r.get("cz")) for r in arcs))
    return check


def _verdicts(p):
    """model_inspect beside the posted dump: the units first, then envelope, floor and tilt."""
    dump = _dump_reader.read_dump(_RECALL["hub_dump_path"])
    if dump.units() != _MM or p.get("units") != "mm":
        return _measured("both reads state millimetres before a distance is judged",
                         {"dump_units": dump.units(), "inspect_units": p.get("units")}, False)
    part, top_world = dump.box("part"), (p.get("max_point") or {}).get("z")
    rim_world = _RECALL["hub_dump_rim"]["position"][2]
    if part is None or not _num(top_world):
        return _measured("the dump states a part box and the model its own top",
                         {"part_box": part, "max_point": p.get("max_point")}, False)
    # The two frames differ by a translation, so the dump's own part box is what carries a world
    # height into it - same part, same extent, one offset, which the extent below asserts.
    anchor = part[1][2] - top_world
    # Measured, one row of this cut sits outside the stated stock box: the feed-down, 1.0 mm over
    # its top face. That is the whole slack - the four walls and the floor are judged at zero.
    inside, env = _dump_reader.envelope(dump, tol=0.0, up_tol=_FEED_IN_SLACK)
    above_rim, rim = _dump_reader.floor(dump, rim_world + anchor, tol=_FLOOR_TOL)
    above_top = _dump_reader.floor(dump, part[1][2], tol=_FLOOR_TOL)[0]
    upright, tilted = _dump_reader.tilt(dump, 90.0)
    return _measured(
        f"the cut sits in the stock box, lands ON the rim at z {rim_world:g} mm, and states no "
        "tool axis",
        {"part_box": part, "anchor": anchor, "up_tol": env["up_tol"],
         "stock_box": env["stock_box"], "cuts": env["cutting_rows"],
         "outside": env["outside_count"], "first_outside": env["first_outside"],
         "first_outside_point": env["first_outside_point"],
         "unread_motion": env["unread_motion"], "unverified_paths": env["unverified_paths"],
         "floor_z": rim["floor_z"], "lowest_z": rim["lowest_z"],
         "reaches_below_the_flange_top": not above_top, "axis_rows": tilted["axis_rows"]},
        inside and above_rim and upright
        # ON the rim, not merely above it: floor() is one-sided, so a pass that stopped short
        # clears it - the lowest cut has to MEET the rim, both ways, within the same band.
        and _near(rim["lowest_z"], rim["floor_z"], _FLOOR_TOL)
        # and it really descended the wall: a contour that never got below the flange top would
        # clear a floor set THERE, and this one does not.
        and not above_top and tilted["axis_rows"] == 0
        and _num(p.get("z")) and _near(part[1][2] - part[0][2], p["z"], 0.5))


def _dumped_5d(setup, program, limit_deg):
    """cam_post of a simultaneous job: the tool axis its 5D rows state, against limit_deg."""
    def check(p):
        files = p.get("files") or []
        dump = _dump_reader.read_dump(files[0]["file_path"]) if files else None
        rows = dump.rows if dump else []
        kinds = {k: sum(1 for r in rows if r["kind"] == k) for k in _KINDS}
        upright, tilted = _dump_reader.tilt(dump, limit_deg) if dump else (False, {})
        # One program over a simultaneous setup writes 3-axis moves BESIDE the 5D ones - measured,
        # three sections and rows of both shapes - so what this beat judges is the tool axis: it
        # has to be there, and it has to leave +Z. The envelope over that mix is reported only.
        return _measured(
            f"'{setup}' posted through {_DUMP_POST}, every tool axis within {limit_deg:g} deg of +Z",
            {"posted": p.get("posted"), "post_config": p.get("post_config"), "files": files,
             "strategy": dump and dump.strategy, "kinds": kinds,
             "skipped": dump and dump.skipped, "tilt": tilted,
             "envelope": dump and _dump_reader.envelope(dump)[1]},
            p.get("posted") is True and p.get("program_name") == program
            and p.get("file_count") == len(files) and len(files) == 1
            and _num(files[0].get("size_bytes")) and files[0]["size_bytes"] > 0
            and dump is not None and upright and tilted.get("axis_rows", 0) > 0
            and _num(tilted.get("max_angle_deg")) and tilted["max_angle_deg"] > 0)
    return check


# ACT 10c5: the contour whose posted dump the oracle judges - one 2D pass round the flange's outer
# wall, aimed at the rim it cuts down to and generated behind the act boundary's poll.
_HUB_CONTOUR = [
    _watch(HUB_COMP + ":1"),
    ("cam_create_operation",
     lambda c: {"setup": HUB_MILL_SETUP, "strategy": "contour2d", "name": _DUMP_OP,
                "tool_scope": "document",
                "tool_index": _ctx_get(c, "hub_tool_base", "the hub tool base") + _FLAT_MILL_AT,
                "generate": False},
     _op_named(HUB_MILL_SETUP, "contour2d", _DUMP_OP), None),
    # the rim the wall ends on, MEASURED before it is machined: the flange top carries a round, so
    # a radius query at the flange top answers with the fillet's own edges instead.
    ("find_geometry", {"target": HUB_COMP, "kind": "circular_edge", "radius": _FLANGE_R,
                       "nearest_to": [_HUB_X, 0, -_FLANGE_T], "max_results": 1},
     _rim_edge(_HUB_X, 0, -_FLANGE_T, _FLANGE_R),
     ("hub_dump_rim", _recall("hub_dump_rim", lambda p: p["matches"][0]))),
    ("cam_select_geometry",
     lambda c: {"operation": _DUMP_OP, "selection": "chain",
                "handles": [_ctx_get(c, "hub_dump_rim", "the flange's bottom rim")["handle"]],
                "generate": False}, _selected(1), None),
    ("cam_generate", {"target": HUB_MILL_SETUP, "skip_valid": False},
     _launched_on(HUB_MILL_SETUP), None),
]


# ACT 10c6: the oracle itself, once the boundary poll has certified that generation - the post, the
# rows read back off the file, and the three verdicts over them.
_HUB_DUMP = [
    ("cam_post", {"scope": HUB_MILL_SETUP, "post": _DUMP_POST, "post_scope": "fusion",
                  "output_folder": _DUMP_DIR, "program_name": _DUMP_PROGRAM},
     _dumped(HUB_MILL_SETUP, _DUMP_PROGRAM),
     ("hub_dump_path", _recall("hub_dump_path", lambda p: p["files"][0]["file_path"]))),
    ("model_inspect", {"target": HUB_COMP + ":1"}, _verdicts, None),
]


# THE SIMULTANEOUS JOB through the same post, where the tool axis is the whole question: the 3-axis
# file above states none at all, and this one states one on every 5D row it carries. It reads the
# rail act's own setup, so it rides the harness that builds one, behind that act's boundary poll.
_MX_DUMP = [
    ("cam_post", {"scope": _MX_SETUP, "post": _DUMP_POST, "post_scope": "fusion",
                  "output_folder": _DUMP_DIR, "program_name": _MX_PROGRAM},
     _needs(MACHINING_EXTENSION, _dumped_5d(_MX_SETUP, _MX_PROGRAM, _MX_TILT_LIMIT)), None),
]
