# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Gate: every registered tool is accounted for in the live tool verification.

The live run (tests/live/tool_verify.py) needs a running Fusion, so it can't run in this
mock suite - but its COMPLETENESS can be checked here from the registry alone: every registered
tool must be covered by a STEP, excused in EXCLUDED, or listed in PENDING. A newly added tool that
is none of these fails here, so coverage can't silently decay as the tool surface grows. The three
tables are shrink-only in spirit (scripting a tool moves it PENDING -> STEPS); stale entries (a
name in a table that no longer registers) also fail, so the tables can't rot the other way.
"""

from conftest import load_tool_verify as _load_verify, register_all_tools


class TestToolVerifyComplete:
    def test_every_tool_is_covered_excluded_or_pending(self):
        verify = _load_verify()
        registered = {i.primitive.name for i in register_all_tools()}
        covered = {step[0] for step in verify.STEPS}
        excluded = set(verify.EXCLUDED)
        pending = set(verify.PENDING)

        unaccounted = sorted(registered - covered - excluded - pending)
        assert not unaccounted, (
            "Tools registered but not accounted for in tool_verify - script a STEP for each, "
            "excuse it in EXCLUDED, or add it to PENDING with intent:\n  " + "\n  ".join(unaccounted))

    def test_no_stale_table_entries(self):
        verify = _load_verify()
        registered = {i.primitive.name for i in register_all_tools()}
        named = {step[0] for step in verify.STEPS} | set(verify.EXCLUDED) | set(verify.PENDING)
        stale = sorted(named - registered)
        assert not stale, (
            "tool_verify tables name tools that are no longer registered (renamed/removed?) - "
            "drop them:\n  " + "\n  ".join(stale))

    def test_pending_and_covered_are_disjoint(self):
        # a tool scripted into STEPS must leave PENDING - otherwise the ledger lies about coverage.
        verify = _load_verify()
        covered = {step[0] for step in verify.STEPS}
        both = sorted(covered & set(verify.PENDING))
        assert not both, ("Tools are both scripted AND listed PENDING - remove them from PENDING:\n  "
                          + "\n  ".join(both))


def _footprints(verify, steps_of):
    """{chunk: [x0, x1, y0, y1]} over the acts, reading whichever step list steps_of returns. An
    axis a chunk never constrains (a sketch drawn edge-on) reads as zero extent at the origin, which
    is where its plane sits."""
    box = {}
    for name, _pre, narr, _fb in verify._ACT_PROGRAM:
        for step, chunk, _cursor, frame in verify._place_walk(steps_of(narr)):
            args = step[1]
            if chunk is None or not isinstance(args, dict):
                continue
            for x, y in verify._place_points(args, frame, step[0]):
                b = box.setdefault(chunk, [None, None, None, None])
                if x is not None:
                    b[0] = x if b[0] is None else min(b[0], x)
                    b[1] = x if b[1] is None else max(b[1], x)
                if y is not None:
                    b[2] = y if b[2] is None else min(b[2], y)
                    b[3] = y if b[3] is None else max(b[3], y)
    return {c: [v if v is not None else 0.0 for v in b] for c, b in box.items()}


def _hits(a, b):
    return a[0] <= b[1] and b[0] <= a[1] and a[2] <= b[3] and b[2] <= a[3]


class TestToolVerifyLayout:
    """The layout pass gives every chunk of the story a cell of its own. These are the properties a
    viewer depends on - one subject per frame - and the ones a new chunk can silently break."""

    def test_coordinate_makers_have_a_readable_placement(self):
        verify = _load_verify()
        missing = []
        for name, _pre, narrative, fallback in verify._ACT_PROGRAM:
            for lane, steps in (("narrative", narrative), ("fallback", fallback or [])):
                for step, _chunk, _cursor, frame in verify._place_walk(steps):
                    tool, args, expect, _save = step
                    expect = verify._unparked(expect)
                    deliberate_refusal = isinstance(expect, verify._Refusal) or expect == "refused"
                    if (tool not in ("sketch_add_geometry", "sketch_add_3d_line")
                            or callable(args) or deliberate_refusal):
                        continue
                    if not isinstance(args, dict) or not verify._place_points(args, "xy", tool):
                        missing.append(f"{name} {lane}: {tool}")
        assert not missing, (
            "successful literal sketch geometry steps have no placement coordinate understood by "
            "verify_layout._place_points:\n  " + "\n  ".join(missing))

    def test_no_two_placed_chunks_share_ground(self):
        # A declared JOINT GROUP is exempt: its members are one assembly, dealt one cell and one
        # offset on purpose, and a joint is about to stack them anyway. The invariant is that two
        # INDEPENDENT chunks never share ground.
        verify = _load_verify()
        box = _footprints(verify, lambda narr: verify._placed(narr, verify._SLOTS))
        fam = verify._JOINT_FAMILY
        placed = sorted((c, b) for c, b in box.items() if c in verify._SLOTS)
        clashes = [f"{a} {box[a]} overlaps {b} {box[b]}"
                   for i, (a, _ba) in enumerate(placed)
                   for b, _bb in placed[i + 1:]
                   if _hits(box[a], box[b])
                   and not (a in fam and fam[a] == fam.get(b))]
        assert not clashes, (
            "placed chunks land on each other, so a frame on one shows the other - the packer in "
            "tool_verify._place_slots must give each its own cell:\n  " + "\n  ".join(clashes))

    def test_no_placed_chunk_lands_on_a_pinned_one(self):
        # measured against the pinned chunks' own footprints, not against the pin constant - the
        # bracket, the vise and the CAM stock are what a stray cell would actually land on.
        verify = _load_verify()
        box = _footprints(verify, lambda narr: verify._placed(narr, verify._SLOTS))
        pinned = {c: b for c, b in box.items() if c not in verify._SLOTS}
        assert pinned, "no chunk is pinned - the origin world would be free to wander"
        clashes = [f"{c} {b} lands on pinned {p} {pinned[p]}"
                   for c, b in sorted(box.items()) if c in verify._SLOTS
                   for p in sorted(pinned) if _hits(b, pinned[p])]
        assert not clashes, (
            "placed chunks land on the origin world, where the bracket, the vise and the CAM "
            "stock live and cannot move:\n  " + "\n  ".join(clashes))

    def test_chunks_that_address_each_other_stay_together(self):
        # the failure this catches is silent and green: two sketches whose solids must overlap (the
        # combine pair) drift apart, the combine still runs, and it joins nothing.
        verify = _load_verify()
        authored = _footprints(verify, lambda narr: narr)
        split = []
        for name, _pre, narr, _fb in verify._ACT_PROGRAM:
            for _step, chunk, cursor, _frame in verify._place_walk(narr):
                if not chunk or not cursor or chunk == cursor:
                    continue
                if chunk not in authored or cursor not in authored:
                    continue
                if not _hits(authored[chunk], authored[cursor]):
                    continue
                if verify._SLOTS.get(chunk, (0, 0)) != verify._SLOTS.get(cursor, (0, 0)):
                    split.append(f"{name}: {cursor} and {chunk} are one body but move apart")
        assert not split, (
            "chunks that share ground and address each other were placed in different cells:\n  "
            + "\n  ".join(sorted(set(split))))

    def test_no_frame_names_an_entity_the_story_already_deleted(self):
        # view_set REFUSES a focus it cannot resolve, so a camera row aimed at something a later
        # act deleted is a hard failure - and one that only shows up live, after three minutes of
        # run.
        verify = _load_verify()
        gone, offenders = {}, []
        for name, _pre, narr, _fb in verify._ACT_PROGRAM:
            for step in narr:
                args = step[1] if isinstance(step[1], dict) else {}
                if step[0] == "design_delete_occurrence" and isinstance(args.get("occurrence"), str):
                    gone.setdefault(args["occurrence"], name)
                if step[0] == "view_set" and args.get("focus"):
                    focus = args["focus"]
                    for nm in (focus if isinstance(focus, list) else [focus]):
                        if nm in gone:
                            offenders.append(f"{name}: frames '{nm}', deleted in {gone[nm]}")
        assert not offenders, (
            "camera rows name entities the story has already deleted - view_set refuses a focus it "
            "cannot resolve:\n  " + "\n  ".join(offenders))

    def test_placement_is_a_rigid_translation(self):
        # every position in a step moves by ONE offset: a shift that dropped or scaled a coordinate
        # would leave the chunk's internal geometry - and its probe points - subtly wrong.
        verify = _load_verify()
        skewed = []
        for name, _pre, narr, _fb in verify._ACT_PROGRAM:
            moved = verify._placed(narr, verify._SLOTS)
            assert len(moved) == len(narr)
            frames = [f for _s, _c, _cur, f in verify._place_walk(narr)]
            for before, after, frame in zip(narr, moved, frames):
                if not isinstance(before[1], dict):
                    continue
                was = verify._place_points(before[1], frame, before[0])
                now = verify._place_points(after[1], frame, before[0])
                if len(was) != len(now):
                    skewed.append(f"{name}: {before[0]} lost a position in the move")
                    continue
                for axis, i in (("x", 0), ("y", 1)):
                    moves = {round(b[i] - a[i], 9) for a, b in zip(was, now)
                             if a[i] is not None and b[i] is not None}
                    if len(moves) > 1:
                        skewed.append(
                            f"{name}: {before[0]} moved its {axis} by {sorted(moves)}")
        assert not skewed, ("placement is not a pure translation:\n  " + "\n  ".join(skewed))
