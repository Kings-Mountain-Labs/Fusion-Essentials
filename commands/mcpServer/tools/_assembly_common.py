# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The occurrence WORLD-POSE sampler an assembly write is judged by: where every target part sat
before the mutation, and which of them moved across it - translation AND rotation."""

from ._common import safe
# The same before/after occurrence-position reader joint_at_geometry publishes 'moved_by' from,
# over one shared solver-noise tolerance.
from .joint_at_geometry import _MOVE_TOL_CM, _move_delta, _occ_origin      # noqa: F401

MAP_BLURB = (
    "the occurrence WORLD-POSE sampler an assembly write is judged by: _constraint_positions - "
    "every target's translation and basis BEFORE the mutation, one that will not read ABSENT "
    "rather than zeroed; _constraint_moves - which moved across it (translation plus "
    "_axes_rotation_deg, the flip a translation-only row hides), and whether ANY target was "
    "sampled on both sides")


def _occ_axes(occ):
    """The occurrence's world basis axes as three unit tuples, or None when unreadable - the
    ROTATION half of the moved verdict (a flip=true relationship can rotate a part 180 deg while
    translating it barely at all; a translation-only row implied a placement that was not what
    happened - measured)."""
    cs = safe(lambda: occ.transform2.getAsCoordinateSystem())
    if not cs:
        return None
    axes = []
    for v in cs[1:4]:
        t = (safe(lambda v=v: v.x), safe(lambda v=v: v.y), safe(lambda v=v: v.z))
        if any(c is None for c in t):
            return None
        axes.append(t)
    return tuple(axes)


def _axes_rotation_deg(a, b):
    """The largest angle (deg) any basis axis swung between two _occ_axes reads; None if either is
    absent."""
    if not a or not b:
        return None
    import math as _m
    worst = 0.0
    for (ax, ay, az), (bx, by, bz) in zip(a, b):
        dot = max(-1.0, min(1.0, ax * bx + ay * by + az * bz))
        worst = max(worst, _m.degrees(_m.acos(dot)))
    return worst


def _constraint_positions(targets):
    """{label: ((x, y, z) cm, axes-or-None)} - the WORLD translation and basis of every target
    occurrence that reads one. An occurrence whose transform cannot be read is ABSENT from the map,
    never a zero: the moved verdict is only computed where both sides were actually sampled."""
    out = {}
    for label, occ in targets.items():
        if occ is None:
            continue
        pos = _occ_origin(occ)
        if pos is not None:
            out[label] = (pos, _occ_axes(occ))
    return out


def _constraint_moves(before, targets):
    """([{occurrence, distance_mm, direction, rotation_deg?}], measured) for the constrained
    occurrences. 'measured' is False when no target could be sampled on BOTH sides of the add, where
    the verdict is UNKNOWN rather than "nothing moved"."""
    rows, measured = [], False
    for label, occ in targets.items():
        if occ is None or label not in before:
            continue
        after = _occ_origin(occ)
        if after is None:
            continue
        measured = True
        was_pos, was_axes = before[label]
        delta = _move_delta(was_pos, after)
        rot = _axes_rotation_deg(was_axes, _occ_axes(occ))
        if delta or (rot is not None and rot > 0.1):
            row = dict(occurrence=label, **(delta or {"distance_mm": 0.0, "direction": None}))
            if rot is not None and rot > 0.1:
                row["rotation_deg"] = round(rot, 2)
            rows.append(row)
    return rows, measured
