# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The layout and framing engine: where each chunk builds, and what the camera is looking at.

Two passes verify_program.py drives over the act program at import, plus the sketch hoisting that
runs before them:

  `_sketches_first` lifts every sketch that can be drawn on a bare origin plane into one phase, and
  `_sketch_reading_order` walks that phase left to right;
  `_place_slots` deals every chunk a cell of its own in a field laid out in narrative order, and
  `_placed`/`_place_shift` translate each step's authored coordinates into it - a rigid translation,
  which is why a read-back asserts a landing position through `_px`/`_py` rather than against the
  authored number;
  `_framed` inserts a camera row wherever the next subject is not already on screen, sized off the
  chunk footprints `_placed_boxes` measured, widened by any `_MEASURED_BOX` row a run recorded.

A chunk is the unit all three share: the sketch or component a step works on, named by
`_place_owner`. tool_verify.py re-exports this surface.
"""

import re

from verify_core import _JOINT_STATIONS, _group_of, _watch

# The cell each chunk was dealt, filled by verify_program.py once the acts are defined - THE
# object the placement pass, _px/_py and the layout lint all read, so it is updated in place
# rather than rebound.
_SLOTS = {}


# The steps that first put a BODY in a freshly created component - the moment there is something to
# frame. model_construction is deliberately NOT one: a datum plane is not geometry a viewer can see,
# and the body it is drawn for arrives a few steps later.
# The steps that first put geometry ON a sketch - the moment it becomes something to look at.
_SKETCH_MAKERS = ("sketch_add_geometry", "sketch_add_3d_line", "sketch_set_text",
                  "sketch_insert_svg", "sketch_project")

_BODY_MAKERS = ("model_extrude", "model_revolve", "model_loft", "model_sweep", "model_pipe",
                "model_base_feature", "surface_extrude", "surface_revolve", "surface_patch",
                "mesh_insert")

# view_set frames a subject at this multiple of its own size - the tool's own view_set._FRAME_MARGIN.
# Modelled here so the sweep can tell what the standing frame already shows and leave the camera
# alone when the answer is "this"; a larger number here calls an off-screen subject on screen.
_FRAME_MARGIN = 2.0
# How much bigger than its subject a frame reaches for context, and the floor under that for a
# subject with no measurable size. Proportional, not absolute: a constant neighbourhood frames a
# small sketch at a few percent of the viewport and a large assembly too tight.
_FRAME_CONTEXT = 3.5
_FRAME_MIN_SPAN = 260.0
# The most a single neighbour may stretch that neighbourhood. Without a ceiling one distant part
# drags the frame out to the whole field, where every named subject reads as a speck.
_FRAME_STRETCH = 1.4
# How many recent neighbours to lend a subject whose own position is unknown, purely for scale.
_FRAME_FALLBACK_NEIGHBOURS = 3

# The most subjects one frame may name. The boxes this pass reasons with are built from AUTHORED
# SKETCH COORDINATES, not from geometry: a ring whose sketch is a point at the origin is 160 mm of
# real body, and a revolve or a pattern reaches further still. So every span computed here is a
# lower bound, and a long focus list quietly frames far more than the arithmetic predicts - graded
# by eye, every single-subject frame read well and the 5-to-8 subject frames were whole-field
# photographs. Keeping the list short is the guard that does not depend on the model being right.
_FRAME_MAX_SUBJECTS = 6
# Sketches are watched a HANDFUL at a time rather than one by one: a sketch command is quick, and a
# camera move per command is more motion than the work is worth. The span cap is what keeps that
# honest - five neighbouring sketches in one row share a frame, five scattered ones do not.
_FRAME_SKETCH_GROUP = 10
_FRAME_SKETCH_SPAN = 560.0
# How much wider a JOINT frame reaches than a build frame - a mate is watched, not inspected.
_FRAME_RELATION_WIDEN = 2.2

# The sketch planes that exist from the start. A sketch on one of these can be drawn at any time; a
# sketch on a named datum or a face cannot exist before the body that datum is derived from.
_ORIGIN_PLANES = ("xy", "xz", "yz")

# Sketch tools whose result depends on what the sketch holds when they run, so a sketch any of them
# touches cannot be drawn up front. A dimension or a constraint is NOT one of these: it names its
# operands, and they travel with the curves.
_SKETCH_ORDER_BOUND = ("sketch_edit_curve", "sketch_copy", "sketch_move", "sketch_set_text",
                       "sketch_delete_entity", "sketch_project", "sketch_insert_svg")


def _sketches_first(program, after):
    """Move every sketch that CAN be drawn before anything is solid into one phase, and return
    (that phase, the program without it).

    A profile has to exist before the feature that consumes it - but nothing says it has to be drawn
    JUST before, and drawing each one where its solid is needed is what makes the modelling acts
    look like they are still doing sketch work. A sketch qualifies when it sits on an origin plane
    and its geometry is written out rather than read from run context; what stays behind is the
    sketches that CANNOT come early - one on a datum plane or a face that a later body defines, and
    one whose curves are projected off a solid.

    A component holding a hoisted sketch moves with it (a sketch needs its parent), and a
    'design_activate_component' takes its place in the narrative so everything after it still builds
    where it did."""
    hoisted, kept_acts, phase_active = [], [], None
    for name, pre, narr, fb in program:
        if name in after:
            kept_acts.append((name, pre, narr, fb))
            continue
        move, comps, owner_of = set(), {}, {}
        active = None
        for i, step in enumerate(narr):
            args = step[1] if isinstance(step[1], dict) else None
            if args and step[0] == "model_create_component" and args.get("activate"):
                active = args.get("name")
            elif args and step[0] == "design_activate_component" and args.get("occurrence"):
                occ = re.sub(r":\d+$", "", args["occurrence"])
                active = None if occ == "root" else occ
            if step[0] != "sketch_create" or not args or not args.get("name"):
                continue
            if args.get("plane") not in _ORIGIN_PLANES:
                continue
            sk = args["name"]
            # A sketch moves early unless a later step EDITS it. A dimension, a constraint or a
            # read works on the curves wherever they were drawn, and those come with it; trimming,
            # copying, moving, texting or projecting all depend on what the sketch holds AT THAT
            # MOMENT, and hoisting every curve up front changes that.
            if any(s[0] in _SKETCH_ORDER_BOUND and isinstance(s[1], dict)
                   and s[1].get("sketch_name") == sk for s in narr):
                continue
            owner_of[i] = active
            # A sketch that later RECEIVES a projection cannot come early: projecting a body's face
            # into a sketch that predates the body is a circular timeline dependency, and Fusion
            # refuses it by name (CIRCULAR_DEPENDENCY).
            if any(s[0] == "sketch_project" and isinstance(s[1], dict)
                   and s[1].get("sketch_name") == sk for s in narr):
                continue
            end = next((j for j in range(i + 1, len(narr))
                        if narr[j][0] in ("sketch_create", "model_create_component")), len(narr))
            draws = [k for k in range(i + 1, end)
                     if narr[k][0] in _SKETCH_MAKERS and isinstance(narr[k][1], dict)
                     and narr[k][1].get("sketch_name") == sk]
            if not draws:
                continue
            # A sketch that READS run context cannot come early. Callable args mean a handle saved by
            # an earlier step - and a handle is a face or an edge on a body, so the sketch is being
            # dimensioned or constrained against geometry a LATER feature defines. Hoisting it puts
            # the sketch before that body in the timeline and the reference points backwards, which
            # Fusion rejects at validation (measured: 'InternalValidationError : rSurface3D' on a
            # line_to_surface dimension whose sketch had been hoisted ahead of the shell it measures).
            if any(not isinstance(narr[k][1], dict) for k in range(i + 1, end)):
                continue
            move.add(i)
            move.update(draws)
            owner_name = owner_of[i]
            owner = next((j for j in range(i - 1, -1, -1)
                          if narr[j][0] == "model_create_component"
                          and isinstance(narr[j][1], dict)
                          and narr[j][1].get("name") == owner_name), None)
            if owner is not None:
                comps[owner] = owner_name

        kept = []
        for i, step in enumerate(narr):
            if i in comps:
                hoisted.append(step)
                if step[1].get("activate"):
                    phase_active = comps[i]
                    kept.append(("design_activate_component",
                                 {"occurrence": comps[i] + ":1"}, "ok", None))
            elif i in move:
                # The sketch phase runs the creates out of their original order, so whichever
                # component happens to be open is NOT the one this sketch was authored in. Put the
                # right one back first, or a root-level sketch lands inside an unrelated component
                # and is carried off to that component's slot.
                want = owner_of.get(i, phase_active)
                if i in owner_of and want != phase_active:
                    hoisted.append(("design_activate_component",
                                    {"occurrence": (want + ":1") if want else "root"}, "ok", None))
                    phase_active = want
                hoisted.append(step)
            else:
                kept.append(step)
        kept_acts.append((name, pre, kept, fb))

    if hoisted:
        hoisted.append(("design_activate_component", {"occurrence": "root"}, "ok", None))
    return hoisted, kept_acts

def _sketch_reading_order(phase, slots):
    """The sketch phase re-ordered so the camera reads the field once instead of commuting.

    The packer already deals cells left to right in the order the sketches are drawn, so the field
    IS in reading order - rows marching across and stepping down. What breaks the walk is that some
    sketches cannot be dealt a cell at all: one anchored to the world origin (a scale or a mirror is
    origin-relative, and an angular dimension's contract is stated against the sketch origin) stays
    in the origin band while the field sits a metre away. Interleaved with the placed ones, every
    such sketch costs a round trip out to the origin and back.

    So the pinned ones are drawn together, ahead of the field. Relative order is preserved inside
    each group, which is what keeps this safe to run AFTER the cells are dealt: the packer's order
    over the PLACED chunks is untouched, so 'slots' stays true.

    A block is one sketch: its create, the component create hoisted with it, and its drawing steps.
    Blocks move whole - a sketch separated from the component it belongs to lands in the wrong one.
    """
    # The owner is READ OFF the phase as built, never re-derived: a sketch whose component was
    # created outside this phase has no create to look at, and guessing 'root' for it drops the
    # sketch into the root component - where the geometry it feeds is then missing by name.
    blocks, pending, owner, owners = [], [], None, {}
    for step in phase:
        args = step[1] if isinstance(step[1], dict) else {}
        if step[0] == "design_activate_component":
            occ = str(args.get("occurrence") or "")
            owner = None if occ in ("", "root") else re.sub(r":\d+$", "", occ)
            continue                       # regenerated below from each block's recorded owner
        if step[0] == "model_create_component":
            pending.append(step)           # travels with the sketch it was hoisted for
            if args.get("activate") and args.get("name"):
                owner = args["name"]
            continue
        if step[0] == "sketch_create":
            blocks.append(pending + [step])
            owners[id(blocks[-1])] = owner
            pending = []
            continue
        if blocks:
            blocks[-1].append(step)

    def owner_of(block):
        return owners.get(id(block))

    def chunk_of(block):
        made = next((s for s in block if s[0] == "model_create_component"), None)
        if made:
            return made[1]["name"]
        held = owner_of(block)
        if held:
            return held
        create = next(s for s in block if s[0] == "sketch_create")
        return create[1].get("name")

    pinned = [b for b in blocks if chunk_of(b) not in slots]
    placed = [b for b in blocks if chunk_of(b) in slots]

    out, active = [], None
    for block in pinned + placed:
        want = owner_of(block)
        for step in block:
            if step[0] == "sketch_create" and want != active:
                out.append(("design_activate_component",
                            {"occurrence": (want + ":1") if want else "root"}, "ok", None))
                active = want
            out.append(step)
            if step[0] == "model_create_component" and step[1].get("activate"):
                active = step[1]["name"]
    if out:
        out.append(("design_activate_component", {"occurrence": "root"}, "ok", None))
    return out


# Tools that ACT on parts already built - no creation step marks the moment, so they are framed on
# their own operands or the assembly act plays out wherever the camera was last left.
_RELATION_TOOLS = ("joint_create", "joint_create_as_built", "joint_edit", "joint_drive",
                   "joint_motion_link", "assembly_ground", "assembly_move", "assembly_rigid_group",
                   "assembly_constrain", "assembly_capture_position")

# {chunk: [x0, x1, y0, y1]} in the PLACED world, and {entity name: its chunk} - both filled beside
# _SLOTS, once the acts are defined.
_PLACED_BOX = {}
_CHUNK_OF = {}
# {chunk: [x0, x1, y0, y1]} in the placed world, off a run's model_inspect reads. The authored box
# counts only the coordinates the steps carry, so it is a LOWER bound; a row here WIDENS a frame
# through _chunk_box and never narrows one.
_MEASURED_BOX = {
    "ArrP1": [200.0, 585.8, 1479.0, 1654.0],
    "ArrP2": [280.0, 592.3, 1444.0, 1629.0],
    "ArrP3": [400.0, 606.3, 1479.0, 1628.5],
    "AsbPin": [210.0, 230.0, 1789.0, 1809.0],
    "AsbPlate": [200.0, 240.0, 1779.0, 1819.0],
    "AxisPost": [1022.0, 1032.0, 1599.0, 1609.0],
    "BallPost": [1022.0, 1032.0, 1599.0, 1609.0],
    "BallSphere": [1021.0, 1033.0, 1598.0, 1610.0],
    "BayCameo": [502.0, 592.0, 1344.0, 1404.0],
    "BoreCameo": [522.5, 533.5, 2144.5, 2155.5],
    "Bracket": [-60.0, 60.0, -40.0, 40.0],
    "CombineCameo": [392.0, 442.0, 1344.0, 1394.0],
    "ConA": [854.0, 874.0, 1779.0, 1799.0],
    "ConB": [854.0, 874.0, 1779.0, 1799.0],
    "DatumBench": [580.0, 640.0, 1934.0, 1974.0],
    "DihedralL": [320.0, 380.0, 1934.0, 1974.0],
    "EmbossBlock": [600.0, 640.0, 100.0, 120.0],
    "FeatureCameo": [-240.0, 390.0, -50.0, 110.0],
    "FillDemo": [-6.0, 6.0, -6.0, 6.0],
    "GrpA": [792.0, 812.0, 1479.0, 1499.0],
    "GrpB": [872.0, 892.0, 1479.0, 1499.0],
    "HolderPart": [480.0, 520.0, 2042.0, 2062.0],
    "Hub": [1160.0, 1240.0, -40.0, 40.0],
    "IndBal": [493.0, 527.0, 1794.0, 1804.0],
    "IndCyl": [403.0, 437.0, 1794.0, 1804.0],
    "IndPin": [448.0, 482.0, 1794.0, 1804.0],
    "IndPla": [538.0, 572.0, 1794.0, 1804.0],
    "IndRev": [313.0, 347.0, 1794.0, 1804.0],
    "IndRig": [583.0, 617.0, 1794.0, 1804.0],
    "IndSld": [358.0, 392.0, 1794.0, 1804.0],
    "JawFixed": [-70.0, 70.0, 43.0, 78.0],
    "JawMoving": [-70.0, 70.0, -78.0, -43.0],
    "JointBase": [300.0, 630.0, 1779.0, 1819.0],
    "LeadScrew": [-30.0, 30.0, -125.0, -80.0],
    "LnkA": [694.0, 714.0, 1779.0, 1799.0],
    "LnkB": [694.0, 714.0, 1779.0, 1799.0],
    "LoftCameo": [300.0, 332.0, 1344.0, 1376.0],
    "MateArm": [1.7, 36.2, -2067.6, -2042.0],
    "MateSeat": [934.0, 954.0, 1779.0, 1799.0],
    "Msh": [651.8, 727.0, 1343.7, 1419.0],
    "PatchRail": [980.0, 1000.0, 1934.0, 1934.0],
    "PinCameo": [523.0, 1022.0, 1419.0, 2155.0],
    "PipeCut": [800.0, 840.0, -20.0, 20.0],
    "PipeHalf": [757.0, 763.0, -3.0, 3.0],
    "PipeRun": [715.0, 725.0, -5.0, 5.0],
    "PoseCameo": [814.0, 834.0, 1779.0, 1799.0],
    "ReplBlock": [660.0, 690.0, 0.0, 30.0],
    "ReplRoof": [655.0, 695.0, -80.0, 80.0],
    "RevolveCameo": [22.0, 38.0, -8.0, 8.0],
    "RigidPost": [1022.0, 1032.0, 1599.0, 1609.0],
    "RmScratch": [980.0, 1000.0, 2042.0, 2062.0],
    "Ruled": [1060.0, 1100.0, 1919.0, 1934.0],
    "RuledSolid": [200.0, 240.0, 2042.0, 2082.0],
    "SAlign": [799.0, 840.0, 1934.0, 1964.0],
    "SDel": [899.6, 1000.4, 1933.9, 1954.1],
    "SHole": [0.0, 640.0, 0.0, 10.0],
    "SRev": [-10.0, 10.0, -10.0, 10.0],
    "STOCK": [-63.0, 63.0, -43.0, 43.0],
    "ScaleBlock": [1244.1, 1346.5, 322.0, 403.2],
    "ShellCap": [580.0, 610.0, 2042.0, 2072.0],
    "Spl": [300.0, 340.0, 2042.0, 2082.0],
    "Stc": [400.0, 420.0, 2042.0, 2062.0],
    "Surf": [697.0, 740.0, 1934.0, 1964.0],
    "SwarfFrustum": [200.0, 260.0, 1942.0, 1982.0],
    "SweepCameo": [245.0, 255.0, -5.0, 5.0],
    "TangentLoop": [318.5, 381.5, 2140.5, 2203.5],
    "TangentRun": [194.0, 260.0, 2140.5, 2202.0],
    "ThreadBore": [787.0, 811.0, 1344.0, 1368.0],
    "ThreadPost": [830.1, 859.9, 2032.1, 2061.9],
    "ThreadPost2": [910.1, 919.9, 2042.1, 2051.9],
    "TorusPost": [596.0, 606.0, 2142.0, 2152.0],
    "TorusRing": [555.0, 647.0, 2101.0, 2193.0],
    "TwicePlaced": [760.0, 820.0, 2042.0, 2062.0],
    "TwoSideCap": [670.0, 700.0, 2042.0, 2072.0],
    "ViseBase": [-95.0, 95.0, -95.0, 95.0],
}
# The chunk names that are COMPONENTS - a body in one is framed as an occurrence ('Name:1'), a body
# at the root as the sketch that drew it.
_COMPONENTS = set()
# Chunks a pattern acts on. Their bodies reach well outside the sketch that drew them, and the frame
# is sized from that sketch - measured, a 70 mm pad carrying a 3x2 grid at 60 mm framed at 154 mm and
# cropped. The step's own spacings cannot be read (its arguments resolve a body from run context and
# are a callable), so the frame widens by a factor rather than by a measurement.
_PATTERNED = set()
_FRAME_PATTERN_WIDEN = 3.0


def _framed(steps):
    """Insert the camera rows an act plays in, and settle the ones the act module already wrote.

    SKIP - a subject already inside the standing frame gets no row, so the camera stops bouncing.
    GROW - a row's focus reaches out through the nearest built neighbours to the target span.
    READ - a hand row becomes the standing frame, and a flat one is shot down its own plane normal.

    A component or sketch that never gets geometry before the next entity starts gets no row at
    all - there is nothing to frame yet.
    """
    ready = {}                       # group -> (last step index it is ready at, [member names])
    hand_framed = set()
    for k, step in enumerate(steps):
        a = steps[k][1] if isinstance(steps[k][1], dict) else {}
        if steps[k][0] == "view_set" and a.get("focus"):
            f = a["focus"]
            for nm in (f if isinstance(f, list) else [f]):
                hand_framed.add(_group_of(str(nm)))

    def note(name, at, member):
        g = _group_of(name)
        if g in hand_framed:
            return
        cur = ready.get(g)
        members = (cur[1] if cur else [])
        if member not in members:
            members = members + [member]
        ready[g] = (max(at, cur[0]) if cur else at, members)

    for i, step in enumerate(steps):
        args = step[1] if isinstance(step[1], dict) else {}
        if step[0] == "sketch_create" and args.get("name"):
            name = args["name"]
            end = next((j for j in range(i + 1, len(steps))
                        if steps[j][0] in ("sketch_create", "model_create_component")), len(steps))
            drawn = next((k for k in range(i + 1, end) if steps[k][0] in _SKETCH_MAKERS
                          and isinstance(steps[k][1], dict)
                          and steps[k][1].get("sketch_name", name) == name), None)
            # A profile drawn inside a component only exists to be extruded a step later: the body
            # frame covers it, and framing the rectangle first is what makes a modelling act look
            # like it is doing sketch work. A sketch at the ROOT is the sketch tools' own subject
            # and keeps its frame - and so does one whose body is built in a LATER act, or the
            # sketch phase would draw everything off camera with nothing following to frame it.
            covered = any(s[0] in _BODY_MAKERS and isinstance(s[1], dict)
                          and s[1].get("sketch_name") == name for s in steps)
            fixture = covered and _CHUNK_OF.get(name, name) != name
            if drawn is not None and not fixture:
                note(name, drawn, name)
        elif step[0] == "model_create_component" and args.get("activate") and args.get("name"):
            comp = args["name"]
            # the chunk runs to the NEXT create: a later part's body is not this part's body, and
            # framing this occurrence on it would aim the camera at the wrong slot.
            end = next((j for j in range(i + 1, len(steps))
                        if steps[j][0] == "model_create_component"), len(steps))
            body = next((k for k in range(i + 1, end) if steps[k][0] in _BODY_MAKERS), None)
            if body is not None:
                note(comp, body, comp + ":1")
        elif step[0] in _BODY_MAKERS and args.get("sketch_name") in _CHUNK_OF:
            # The moment a solid appears, whether or not this act is where its component and sketch
            # were made. Once the sketch phase hoists those away, a creation-only trigger leaves a
            # whole act - the finale among them - with no camera row at all, playing out at
            # whatever zoom the previous act left behind.
            owner = _CHUNK_OF.get(args["sketch_name"], args["sketch_name"])
            note(owner, i, owner + ":1" if owner in _COMPONENTS else owner)

    inserts = {}
    for _g, (at, members) in ready.items():
        inserts.setdefault(at, []).extend(members)

    # A joint, a ground, a rigid group or a drive ACTS on parts that already exist, so no creation
    # step marks the moment - and the parts it mates sit in whichever slots they were built in.
    # Without a row of their own the whole assembly act plays out wherever the camera happened to be
    # left. These frame on their own operands, at the step that does the work.
    relation_at = set()
    for i, step in enumerate(steps):
        if step[0] not in _RELATION_TOOLS or not isinstance(step[1], dict):
            continue
        operands = []
        for key in ("occurrence", "occurrences", "occurrence_one", "occurrence_two"):
            v = step[1].get(key)
            for nm in (v if isinstance(v, list) else [v]):
                # an occurrence may be named through a sub-entity ('Bracket:1:origin') - the frame
                # wants the occurrence itself.
                if isinstance(nm, str) and nm and nm != "origin":
                    m = re.match(r"^([^:]+:\d+)", nm)
                    if m and m.group(1) not in operands:
                        operands.append(m.group(1))
        if operands:
            at = i - 1 if i else 0
            inserts.setdefault(at, []).extend(operands)
            relation_at.add(at)

    # A neighbour is only worth framing while it still answers to the name it was built under: the
    # story renames and deletes as it goes, and view_set refuses a focus it cannot resolve.
    retired = {}
    for k, step in enumerate(steps):
        a = step[1] if isinstance(step[1], dict) else {}
        if step[0] in ("design_set_name", "design_delete_occurrence", "design_move_occurrence",
                       "design_delete_feature", "mesh_delete", "cam_delete"):
            for key in ("target", "occurrence", "name"):
                if isinstance(a.get(key), str):
                    retired.setdefault(re.sub(r":\d+$", "", a[key]), k)

    out, frame, built = [], None, []
    for i, step in enumerate(steps):
        out.append(step)
        if step[0] == "view_set" and isinstance(step[1], dict) and step[1].get("focus"):
            f = step[1]["focus"]
            # An act module writes its camera rows as it imports, before any sketch plane is known,
            # so a flat subject falls back to iso and renders edge-on. Rewriting the row here - and
            # only a row _watch itself wrote - shoots that sketch down its own normal.
            fresh = _watch(f)
            if step[2:] == fresh[2:] and step[1] == dict(fresh[1], orientation="iso-top-right"):
                out[-1] = fresh
            # A hand-authored row that FITS on its focus IS the standing frame from here on.
            # Tracking only the rows this pass inserts leaves the frame where the camera no longer
            # is, and the next subject then tests as already on screen - the act plays off camera.
            if step[1].get("action") == "orient" and step[1].get("fit") is not False:
                held = _frame_box(f if isinstance(f, list) else [f])
                frame = _expand(held, _FRAME_MARGIN) if held else None
        members = inserts.get(i)
        if not members:
            continue
        built = [b for b in built
                 if all(retired.get(re.sub(r":\d+$", "", str(n)), len(steps)) > i for n in b[0])]
        # A relation's operands are NOT trimmed: a joint between two parts is only legible with BOTH
        # of them in shot, so the frame widens to hold them however far apart they were built.
        # A SKETCH has almost no visual area, so a frame spanning two of them is sized by the gap
        # between them rather than by either one - graded by eye, sketch-only frames naming two or
        # more scored 1 good in 27. One at a time; a solid is big enough to share a shot.
        solid = any(str(n).endswith(":1") for n in members)
        if i not in relation_at:
            keep = _FRAME_MAX_SUBJECTS if solid else _FRAME_SKETCH_GROUP
            members = _frame_cluster(members, None if solid else _FRAME_SKETCH_SPAN)[-keep:]
        subject = _frame_box(members)
        built.append((members, subject))
        if subject and frame and _inside(subject, frame):
            continue                     # already on screen - moving would only jog the view
        # How far the frame reaches is set by the SUBJECT's own size, never by a constant: a 20 mm
        # sketch inside a fixed 220 mm neighbourhood renders in a 440 mm view, which is 4% of the
        # frame - graded by eye, every one of those read as a speck. A joint is the exception that
        # wants space around it, because a mate is watched rather than inspected.
        # from the LARGEST single subject, never the union: a union of two far-apart parts is a
        # measure of their SEPARATION, and zooming to 2.2x that is the whole-field photograph again.
        each = [_frame_box([m]) for m in members]
        own = max((max(b[1] - b[0], b[3] - b[2]) for b in each if b), default=0.0)
        pattern_names = [str(_CHUNK_OF.get(str(m), str(m))) for m in members]
        if any(name in _PATTERNED
               or (name.endswith(":1") and name[:-2] in _PATTERNED)
               for name in pattern_names):
            own *= _FRAME_PATTERN_WIDEN
        span = max(own * _FRAME_CONTEXT, _FRAME_MIN_SPAN)
        if i in relation_at:
            span *= _FRAME_RELATION_WIDEN
        # A SKETCH frame reaches for its neighbours on purpose: sketch commands are quick, and a
        # camera move for each one is more motion than the work is worth. The span cap is what keeps
        # it honest - a handful of sketches from the SAME row share a frame, scattered ones do not,
        # and the group is what the earlier one-at-a-time rule was over-correcting for.
        if not solid and subject is not None:
            span, cap = _FRAME_SKETCH_SPAN, _FRAME_SKETCH_GROUP
        else:
            cap = _FRAME_MAX_SUBJECTS
        focus, box = _frame_neighbourhood(members, subject, built, span, cap)
        frame = _expand(box, _FRAME_MARGIN) if box else None
        out.append(_watch(focus))
    return out


def _union(a, b):
    """The box holding both, or whichever one is there."""
    if a is None or b is None:
        return a or b
    return [min(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), max(a[3], b[3])]


def _chunk_box(chunk):
    """The world box one chunk occupies: its authored box WIDENED by the measured extents where a
    run recorded them. A union, never a replacement - a measurement of one body does not bound a
    chunk whose other bodies were never read."""
    return _union(_PLACED_BOX.get(chunk), _MEASURED_BOX.get(chunk))


def measured_boxes(inspected):
    """{chunk: [x0, x1, y0, y1]} from {entity name: its model_inspect payload} - the _MEASURED_BOX
    rows a run's own reads produce, so the next layout move is made from measurement rather than
    from the coordinates the steps were written with. An entity whose min/max corner did not read
    is left out; a corner that reads null would record a box the geometry does not have."""
    out = {}
    for name, payload in (inspected or {}).items():
        lo = (payload or {}).get("min_point") or {}
        hi = (payload or {}).get("max_point") or {}
        corners = (lo.get("x"), lo.get("y"), hi.get("x"), hi.get("y"))
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in corners):
            continue
        stem = re.sub(r":\d+$", "", str(name))
        chunk = _CHUNK_OF.get(stem, stem)
        out[chunk] = _union(out.get(chunk), [lo["x"], hi["x"], lo["y"], hi["y"]])
    return out


# The drift gate ACT 9 runs, one row per chunk. These four travel furthest from their authored
# cell - a joint carries one clear of the packed field, a pattern and a mirror overrun two more -
# so a re-pack that moves anything moves one of them. 5 mm sits two orders above the read-to-read
# agreement measured (0.05 mm) and an order below the smallest move that matters (_FIELD_GUTTER).
_DRIFT_CHUNKS = ("MateArm", "TorusRing", "BallSphere", "FeatureCameo")
_DRIFT_TOL_MM = 5.0


def layout_drift_mm(chunk, inspected):
    """The largest mm any edge of `chunk` has moved from its _MEASURED_BOX row, or None where the
    read carried no usable corner - which is no agreement, never agreement."""
    got = measured_boxes(inspected).get(chunk)
    want = _MEASURED_BOX.get(chunk)
    if got is None or want is None:
        return None
    return max(abs(got[i] - want[i]) for i in range(4))


def layout_placed_as_measured(chunk, min_point, max_point):
    """True where `chunk` still sits where _MEASURED_BOX records it, within _DRIFT_TOL_MM."""
    drift = layout_drift_mm(chunk, {chunk: {"min_point": min_point, "max_point": max_point}})
    return drift is not None and drift <= _DRIFT_TOL_MM


def drift_row(chunk):
    """ACT 9's receipt row for ONE chunk - the four share this predicate, so the gate widens by a
    name in _DRIFT_CHUNKS rather than by another copy of the check."""
    return ("model_inspect", {"target": chunk + ":1", "units": "mm"},
            lambda p: layout_placed_as_measured(chunk, p["min_point"], p["max_point"]), None)


def _frame_box(names):
    """The world box the named entities occupy, or None when none of them is placed. A name is
    resolved through _CHUNK_OF first: a sketch rides on the body that owns it, and it is that body's
    box the camera will see."""
    stems = [re.sub(r":\d+$", "", str(n)) for n in names]
    got = [b for b in (_chunk_box(_CHUNK_OF.get(s, s)) for s in stems) if b]
    if not got:
        return None
    return [min(b[0] for b in got), max(b[1] for b in got),
            min(b[2] for b in got), max(b[3] for b in got)]


def _frame_cluster(members, limit=None):
    """The members that actually fit in one shot together, keeping the ones nearest the LAST one
    built - the work just done. A family is framed as a family, but a family whose members ended up
    in different rows of the field (SlotA..SlotH, or a profile pinned at the origin beside the post
    it revolved) does not fit in any one frame, and stretching to cover both leaves every member a
    speck."""
    if len(members) < 2 or _frame_box(members) is None:
        return members
    anchor = _frame_box([members[-1]]) or _frame_box(members)
    # what the group may grow to is set by the anchor's OWN size, so a family of small sketches
    # stays tight and a family of large parts is allowed the room it needs
    own = max(anchor[1] - anchor[0], anchor[3] - anchor[2])
    limit = limit or max(own * _FRAME_CONTEXT, _FRAME_MIN_SPAN) * _FRAME_STRETCH
    kept, box = [], list(anchor)
    for name in members:
        b = _frame_box([name])
        if b is None:
            # Where this one sits is unknown, so it cannot be shown to fit - and keeping it anyway
            # is what let a member at the far end of the field back into a trimmed frame.
            continue
        grown = [min(box[0], b[0]), max(box[1], b[1]), min(box[2], b[2]), max(box[3], b[3])]
        if max(grown[1] - grown[0], grown[3] - grown[2]) > limit:
            continue
        kept.append(name)
        box = grown
    return kept or members


def _expand(box, factor):
    cx, cy = (box[0] + box[1]) / 2.0, (box[2] + box[3]) / 2.0
    hw, hh = (box[1] - box[0]) * factor / 2.0, (box[3] - box[2]) * factor / 2.0
    return [cx - hw, cx + hw, cy - hh, cy + hh]


def _inside(box, outer):
    return (outer[0] <= box[0] and box[1] <= outer[1]
            and outer[2] <= box[2] and box[3] <= outer[3])


def _frame_neighbourhood(members, subject, built, target=None, cap=None):
    """The focus list to hand view_set: the subject, widened through the nearest already-built
    neighbours until it spans 'target' (_FRAME_MIN_SPAN by default). Only things already
    built can be framed - a
    sketch that does not exist yet cannot be resolved - so the frame trails backwards, which reads
    as the new work arriving beside what it followed."""
    target, cap = target or _FRAME_MIN_SPAN, cap or _FRAME_MAX_SUBJECTS
    known = [(names, b) for names, b in built if b is not None]
    if subject is None:
        # Nothing is known about where this subject is - a sketch with no coordinates of its own, an
        # SVG import. Framing it alone lets the camera fit whatever degenerate extent it has and dive
        # into the origin construction geometry, so hand it the last few neighbours for scale.
        focus, box = list(members), None
        for names, other in reversed(known[-_FRAME_FALLBACK_NEIGHBOURS:]):
            grown = list(other) if box is None else [
                min(box[0], other[0]), max(box[1], other[1]),
                min(box[2], other[2]), max(box[3], other[3])]
            # the ceiling applies here too - lending a subject three neighbours spread over a metre
            # buys it scale by making everything in shot a speck.
            if max(grown[1] - grown[0], grown[3] - grown[2]) > target * _FRAME_STRETCH:
                continue
            additions = [n for n in names if n not in focus]
            if len(focus) + len(additions) > cap:
                continue
            focus += additions
            box = grown
        return focus, box

    focus, box = list(members), list(subject)

    def span(b):
        return max(b[1] - b[0], b[3] - b[2])

    def reach(b):
        return max(abs((b[0] + b[1]) / 2.0 - (subject[0] + subject[1]) / 2.0),
                   abs((b[2] + b[3]) / 2.0 - (subject[2] + subject[3]) / 2.0))

    for names, other in sorted(known, key=lambda nb: reach(nb[1])):
        if span(box) >= target or len(focus) >= cap:
            break
        if all(n in focus for n in names):
            continue
        grown = [min(box[0], other[0]), max(box[1], other[1]),
                 min(box[2], other[2]), max(box[3], other[3])]
        # A neighbour is only context while it stays in shot WITH the subject. Testing the union
        # AFTER adding is what let one distant part drag the frame out to the whole 1500 mm field,
        # where every named subject reads as a speck.
        if span(grown) > target * _FRAME_STRETCH:
            continue
        additions = [n for n in names if n not in focus]
        if len(focus) + len(additions) > cap:
            continue
        focus += additions
        box = grown
    return focus, box


# --- physical layout: one slot per scratch chunk, laid out in narrative order --------------------
# A chunk's coordinates as written are LOCAL to that chunk: two chunks may be authored on the same
# patch of the XY plane, and this pass translates each one into a cell of its own, in the order the
# acts build them. So a camera framed on a chunk contains its subject, and the neighbours in shot
# are the steps that ran just before and just after it.
#
# Rigid translation is what makes it safe: a chunk's internal offsets, sizes and probe points all
# move with it, so nothing inside a chunk can be broken by the move. What could break is a world
# point one chunk aims at another - so the chunk is the COMPONENT, which is the unit those points
# are shared within, and the part/billet/vise world at the origin never moves at all. A world
# point that DOES cross a component boundary fails the layout gate in test_tool_verify_complete.py
# rather than drifting quietly: put the geometry in one component, or pin it.

# Key PAIRS that pin a step to a PLACE. A key holding a delta (dx, dy, distance, spacing) or a size
# (radius, slot_length, depth) is deliberately absent - a rigid translation leaves those alone. Both
# halves of a pair must be present: a lone 'x' is a cross-mode input a refusal step passes to be
# rejected, not a place, and reading it as one would drag its whole chunk across the world.
_PLACE_PAIRS = (("x", "y"), ("x1", "y1"), ("x2", "y2"), ("x3", "y3"), ("cx", "cy"),
                ("center_x", "center_y"))
# Tools whose x/y is a TRANSFORM relative to the entity's own origin, not a place in the world.
# design_add_instance sets an occurrence's translation, and the component's geometry already carries
# wherever the layout put it - so shifting the transform too applies the offset TWICE and throws the
# instance a whole field away from the original it is meant to sit beside.
_PLACE_DELTA_TOOLS = ("design_add_instance",)

_PLACE_XYZ = ("nearest_to", "point")        # one [x, y, z]
_PLACE_POINTS = ("points",)                 # a list of [x, y] or [x, y, z]

# Keys holding a LIST of entry objects. A sketch write tool takes one per call and its entries carry
# the authored coordinates the call draws at, so every position the layout reads and shifts lives one
# level down rather than beside 'sketch_name'.
_PLACE_ENTRY_LISTS = ("geometry",)

# Keys naming the entity a step addresses, most specific first. A step that names one belongs to
# that entity's chunk wherever it sits in the narrative; only a step naming none inherits the chunk
# being built around it. 'name' is absent on purpose - on a creating step it names the thing being
# made, which belongs to the chunk around it (a datum plane that split off on its own name would be
# left behind by the body it cuts).
_PLACE_NAMES = ("sketch_name", "target", "occurrence", "component", "body", "bodies")

# A sketch's coordinates are in ITS plane's frame, not the world's: on XZ a 'cy' is a world Z, on YZ
# a 'cx' is a world Y. Each entry maps the sketch's (u, v) onto the world axes a translation moves,
# with None for the axis the plane does not span. Reading an XZ sketch as if it were XY moves a
# revolve profile off its axis, which turns a sphere into a torus and passes every count check.
_PLACE_FRAMES = {"xy": ("x", "y"), "xz": ("x", None), "yz": ("y", None)}

# Chunks the layout cannot see are one rigid body. Both are components in their own right - which is
# what lets one find_geometry name each without ambiguity - but they are stacked on purpose, and the
# call that joins them reaches for both through run-time handles no static read can follow. Sharing
# ground is NOT the test: half the scratch field is authored on the same patch of XY by chance.
_PLACE_WITH = {
    "ReplBlock": "ReplRoof",     # the open sheet that replaces the block's top face sits above it
}

# Chunks a JOINT or an assembly CONSTRAINT co-locates. Both move a part onto the other's geometry,
# so the
# cell the layout dealt the mover is abandoned the instant the joint lands, and the mover arrives in
# the PARTNER's cell - on top of whatever the packer had already put there. Measured on the finished
# document: the ball/axis/rigid post chain piled five bodies into one cell and spilled into the next,
# which is what a viewer sees as cameos sitting inside older ones.
# Each group is dealt ONE cell and every member takes the SAME offset, so the group travels as the
# rigid assembly it is about to become and keeps its authored relative positions. The joint then
# moves parts WITHIN that cell, which is the only place it was ever going to move them.
_JOINT_GROUPS = (
    ("BallSphere", "BallPost", "AxisPost", "RigidPost"),
    ("TorusRing", "TorusPost"),
    ("AsbPin", "AsbPlate"),
    ("ConA", "ConB"),            # the single-relationship constrain pair
    ("MateSeat", "MateArm"),     # the multi-relationship one - a seat AND a turn in one feature
    ("LnkA", "LnkB"),            # the motion link's fresh revolute pair
    # the joint bench: the base and every indicator arm its stations carry into place
    ("JointBase",) + tuple("Ind" + s[0] for s in _JOINT_STATIONS),
)
_JOINT_FAMILY = {c: g[0] for g in _JOINT_GROUPS for c in g}

# Chunks whose READ-BACK is relative to the world origin, so moving them changes the answer. The
# angular-dimension contract is "the wedge FACING THE SKETCH ORIGIN", which flips to the supplement
# once the crossing point moves to the other side of it - a 60 degree beat silently becomes 120.
_PLACE_ANCHORED = ("W3Dims",)

# Tools whose RESULT is measured from the world origin, so the further out the chunk sits the
# further its result is thrown: a mirror about an origin plane reflects to the far side (joined,
# that is ONE body spanning both), and a scale multiplies the distance along with the size. Measured
# on a placed field: EmbossBlock mirrored+joined at y 720 became a single body 1480 mm long, and
# ScaleBlock scaled at y 720 ended up at y 1594. A chunk either of these acts on stays put.
_ORIGIN_RELATIVE_TOOLS = ("model_mirror", "model_scale")

_PIN_HALF = 160.0        # half-width of the origin neighbourhood, which never moves
_FIELD_X0 = 200.0        # the scratch field starts clear of it
# ...and starts ABOVE the band the anchored chunks occupy (every one of them sits at y <= 140), so
# the packer never has to step around an obstacle. Stepping around one is what made the sequence
# jump: a row would skip a gap, and the eye loses the order the acts were built in.
_FIELD_Y0 = 220.0
_FIELD_WIDTH = 900.0     # a row wraps past here - narrow keeps the field a BLOCK, not a long strip
# Empty millimetres between one chunk's cell and the next. This is ALSO the packer's only margin for
# error, and it needs one: a cell is measured from the coordinates the steps are WRITTEN with, which
# under-counts the body that grows from them - a circle contributes its centre, not its radius, and
# an extrude contributes nothing at all in the third axis. So the real geometry routinely reaches
# past its cell by a radius or two, and at 18 mm that reach landed in the neighbour. Measured on the
# finished document: widening this from 18 to 60 is what separates the cameos that were touching.
# The cost is a taller field, which nothing pays for - every chunk is framed on itself.
_FIELD_GUTTER = 60.0


def _place_owner(args):
    """The entity a step addresses, or None when it names none. An occurrence path is reduced to
    its component - instance :1 and :2 of a component are the same chunk of the world."""
    for key in _PLACE_NAMES:
        v = args.get(key)
        if isinstance(v, (list, tuple)) and v and isinstance(v[0], str):
            v = v[0]
        if isinstance(v, str) and v:
            return re.sub(r":\d+$", "", v.split("+")[0].strip())
    return None


def _place_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _place_dicts(args):
    """The dicts a step pins positions in: its own arguments, then each entry of an entry list."""
    yield args
    for key in _PLACE_ENTRY_LISTS:
        for entry in (args.get(key) if isinstance(args.get(key), (list, tuple)) else ()):
            if isinstance(entry, dict):
                yield entry


def _place_points(args, frame="xy", tool=""):
    """Every world position the step's arguments pin, as (x, y) with None on an axis the argument
    does not constrain. Pair keys are read in 'frame' - the plane the sketch was drawn on - so an
    XZ sketch pins only X. frame None is a plane derived from geometry that travels with its chunk,
    which anchors nothing."""
    out = []
    for one in _place_dicts(args):
        out.extend(_place_points_in(one, frame, tool))
    return out


def _place_points_in(args, frame="xy", tool=""):
    """The positions ONE argument dict pins - see _place_points."""
    num, out = _place_num, []
    axes = _PLACE_FRAMES.get(frame or "", (None, None))

    def world(u, v):
        """(x, y) with None on the axis this frame does not span; None when it spans neither."""
        pinned = dict(zip(axes, (u, v)))
        pinned.pop(None, None)
        return (pinned.get("x"), pinned.get("y")) if pinned else None

    pairs = tuple(p for p in _PLACE_PAIRS
                  if not (p == ("x", "y") and tool in _PLACE_DELTA_TOOLS))
    for kx, ky in pairs:
        if not (num(args.get(kx)) and num(args.get(ky))):
            continue
        # a circle or polygon is authored as a CENTRE plus a radius, so the centre alone measures it
        # as a point and the chunk's cell comes out far too small to hold it
        r = args.get("radius") if (kx, ky) == ("cx", "cy") else None
        r = r if num(r) else 0
        for dx, dy in (((0, 0),) if not r else ((-r, -r), (r, r))):
            at = world(args[kx] + dx, args[ky] + dy)
            if at is not None:
                out.append(at)
    if args.get("kind") == "plane" and num(args.get("offset")):
        # an origin plane's offset is a world position along its NORMAL: an XZ plane sits at that Y,
        # so a body it splits moves out from under it unless the offset moves too.
        normal = {"xz": "y", "yz": "x"}.get(args.get("plane"))
        if normal == "x":
            out.append((args["offset"], None))
        elif normal == "y":
            out.append((None, args["offset"]))
    # A coordinate list is read in the SAME frame as the pair keys: a polyline drawn on an XZ sketch
    # pins world X and a depth the layout does not span, so reading it as (x, y) records the depth
    # as a world Y and carries the chunk along an axis it does not live on.
    for key in _PLACE_XYZ:
        v = args.get(key)
        if isinstance(v, (list, tuple)) and len(v) >= 2 and num(v[0]) and num(v[1]):
            at = world(v[0], v[1])
            if at is not None:
                out.append(at)
    for key in _PLACE_POINTS:
        v = args.get(key)
        if isinstance(v, (list, tuple)):
            for p in v:
                if isinstance(p, (list, tuple)) and len(p) >= 2 and num(p[0]) and num(p[1]):
                    at = world(p[0], p[1])
                    if at is not None:
                        out.append(at)
    return out


def _place_walk(steps, home_out=None):
    """Yield (step, chunk, cursor, frame) for every step. chunk is the rigid body the step
    addresses: the ACTIVE COMPONENT when one is open, otherwise the root-level sketch. A component
    is the natural unit - CC1 and CC2 are two sketches inside CombineCameo whose solids must keep
    overlapping, and they do because the component moves as one - while root sketches each get their
    own cell, so SlotA..SlotH lie side by side and the framing pass still frames all eight together.

    chunk is None for a step that pins nothing to the world. cursor is the body being built around
    the step, which couples a probe to the pad it reads back. frame is the plane the step's pair
    keys are read in. home_out, when given, collects {entity name: its chunk} - the map that turns a
    sketch name back into the body it rides on."""
    active, plane, cursor = None, {}, None
    home = home_out if home_out is not None else {}
    for step in steps:
        args = step[1]
        frame = "xy"
        if isinstance(args, dict):
            if step[0] == "model_create_component" and args.get("name"):
                if args.get("activate"):
                    active = args["name"]
                home[args["name"]] = args["name"]
            elif step[0] == "design_activate_component" and args.get("occurrence"):
                occ = re.sub(r":\d+$", "", args["occurrence"])
                active = None if occ == "root" else occ
            elif step[0] == "model_construction" and args.get("kind") == "plane" and args.get("name"):
                # a datum offset from an origin plane keeps that plane's frame; one built on a path
                # or a face gets its frame from geometry, and nothing drawn on it is world-anchored.
                plane[args["name"]] = args.get("plane") if args.get("plane") in _PLACE_FRAMES else None
            elif step[0] == "sketch_create" and args.get("name"):
                home[args["name"]] = active or args["name"]
                p = args.get("plane")
                plane[args["name"]] = p if p in _PLACE_FRAMES else plane.get(p)
            if step[0] == "design_activate_component":
                # back at the root nothing is being built around the steps that follow, so the
                # cursor goes with it - leaving it set attributes the next loose coordinate to
                # whatever component happened to be open last.
                cursor = active
            elif active:
                cursor = active
            elif args.get("sketch_name") in home:
                cursor = home[args["sketch_name"]]
            elif step[0] == "sketch_create" and args.get("name"):
                cursor = home[args["name"]]
            if step[0].startswith("sketch_"):
                sk = args.get("sketch_name") or (args.get("name") if step[0] == "sketch_create" else None)
                frame = plane.get(sk, "xy") if sk else "xy"
            if _place_points(args, frame, step[0]):
                own = _place_owner(args)
                yield step, (home.get(own, own) if own else cursor), cursor, frame
                continue
        # a callable builds its arguments from run context and cannot be read here, so it belongs
        # to the body being built around it - which is where any world point it bakes in came from.
        yield step, (cursor if callable(args) else None), cursor, frame


def _place_shift(args, dx, dy, frame="xy", tool=""):
    """args translated by (dx, dy): every pinned position moves, everything else is untouched. Pair
    keys AND coordinate lists move by the world delta resolved into 'frame's axes, so an XZ sketch
    shifts only the coordinate that is a world X and leaves the one that is a world Z alone."""
    num = _place_num
    delta = {"x": dx, "y": dy, None: 0.0}
    du, dv = (delta[a] for a in _PLACE_FRAMES.get(frame or "", (None, None)))

    def shift_seq(v):
        if not isinstance(v, (list, tuple)) or len(v) < 2 or not (num(v[0]) and num(v[1])):
            return v
        return [v[0] + du, v[1] + dv] + list(v[2:])

    if callable(args):
        return lambda ctx, _f=args: _place_shift(_f(ctx), dx, dy, frame, tool)

    def shift_one(src):
        out = dict(src)
        for kx, ky in _PLACE_PAIRS:
            if (kx, ky) == ("x", "y") and tool in _PLACE_DELTA_TOOLS:
                continue
            if num(out.get(kx)) and num(out.get(ky)):
                out[kx], out[ky] = out[kx] + du, out[ky] + dv
        if out.get("kind") == "plane" and num(out.get("offset")):
            out["offset"] += {"xz": dy, "yz": dx}.get(out.get("plane"), 0.0)
        for key in _PLACE_XYZ:
            if key in out:
                out[key] = shift_seq(out[key])
        for key in _PLACE_POINTS:
            v = out.get(key)
            if isinstance(v, (list, tuple)):
                out[key] = [shift_seq(p) for p in v]
        return out

    out = shift_one(args)
    for key in _PLACE_ENTRY_LISTS:
        v = out.get(key)
        if isinstance(v, (list, tuple)):
            out[key] = [shift_one(e) if isinstance(e, dict) else e for e in v]
    return out


def _place_slots(program):
    """Measure every chunk, then deal each movable one a cell of its own; return {chunk: (dx, dy)}.

    Cells are dealt left to right in the order the acts build them, wrapping into a new row past
    _FIELD_WIDTH, so the camera walks the field in the order the story runs.

    Anything reaching into the origin neighbourhood stays exactly where it is: that is the machined
    part, the billet and the vise built around it, and the fallback world, which are addressed by
    scripts and by each other."""
    box, order, locked = {}, [], set()
    for _name, _pre, narr, _fb in program:
        for step, chunk, _cursor, frame in _place_walk(narr):
            args = step[1]
            if chunk is None or not isinstance(args, dict):
                continue
            pinned = _place_points(args, frame, step[0])
            # A sketch on a GLOBAL origin plane is nailed to that plane: an XZ sketch is at y=0, so
            # a chunk holding one stays where it was authored rather than being carried in y.
            # Any pinned position locks it, a coordinate list as much as a pair key.
            if frame in ("xz", "yz") and pinned:
                locked.add(chunk)
            for x, y in pinned:
                if chunk not in box:
                    box[chunk] = [None, None, None, None]
                    order.append(chunk)
                b = box[chunk]
                if x is not None:
                    b[0] = x if b[0] is None else min(b[0], x)
                    b[1] = x if b[1] is None else max(b[1], x)
                if y is not None:
                    b[2] = y if b[2] is None else min(b[2], y)
                    b[3] = y if b[3] is None else max(b[3], y)
    # a chunk drawn only on an edge-on plane constrains one axis; the other reads as zero extent at
    # the origin, which is where that plane sits.
    for b in box.values():
        for i in (0, 1, 2, 3):
            if b[i] is None:
                b[i] = 0.0

    # Cells are dealt a FAMILY at a time - SlotA..SlotH together - and a family that will not fit in
    # what is left of the row starts the next one, so the frame _framed puts around a family never
    # straddles a row break.
    locked.update(c for c in _PLACE_ANCHORED if c in box)
    for _name, _pre, narr, _fb in program:
        for step, _chunk, cursor, _frame in _place_walk(narr):
            if step[0] in _ORIGIN_RELATIVE_TOOLS:
                named = [n for n in ((step[1].get("bodies") or []) if isinstance(step[1], dict)
                                     else []) if isinstance(n, str)]
                for c in [_CHUNK_OF.get(n, n) for n in named] + ([cursor] if cursor else []):
                    if c in box:
                        locked.add(c)
    for chunk, (x0, x1, y0, y1) in box.items():
        if x0 <= _PIN_HALF and -_PIN_HALF <= x1 and y0 <= _PIN_HALF and -_PIN_HALF <= y1:
            locked.add(chunk)
    for a, b in _PLACE_WITH.items():
        if a in locked or b in locked:
            locked |= {a, b}
    for group in _JOINT_GROUPS:
        if any(c in locked for c in group):
            locked.update(c for c in group if c in box)

    families, welded = {}, {}
    for chunk in order:
        if chunk in locked:
            continue
        # a joint group is ONE cell: its members are collapsed to a single pseudo-chunk here and
        # handed the same offset below, so the packer never deals a cell to a part a joint is about
        # to move out of it.
        lead = _JOINT_FAMILY.get(chunk)
        if lead:
            # the pseudo-chunk is prefixed so it can never collide with a real chunk name - the
            # group's leader IS a real chunk, and reusing its name would make the group's cell and
            # the leader's own cell the same entry.
            weld = "~" + lead
            welded.setdefault(weld, []).append(chunk)
            if weld in box:
                continue
            box[weld] = list(box[chunk])
            families.setdefault(_group_of(lead), []).append(weld)
            continue
        families.setdefault(_group_of(chunk), []).append(chunk)
    # the welded cell has to hold every member's authored ground, or the joint group overflows it
    for weld, members in welded.items():
        for c in members:
            b = box[c]
            w = box[weld]
            box[weld] = [min(w[0], b[0]), max(w[1], b[1]), min(w[2], b[2]), max(w[3], b[3])]

    # The field starts beside the part and steps AROUND what cannot move, rather than being
    # exiled to a band of its own - a scene twice as tall is not easier to watch. The obstacles are
    # small and clustered (the origin world, and the strip of chunks nailed to an origin plane), so
    # stepping past one costs a gap in a row, not a row.
    blocked = [(box[c][0] - _FIELD_GUTTER, box[c][1] + _FIELD_GUTTER,
                box[c][2] - _FIELD_GUTTER, box[c][3] + _FIELD_GUTTER) for c in locked]

    def clear(cx, cy, w, h):
        """The leftmost x at or after cx where a w x h cell at cy hits nothing, or None past the
        row's end."""
        while cx + w <= _FIELD_X0 + _FIELD_WIDTH:
            hit = next((b for b in blocked
                        if cx <= b[1] and b[0] <= cx + w and cy <= b[3] and b[2] <= cy + h), None)
            if hit is None:
                return cx
            # a zero-width obstacle sits exactly at cx, so step past it, never onto it
            cx = max(hit[1], cx + _FIELD_GUTTER)
        return None

    offsets, x, y, row_h = {}, _FIELD_X0, _FIELD_Y0, 0.0
    for members in families.values():
        span = sum(box[c][1] - box[c][0] + _FIELD_GUTTER for c in members) - _FIELD_GUTTER
        if x > _FIELD_X0 and x + min(span, _FIELD_WIDTH) > _FIELD_X0 + _FIELD_WIDTH:
            x, y, row_h = _FIELD_X0, y + row_h + _FIELD_GUTTER, 0.0
        for chunk in members:
            x0, x1, y0, y1 = box[chunk]
            w, h = x1 - x0, y1 - y0
            at = clear(x, y, w, h)
            if at is None:
                x, y, row_h = _FIELD_X0, y + row_h + _FIELD_GUTTER, 0.0
                at = clear(x, y, w, h)
                if at is None:
                    raise ValueError(
                        f"layout cannot place {chunk!r} collision-free within "
                        f"{_FIELD_WIDTH:g} mm field width")
            shift = (at - x0, y - y0)
            # one offset for the whole welded group - the members keep their authored relative
            # positions, so the assembly arrives in its cell already put together.
            for c in welded.get(chunk, [chunk]):
                offsets[c] = shift
            x = at + w + _FIELD_GUTTER
            row_h = max(row_h, h)
    return offsets


def _placed(steps, offsets):
    """steps with every chunk translated into the slot _place_slots gave it."""
    out = []
    for step, chunk, _cursor, frame in _place_walk(steps):
        off = offsets.get(chunk)
        if off and (off[0] or off[1]):
            step = (step[0], _place_shift(step[1], off[0], off[1], frame, step[0])) + tuple(step[2:])
        out.append(step)
    return out


def _px(chunk, x):
    """The world X an authored X ends up at, once 'chunk' is placed. A read-back that asserts WHERE
    geometry landed - the copy that must sit 100 mm right of the original, the slot whose arc
    centres must be 52 mm apart at a known place - has to ask, because the chunk moved. Asserting a
    bare authored number instead is how a layout change turns a real check into a false failure."""
    return x + _SLOTS.get(chunk, (0.0, 0.0))[0]


def _py(chunk, y):
    """The world Y an authored Y ends up at, once 'chunk' is placed. See _px."""
    return y + _SLOTS.get(chunk, (0.0, 0.0))[1]
