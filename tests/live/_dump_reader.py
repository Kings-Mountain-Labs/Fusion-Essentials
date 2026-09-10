# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The dump-post reader: Autodesk's dump.cps writes every post event to a .dmp, and this turns one
into the post's parameters plus its motion rows - then judges WHERE the toolpath cut.

Numbers are the dump's own, unconverted; DumpFile.units() hands back the unit rows it states.
read_dump/parse_dump build a DumpFile; envelope, floor, tilt and axis_band are the four verdicts
over its rows, each returning (ok, facts). Pure Python - no adsk, so a test reads a posted file."""

import math
import re

MAP_BLURB = ("the dump-post reader: read_dump/parse_dump turn a dump.cps .dmp into parameters and "
             "motion rows; envelope/floor/tilt/axis_band are the (ok, facts) verdicts over those "
             "rows.")

# Circular events carry unnumbered sweep/plane details before the next numbered event.
_EVENT = re.compile(r"^\s*(-?\d+):\s*(?:EXPANDED\s+)?([A-Za-z0-9_]+)\((.*)\)\s*$")
_SWEEP = re.compile(r"^\s*sweep:\s*(\S+)deg\s*$")
_PLANE = re.compile(r"^\s*normal:.*\((XY|ZX|YZ)\)\s*$")
_PLANES = {"XY": ("x", "y", "z"), "ZX": ("z", "x", "y"), "YZ": ("y", "z", "x")}

# wire event -> (kind, xyz start, tool-axis start or None, feed index or None, arc-centre start or
# None), measured: onLinear5D(x, y, z, i, j, k, feed, _) eighth argument unread, onRapid5D, onLinear,
# onRapid, onCircular(clockwise, cx, cy, cz, x, y, z, feed).
_MOTION = {
    "onRapid5D": ("rapid5d", 0, 3, None, None),
    "onLinear5D": ("linear5d", 0, 3, 6, None),
    "onRapid": ("rapid", 0, None, None, None),
    "onLinear": ("linear", 0, None, 3, None),
    "onCircular": ("circular", 4, None, 7, 1),
}

# The rows envelope and floor judge: a cut has to stay inside the stock and above the floor, while
# a rapid is free to sit above both. An arc is a cut.
CUTTING_KINDS = ("linear5d", "linear", "circular")


def _split_args(text):
    """One event's comma-separated arguments, a comma inside a quoted value kept whole."""
    args, buf, quoted = [], [], False
    for ch in text:
        if quoted:
            quoted = ch != "'"
        elif ch == "'":
            quoted = True
        elif ch == ",":
            args.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    return args + [tail] if args or tail else []


def _value(text):
    """One argument as the dump states it: a quoted string unquoted, a number as a number."""
    if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def _num(text):
    """One argument as a finite float, or None."""
    try:
        value = float(text)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _row(spec, args):
    """One motion row, or None where the position, tool axis or arc centre did not read as
    numbers."""
    kind, point_at, axis_at, feed_at, centre_at = spec
    starts = [n for n in (point_at, axis_at, centre_at) if n is not None]
    if len(args) < max(starts) + 3:
        return None
    row = {"kind": kind}
    for keys, start in (("xyz", point_at), ("ijk", axis_at), (("cx", "cy", "cz"), centre_at)):
        if start is None:
            continue
        values = [_num(a) for a in args[start:start + 3]]
        if None in values:
            return None
        row.update(dict(zip(keys, values)))
    row["feed"] = _num(args[feed_at]) if feed_at is not None and len(args) > feed_at else None
    if feed_at is not None and row["feed"] is None:
        return None
    if kind == "circular":
        if args[0] not in ("true", "false", "1", "0"):
            return None
        row["clockwise"] = args[0] in ("true", "1")
    return row


def _has_axis(row):
    """True where the row states a tool axis - a 3-axis event states none."""
    return all(key in row for key in "ijk")


class DumpFile:
    """One parsed .dmp: the post's parameters, its motion rows, and the events left unread."""

    def __init__(self, parameters, rows, skipped):
        self.parameters = parameters
        self.rows = rows
        self.skipped = skipped

    @property
    def strategy(self):
        """The operation's strategy id as the dump states it, or None."""
        return self.parameters.get("operation-strategy")

    def units(self):
        """The two unit rows the dump states, uninterpreted - what a caller compares before it
        judges any distance this file carries."""
        return {key: self.parameters.get(key)
                for key in ("operation:metric", "operation:tool_unit")}

    def box(self, prefix):
        """The (lower, upper) xyz the dump states under '<prefix>-lower-*'/'<prefix>-upper-*'."""
        corners = []
        for end in ("lower", "upper"):
            values = [self.parameters.get(f"{prefix}-{end}-{axis}") for axis in "xyz"]
            if any(not isinstance(v, (int, float)) or isinstance(v, bool)
                   or _num(v) is None for v in values):
                return None
            corners.append(tuple(float(v) for v in values))
        if any(lo > hi for lo, hi in zip(*corners)):
            return None
        return tuple(corners)


def parse_dump(text):
    """A dump post's text as a DumpFile."""
    parameters, rows, skipped = {}, [], {}
    previous, arc = None, None
    for line in text.split("\n"):
        match = _EVENT.match(line)
        if match is None:
            if arc is not None:
                sweep, plane = _SWEEP.match(line), _PLANE.match(line)
                if sweep:
                    arc["sweep_deg"] = _num(sweep.group(1))
                if plane:
                    arc["plane"] = plane.group(1)
                if line.strip() == "spiral" or line.strip().startswith("helical pitch:"):
                    arc["unsupported_path"] = line.strip()
            continue
        arc = None
        event, args = match.group(2), _split_args(match.group(3))
        row = _row(_MOTION[event], args) if event in _MOTION else None
        if event == "onParameter" and len(args) >= 2:
            parameters[_value(args[0])] = _value(args[1])
        elif row is not None:
            if row["kind"] == "circular":
                row["start"] = previous
                arc = row
            rows.append(row)
            previous = {key: row[key] for key in "xyz"}
        else:
            skipped[event] = skipped.get(event, 0) + 1
            if event in _MOTION or event in ("onSection", "onSectionEnd"):
                previous = None
    return DumpFile(parameters, rows, skipped)


def read_dump(path):
    """The .dmp at `path` as a DumpFile."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_dump(fh.read())


def _inside(row, box, tol, up_tol):
    """True where a row's point sits within `tol` of the box, `up_tol` above its TOP face alone."""
    lower, upper = box
    point = (row["x"], row["y"], row["z"])
    return all(lower[n] - tol <= v <= upper[n] + (up_tol if n == 2 else tol)
               for n, v in enumerate(point))


def _axis_angle(row):
    """The axis angle off +Z, or None for nonfinite or zero-length axes."""
    axis = [row[key] for key in "ijk"]
    if any(_num(value) is None for value in axis):
        return None
    scale = max(abs(value) for value in axis)
    if not scale:
        return None
    i, j, k = (value / scale for value in axis)
    return math.degrees(math.acos(max(-1.0, min(1.0, k / math.hypot(i, j, k)))))


def _unread_motion(dump):
    """Known motion events whose arguments could not be read completely."""
    return {name: count for name, count in dump.skipped.items() if name in _MOTION}


def _arc_points(row):
    """Arc endpoints and swept coordinate extrema, or a reason its path is unverifiable."""
    start, plane, sweep = row.get("start"), row.get("plane"), row.get("sweep_deg")
    if row.get("unsupported_path"):
        return [], "spiral or helical arc is unsupported"
    if start is None or plane not in _PLANES or sweep is None:
        return [], "arc needs a start in this section, a primary plane and a finite sweep"
    if not 0 < sweep <= 360:
        return [], "arc sweep must be in (0, 360] degrees"
    a, b, fixed = _PLANES[plane]
    ca, cb = row["c" + a], row["c" + b]
    radius = math.hypot(start[a] - ca, start[b] - cb)
    end_radius = math.hypot(row[a] - ca, row[b] - cb)
    if (not math.isfinite(radius) or not radius
            or not math.isclose(radius, end_radius, rel_tol=1e-9, abs_tol=1e-6)
            or any(not math.isclose(p[fixed], row["c" + fixed], rel_tol=0, abs_tol=1e-6)
                   for p in (start, row))):
        return [], "arc endpoints and centre do not describe a planar circle"
    angle = math.atan2(start[b] - cb, start[a] - ca)
    direction = -1 if row["clockwise"] else 1
    end_angle = angle + direction * math.radians(sweep)
    # dump.cps rounds sweep degrees to six decimals; numbered coordinates are not rounded.
    slack = radius * math.radians(1e-6) + 1e-6
    if (abs(row[a] - (ca + radius * math.cos(end_angle))) > slack
            or abs(row[b] - (cb + radius * math.sin(end_angle))) > slack):
        return [], "arc sweep and direction do not reach its endpoint"
    points = [start, row]
    for quarter, (da, db) in enumerate(((1, 0), (0, 1), (-1, 0), (0, -1))):
        travel = (direction * (quarter * math.pi / 2 - angle)) % math.tau
        if travel <= math.radians(sweep + 1e-6):
            points.append({a: ca + radius * da, b: cb + radius * db, fixed: row[fixed]})
    return points, None


def _cut_paths(dump):
    """Cut rows with their extrema and any paths the reader cannot verify."""
    paths, unverified = [], []
    for index, row in enumerate(dump.rows):
        if row["kind"] not in CUTTING_KINDS:
            continue
        points, reason = _arc_points(row) if row["kind"] == "circular" else ([row], None)
        paths.append((row, points))
        if reason:
            unverified.append({"row_index": index, "reason": reason})
    return paths, unverified


def envelope(dump, tol=0.0, up_tol=None):
    """Verdict over cut endpoints and planar arc paths; up_tol relaxes only the stock's top."""
    box = dump.box("stock")
    up_tol = tol if up_tol is None else up_tol
    paths, unverified = _cut_paths(dump)
    outside = []
    if box:
        for row, points in paths:
            point = next((p for p in points if not _inside(p, box, tol, up_tol)), None)
            if point is not None:
                outside.append((row, point))
    unread = _unread_motion(dump)
    facts = {"stock_box": box, "cutting_rows": len(paths), "tol": tol, "up_tol": up_tol,
             "outside_count": len(outside), "first_outside": outside[0][0] if outside else None,
             "first_outside_point": outside[0][1] if outside else None,
             "unread_motion": unread, "unverified_paths": unverified}
    return (bool(box) and bool(paths) and not outside and not unread and not unverified
            and _num(tol) is not None and _num(up_tol) is not None), facts


def floor(dump, floor_z, tol=0.0):
    """Verdict: cut endpoints and planar arc paths stay at or above floor_z minus tol."""
    paths, unverified = _cut_paths(dump)
    depths = [point["z"] for _, points in paths for point in points]
    unread = _unread_motion(dump)
    finite = all(_num(z) is not None for z in depths)
    lowest = min(depths) if depths and finite else None
    facts = {"floor_z": floor_z, "tol": tol, "cutting_rows": len(paths), "lowest_z": lowest,
             "unread_motion": unread, "unverified_paths": unverified}
    return (lowest is not None and not unread and not unverified and _num(floor_z) is not None
            and _num(tol) is not None and lowest >= floor_z - tol), facts


def tilt(dump, limit_deg=90.0):
    """Verdict: every tool axis the file states lies within limit_deg (degrees) of +Z."""
    angles = [_axis_angle(r) for r in dump.rows if _has_axis(r)]
    measured = [a for a in angles if a is not None]
    # A 3-axis event states no tool axis, so a file holding only those reads 'axis_rows' 0 - a
    # caller that needs the file to BE five-axis reads that count, not the verdict.
    over = [a for a in measured if a > limit_deg]
    unread = _unread_motion(dump)
    facts = {"limit_deg": limit_deg, "axis_rows": len(angles),
             "unreadable_axes": len(angles) - len(measured),
             "max_angle_deg": max(measured) if measured else None, "unread_motion": unread}
    return (not over and len(measured) == len(angles) and not unread
            and _num(limit_deg) is not None), facts


def axis_band(dump, target_deg, tol):
    """Verdict: every row states a tool axis, and every one lies within tol of target_deg off +Z."""
    flat = [r for r in dump.rows if not _has_axis(r)]
    angles = [_axis_angle(r) for r in dump.rows if _has_axis(r)]
    measured = [a for a in angles if a is not None]
    unread = _unread_motion(dump)
    facts = {"target_deg": target_deg, "tol": tol, "axis_rows": len(angles),
             "unreadable_axes": len(angles) - len(measured), "rows_without_an_axis": len(flat),
             "angle_span_deg": (min(measured), max(measured)) if measured else None,
             "unread_motion": unread}
    return (bool(angles) and not flat and len(measured) == len(angles) and not unread
            and _num(target_deg) is not None and _num(tol) is not None
            and all(abs(a - target_deg) <= tol for a in measured)), facts
