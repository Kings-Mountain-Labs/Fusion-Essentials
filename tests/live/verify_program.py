# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The act program: the ordered acts, the passes they run through, and the ledger tables.

`_ACT_PROGRAM` is (name, precondition, narrative, fallback) per act - a precondition read that
ERRORS routes that act to its fallback fixture. It is hoisted, laid out and framed at import
through verify_layout.py, and `ACTS` is the result run() walks. `STEPS` is the flat union the
completeness lint reads, `STORY` the per-tool shot-list note the receipt carries, `EXCLUDED` the
tools deliberately not driven unattended, `PENDING` the honest todo.
"""

from verify_acts_cam import (
    CAM_SETUP, FLIP_SETUP, MACHINING_EXTENSION, _CAM, _CAM_DELIVER, _CAM_EXTENSION,
    _CAM_FB_DELIVER, _CAM_GREEN, _CAM_MULTI_POST, _CAM_SCOPE, _CAM_SECOND_SETUP, _CAM_STORY,
    _MX_SETUP, _SW_SETUP, _SW_SETUP2, _SWARF_RIG)
from verify_acts_cloud import _CLOUD_DATA, _CLOUD_DOC, _CLOUD_DRAWING
from verify_acts_census import (
    HUB_POCKET_SETUP, _CENSUS_EXT, _CENSUS_EXT_READ, _CENSUS_LONG, _CENSUS_LONG_READ, _CENSUS_MILL,
    _CENSUS_MILL_READ, _CENSUS_POCKET, _CENSUS_POCKET_READ, _CENSUS_TURN, _CENSUS_TURN_READ)
from verify_acts_doc import _FINALE, _OVERTURE, _SHOWCASE
from verify_acts_dump import _HUB_CONTOUR, _HUB_DUMP, _MX_DUMP
from verify_acts_hub import (
    HUB_MILL_SETUP, HUB_ROT_SETUP, HUB_TURN_SETUP, _HUB, _HUB_JOB, _HUB_ROTARY, _HUB_ROTARY_READ,
    _HUB_TURNED_READ)
from verify_acts_mesh import _MACHINING, _MESH, _NESTING
from verify_acts_model import (
    _DETAILS, _DETAILS_FB, _RESIZE, _RESIZE_FB, _SOLIDS, _SOLIDS_FB)
from verify_acts_motion import _MOTION, _VISE
from verify_acts_sketch import _SKELETON, _SKETCHWORK
from verify_core import CLOUD_TIER, _DWELL, _PLANE_VIEW, _SKETCH_PLANE
from verify_layout import (
    _CHUNK_OF, _COMPONENTS, _PATTERNED, _PLACED_BOX, _SLOTS, _framed, _place_points, _place_slots,
    _place_walk, _placed, _sketch_reading_order, _sketches_first)


# --- the build, as ACTS: one machinable part, end to end, in one unsaved document --------------
# The sweep is a STORY, not a scratch pile: a bracket is drawn from one driving length, turned
# solid (stepped top, radiused pocket, through bores, a counterbored pattern, a boss, broken
# edges), detailed, re-driven parametrically, sized into a billet, clamped in a modelled vise,
# photographed, machined and discarded. Every covered tool's receipt step is woven into that story
# where it fits; where it does not, a CAMEO fixture rides inside the SAME document.
#
# Each ACT is a dict: name, precondition, narrative, fallback.
#   precondition: (tool, args) - a live read gating the narrative (the geometry it consumes exists),
#                 or None (an opening act with nothing upstream to depend on).
#   narrative:    the steps weaving the act's tools into that story.
#   fallback:     self-contained SCRATCH steps covering the SAME tools if the precondition read
#                 fails (a cascade from an upstream act that could not build) - or None for a
#                 same-doc cameo that depends on nothing. A fallback row is marked "(fallback
#                 fixture)" in the ledger so a narrative regression shows in the diff.
# A step is (tool, args, expect, save): args a dict or callable(ctx); expect "ok"/"refused"; save an
# extractor pair or None. The STORY map below gives each covered tool its ledger shot-list note.

# --- the ACT program --------------------------------------------------------------------------
# (name, precondition, narrative, fallback). A precondition read that ERRORS routes the act to its
# fallback (its tools are still covered, each marked "(fallback fixture)"). None precondition = an
# opening/cameo act that always runs its narrative.
_ACT_PROGRAM = [
    ("ACT 0 - OVERTURE", None, _OVERTURE, None),
    # SKETCH. The part's parametric profiles and the shared skeleton, then the sketch TOOLS - trim,
    # offset, pattern, dimension, constrain, text, the slot kinds - on scratch sketches of their
    # own. Both run before anything is solid, which is the order the work is done in.
    ("ACT 1 - SKETCH + PARAMETERS", None, _SKELETON, None),
    ("ACT 1b - SKETCH TOOLS", None, _SKETCHWORK, []),
    # CREATE. Material appears: the bracket off its own profiles, then surfaces, then mesh bodies.
    ("ACT 2 - SOLIDS", ("sketch_get", {"sketch_name": "BracketBody"}), _SOLIDS, _SOLIDS_FB),
    ("ACT 3 - SURFACES", None, _MACHINING, None),
    ("ACT 4 - MESH", None, _MESH, None),
    # MODIFY. Existing material is broken, rounded, patterned and drafted.
    ("ACT 5 - DETAILS", ("find_geometry", {"target": "Bracket", "kind": "circular_edge", "max_results": 1}), _DETAILS, _DETAILS_FB),
    # RE-DRIVE, while the part is still alone: the billet is sized from the part it ENDS UP as, so
    # the resize runs before the fixture rather than inside it.
    ("ACT 6 - RESIZE", ("model_inspect", {"target": "Bracket:1"}), _RESIZE, _RESIZE_FB),
    # NEST. Away from the acts that recompute the part, because the arrange solver restructures
    # what it nests under Envelope occurrences.
    ("ACT 6b - NESTING", None, _NESTING, []),
    # ASSEMBLE. The billet the part is cut from, and the vise that closes on it - the story's one
    # real mechanism, driven on camera. Its fallback is empty: every tool it drives is also driven
    # on the bench act below, which builds its own rigs and so always runs.
    ("ACT 7 - THE VISE", ("model_inspect", {"target": "Bracket:1"}), _VISE, []),
    ("ACT 7b - MOTION BENCH", None, _MOTION, []),
    # The drafted cameo the extension strategies are machined on, built here rather than beside
    # them: it is modelling work, and the acts below it run in the Manufacture workspace.
    ("ACT 8 - SWARF CAMEO", None, _SWARF_RIG, []),
    # The CAM competence world, built the way the guidance argues for: a turned hub whose three
    # dimensioned sketches are fully constrained before a feature consumes them. It depends on
    # nothing the story built and nothing depends on its geometry, so it runs its narrative always.
    ("ACT 8b - THE HUB", None, _HUB, []),
    # SHOW. Beauty shots, the whole view vocabulary, the renames and the export/import round trips.
    # It runs BEFORE the machining so the sweep ends on the job and its post, and it touches
    # neither the part's name nor its geometry, so the CAM acts address what the model acts built.
    ("ACT 9 - THE SHOWCASE", None, _SHOWCASE, None),
    # MACHINE, last: the REAL part in the REAL fixture, ending with the posted NC. Each act's
    # precondition routes to a fallback (empty when the act's tools are all covered by earlier
    # acts) so a broken story world still yields a complete per-tool ledger - on the scratch-stock
    # fixtures.
    ("ACT 10a - CAM: JOB + GENERATE", ("model_inspect", {"target": "STOCK:1"}), _CAM_STORY, _CAM),
    ("ACT 10b - CAM: DELIVERABLES", ("cam_get", {"include": ["operations"], "setup": CAM_SETUP}), _CAM_DELIVER, _CAM_FB_DELIVER),
    # The cameo's plain milling setup and the component-scope beats: a base-licence job on the same
    # drafted block, so it runs where the extension act below does not. It depends on nothing the
    # story built, hence no precondition and no fallback.
    ("ACT 10b2 - CAM: COMPONENT SCOPE", None, _CAM_SCOPE, []),
    # The Machining Extension's strategies, on the drafted cameo rather than on the bracket - rails
    # and drive surfaces need a wall that leans, and the machined part has none. They ride the same
    # document and depend on nothing the story built, so the act runs its narrative always - where
    # the entitlement is there to run it (ACT_NEEDS below).
    ("ACT 10c - CAM: EXTENSION STRATEGIES", None, _CAM_EXTENSION, []),
    # The hub's own job, added AFTER the acts above have taken their library reads: this one appends
    # ten cutters, and every index those acts select by is read off the count before it. It is
    # sequenced the way the part is made - the lathe job first, then the milling setup on the stock
    # the lathe leaves - so the turning cycles it launches are certified before anything is milled.
    ("ACT 10c4 - CAM: THE HUB JOB", None, _HUB_JOB, []),
    # The turned part read, once that poll has certified it: only valid toolpaths post, and the
    # boundary poll that certifies the turning generation runs BETWEEN acts.
    ("ACT 10c4b - CAM: THE TURNED PART", None, _HUB_TURNED_READ, []),
    # WHERE the toolpath cuts, which no other beat reads: one contour on the hub's flange wall,
    # then the dump post that writes its every motion event to a file the verdicts judge. The two
    # are separate acts because only a generated toolpath posts, and the poll that certifies the
    # generation runs BETWEEN acts.
    ("ACT 10c5 - CAM: THE HUB CONTOUR",
     ("cam_get", {"include": ["operations"], "setup": HUB_MILL_SETUP}), _HUB_CONTOUR, []),
    ("ACT 10c6 - CAM: THE DUMP ORACLE",
     ("cam_get", {"include": ["operations"], "setup": HUB_MILL_SETUP}), _HUB_DUMP, []),
    # The rail act's own simultaneous job, posted through the same dump post.
    ("ACT 10c6b - CAM: THE 5-AXIS DUMP", None, _MX_DUMP, []),
    # THE CENSUS: the rest of each hub setup's strategy vocabulary, driven one geometry kind at a
    # time. It runs AFTER the dump post, which writes every operation the milling setup holds. Each
    # census act creates and launches; the read that stands on the generation is the act after it,
    # because the poll that certifies a generation runs BETWEEN acts - and each act's own poll budget
    # is sized to the families it launched (POLL_AFTER below).
    ("ACT 10c7 - CAM: THE MILLING CENSUS",
     ("cam_get", {"include": ["operations"], "setup": HUB_MILL_SETUP}), _CENSUS_MILL, []),
    ("ACT 10c8 - CAM: THE MILLING CENSUS READ",
     ("cam_get", {"include": ["operations"], "setup": HUB_MILL_SETUP}), _CENSUS_MILL_READ, []),
    # The two whole-model families whose GENERATION wall clock (105 s and 71 s, measured) is the
    # reason they ride an act of their own: their boundary poll gets a budget sized to the pair,
    # where the census act's own is sized to sixteen short ones.
    ("ACT 10c8b - CAM: THE LONG FAMILIES",
     ("cam_get", {"include": ["operations"], "setup": HUB_MILL_SETUP}), _CENSUS_LONG, []),
    ("ACT 10c8c - CAM: THE LONG FAMILIES READ",
     ("cam_get", {"include": ["operations"], "setup": HUB_MILL_SETUP}), _CENSUS_LONG_READ, []),
    # no precondition probe: this act CREATES the setup it works in, so a read of it beforehand
    # resolves nothing and drops the act to its fallback.
    ("ACT 10c8d - CAM: POCKET CLEARING FIRST", None, _CENSUS_POCKET, []),
    ("ACT 10c8e - CAM: POCKET CLEARING READ",
     ("cam_get", {"include": ["operations"], "setup": HUB_POCKET_SETUP}), _CENSUS_POCKET_READ, []),
    ("ACT 10c9 - CAM: THE TURNING CENSUS",
     ("cam_get", {"include": ["operations"], "setup": HUB_TURN_SETUP}), _CENSUS_TURN, []),
    ("ACT 10c10 - CAM: THE TURNING CENSUS READ",
     ("cam_get", {"include": ["operations"], "setup": HUB_TURN_SETUP}), _CENSUS_TURN_READ, []),
    # The Machining Extension's own families on the same hub: three strategies no other act drives,
    # each on the geometry kind it was measured to take. The tier holds both acts back whole where
    # the entitlement is not read, and every TOOL they drive is driven by the acts above.
    ("ACT 10c11 - CAM: THE EXTENSION FAMILIES", None, _CENSUS_EXT, []),
    ("ACT 10c12 - CAM: THE EXTENSION FAMILIES READ", None, _CENSUS_EXT_READ, []),
    # THE ROTARY WRAP, in a setup of its own: creating a rotary operation invalidates every
    # operation in its setup, and the passes wrap about an axis - so they ride the hub, whose shape
    # IS a revolve about that axis, on a 4-axis machine this act builds and takes back out.
    ("ACT 10c13 - CAM: THE ROTARY FAMILIES", None, _HUB_ROTARY, []),
    ("ACT 10c14 - CAM: THE ROTARY FAMILIES READ", None, _HUB_ROTARY_READ, []),
    # The last two acts machine the PART - the flip setup and the program that spans it and the
    # first - so each is gated on the job ACT 10a built, and falls back to nothing rather than to a
    # scratch world: every tool they drive is driven again by the scratch-stock fallbacks above, so
    # a story world that could not build costs the ledger no row.
    ("ACT 10d - CAM: THE SECOND SETUP",
     ("cam_get", {"include": ["operations"], "setup": CAM_SETUP}), _CAM_SECOND_SETUP, []),
    ("ACT 10e - CAM: MULTI-SETUP POST",
     ("cam_get", {"include": ["operations"], "setup": FLIP_SETUP}), _CAM_MULTI_POST, []),
    # The last CAM step: ACT 10e ends by relaunching what its own edits left stale, its boundary
    # poll certifies the document, and this act reads the tree a watcher is left with.
    ("ACT 10f - CAM: THE TREE LEFT BEHIND",
     ("cam_get", {"include": ["operations"], "setup": CAM_SETUP}), _CAM_GREEN, []),
    # THE OPT-IN CLOUD TIER, after the whole unattended story and before the discard: the acts that
    # write into an operator's own hub. They depend on nothing the story built and nothing depends on
    # them, and the tier holds all three back whole where cloud_config names no hub (ACT_NEEDS
    # below), which is what an unconfigured run stamps as skipped. Each act ends by activating the
    # document the session was on when the tier started, so a chunk boundary may fall between them
    # and a partial --acts run leaves the session where it found it.
    ("ACT 11a - CLOUD: THE DATA MODEL", None, _CLOUD_DATA, []),
    ("ACT 11b - CLOUD: THE SAVED DOCUMENT", None, _CLOUD_DOC, []),
    ("ACT 11c - CLOUD: THE DRAWING", None, _CLOUD_DRAWING, []),
    ("FINALE", None, _FINALE, None),
]

# Every sketch that can be drawn on bare origin planes is drawn in ACT 1c, before anything is
# solid - the acts after it model, they do not sketch. The acts named here keep their own steps: the
# two sketch acts are already sketch-first, the OVERTURE has no geometry, the vise draws every
# profile on a datum plane derived from the part it is being built around, and the hub's
# constraints and dimensions stay behind the curves they close, which the hoist would carry away.
_SKETCH_PHASE, _ACT_PROGRAM = _sketches_first(
    _ACT_PROGRAM, after=("ACT 0 - OVERTURE", "ACT 1 - SKETCH + PARAMETERS", "ACT 1b - SKETCH TOOLS",
                         "ACT 7 - THE VISE", "ACT 8b - THE HUB"))
_ACT_PROGRAM = (_ACT_PROGRAM[:3]
                + [("ACT 1c - EVERY OTHER SKETCH", None, _SKETCH_PHASE, [])]
                + _ACT_PROGRAM[3:])

# Every act runs through the layout pass and then the framing pass, so a part added to the story
# later gets a slot of its own and a camera row without anyone remembering to give it either. Only
# the narrative is laid out: a fallback act rebuilds a story-less world at the origin, and the two
# never run together.
_SLOTS.update(_place_slots(_ACT_PROGRAM))

# ...then walk the sketch phase in reading order. This runs AFTER the cells are dealt because it
# needs to know which sketches got one: an origin-anchored sketch has no cell and sits a metre from
# the field, so it is drawn with the others of its kind rather than in the middle of a row. The
# re-order preserves the packer's order over the placed chunks, so _SLOTS stays true.
_ACT_PROGRAM = [(name, pre, (_sketch_reading_order(narr, _SLOTS) if "ACT 1c" in name else narr), fb)
                for name, pre, narr, fb in _ACT_PROGRAM]


def _placed_boxes(program, slots):
    """{chunk: [x0, x1, y0, y1]} once every chunk is in its slot - what the framing pass reads to
    tell whether the next subject is already on screen."""
    box = {}
    for _name, _pre, narr, _fb in program:
        for step, chunk, _cursor, frame in _place_walk(_placed(narr, slots), home_out=_CHUNK_OF):
            if chunk is None or not isinstance(step[1], dict):
                continue
            for x, y in _place_points(step[1], frame, step[0]):
                b = box.setdefault(chunk, [None, None, None, None])
                if x is not None:
                    b[0] = x if b[0] is None else min(b[0], x)
                    b[1] = x if b[1] is None else max(b[1], x)
                if y is not None:
                    b[2] = y if b[2] is None else min(b[2], y)
                    b[3] = y if b[3] is None else max(b[3], y)
    return {c: [v if v is not None else 0.0 for v in b] for c, b in box.items()}


_PLACED_BOX.update(_placed_boxes(_ACT_PROGRAM, _SLOTS))
_COMPONENTS.update(s[1]["name"] for _n, _p, narr, _f in _ACT_PROGRAM for s in narr
                   if s[0] == "model_create_component" and isinstance(s[1], dict) and s[1].get("name"))
_PATTERNED.update(c for _n, _p, narr, _f in _ACT_PROGRAM
                  for st, c, _cur, _fr in _place_walk(narr)
                  if st[0].startswith("model_pattern_") and c)
_SKETCH_PLANE.update({s[1]["name"]: s[1].get("plane") for _n, _p, narr, _f in _ACT_PROGRAM
                      for s in narr if s[0] == "sketch_create" and isinstance(s[1], dict)
                      and s[1].get("name") and s[1].get("plane") in _PLANE_VIEW})

ACTS = [(name, pre, _framed(_placed(narr, _SLOTS)), _framed(fb) if fb is not None else fb)
        for name, pre, narr, fb in _ACT_PROGRAM]

# The CAPABILITY each act declares. An act whose capability the start-of-run probe did not read as
# ENTITLED is not run at all: its steps land in the receipt's skipped(<capability> not entitled)
# bucket beside the cloud and user-present tiers, rather than reddening the sweep on the entitlement
# refusal an unlicensed installation answers a rail/surface strategy with. Steps declare the same
# thing one row at a time through verify_core.Needs; the probe per capability name lives in
# verify_core.CAPABILITY_PROBES.
ACT_NEEDS = {
    "ACT 11a - CLOUD: THE DATA MODEL": CLOUD_TIER,
    "ACT 11b - CLOUD: THE SAVED DOCUMENT": CLOUD_TIER,
    "ACT 11c - CLOUD: THE DRAWING": CLOUD_TIER,
    "ACT 10c - CAM: EXTENSION STRATEGIES": MACHINING_EXTENSION,
    "ACT 10c11 - CAM: THE EXTENSION FAMILIES": MACHINING_EXTENSION,
    "ACT 10c12 - CAM: THE EXTENSION FAMILIES READ": MACHINING_EXTENSION,
    "ACT 10c13 - CAM: THE ROTARY FAMILIES": MACHINING_EXTENSION,
    "ACT 10c14 - CAM: THE ROTARY FAMILIES READ": MACHINING_EXTENSION,
}

# Post-act hook run() fires after an act completes: the bounded generation poll between a CAM job
# act and the reads that stand on it. A value may be one setup or a LIST of them - an act that
# leaves several generating gets each certified in turn. An act the capability tier held back polls
# nothing, because nothing in it launched.
POLL_AFTER = {
    "ACT 10a - CAM: JOB + GENERATE": {"narrative": CAM_SETUP, "fallback": "Setup1"},
    # 10b ends on the Setup2 launch, so its boundary certifies that generation before the next act's
    # CAM writes start - a write landing while a generation free-runs is what parks the process. The
    # fallback lane creates a Setup2 of its own and launches nothing, so it polls nothing.
    "ACT 10b - CAM: DELIVERABLES": {"narrative": "Setup2", "fallback": []},
    # The two cameo acts route to their narrative always (no precondition), so both modes name the
    # same setup - the poll has to answer whichever key run() looks up.
    "ACT 10b2 - CAM: COMPONENT SCOPE": {"narrative": _SW_SETUP2, "fallback": _SW_SETUP2},
    # the extension act leaves TWO setups generating - the rails and the simultaneous strategies -
    # each certified in turn.
    "ACT 10c - CAM: EXTENSION STRATEGIES": {"narrative": [_SW_SETUP, _MX_SETUP],
                                            "fallback": [_SW_SETUP, _MX_SETUP]},
    # the hub job's lathe cycles, certified before the act that reads and posts them.
    "ACT 10c4 - CAM: THE HUB JOB": {"narrative": HUB_TURN_SETUP, "fallback": []},
    # the dump contour's own launch; its fallback lane is empty, so it leaves nothing generating.
    "ACT 10c5 - CAM: THE HUB CONTOUR": {"narrative": HUB_MILL_SETUP, "fallback": []},
    # the census launches one setup per act, each certified before the read act that stands on it.
    # The milling act creates sixteen operations at once, so its poll takes more than the default 40.
    "ACT 10c7 - CAM: THE MILLING CENSUS": {"narrative": HUB_MILL_SETUP, "fallback": [],
                                           "max_polls": 70},
    # the long family: its generation ran PAST 70 polls (350 s) on a session that had already driven
    # a full sweep, against the 105 s it was first measured at - so the budget is sized to the slow
    # reading, not the fast one.
    "ACT 10c8b - CAM: THE LONG FAMILIES": {"narrative": HUB_MILL_SETUP, "fallback": [],
                                           "max_polls": 160},
    # the pocket-clearing setup's single operation, certified before its read act and before the
    # turning census starts CAM writes.
    "ACT 10c8d - CAM: POCKET CLEARING FIRST": {"narrative": HUB_POCKET_SETUP, "fallback": []},
    "ACT 10c9 - CAM: THE TURNING CENSUS": {"narrative": HUB_TURN_SETUP, "fallback": []},
    # the extension families launch onto the milling setup whose stock is the lathe's rest, and that
    # generation ran past the default 40 polls with every operation reading valid behind it.
    "ACT 10c11 - CAM: THE EXTENSION FAMILIES": {"narrative": HUB_MILL_SETUP, "fallback": [],
                                                "max_polls": 70},
    # the rotary setup's own three, wrapped about the hub axis.
    "ACT 10c13 - CAM: THE ROTARY FAMILIES": {"narrative": HUB_ROT_SETUP, "fallback": [],
                                             "max_polls": 70},
    # the flip act's fallback is EMPTY, so it launches nothing and there is nothing to certify -
    # an empty target list polls nothing rather than reading a setup that was never created.
    "ACT 10d - CAM: THE SECOND SETUP": {"narrative": FLIP_SETUP, "fallback": []},
    # the last act relaunches every setup its own edits left stale, and how many of them exist
    # depends on the capability tier - so the DOCUMENT is the scope certified here, which covers
    # whichever of them launched.
    "ACT 10e - CAM: MULTI-SETUP POST": {"narrative": "document", "fallback": [], "max_polls": 70},
}

# STEPS: the flat union of every act's narrative + fallback steps - the coverage ledger the
# completeness lint reads (every registered tool must appear as some step's tool). run() iterates
# ACTS (choosing narrative or fallback per act); STEPS exists so the lint sees the whole surface.
STEPS = [s for _, _, narr, fb in ACTS for s in (list(narr) + list(fb or []))
         if s[0] != _DWELL]

# STORY: each covered tool's ledger shot-list note - the receipt doubles as the demo's shot list.
STORY = {
    "doc_new": "open the one document the whole story lives in",
    "workspace_orient": ("orient: read the empty design before building; and the entitlement block "
                         "as the CAM job opens - exactly the four sentinel strategies, each "
                         "answering its own isGenerationAllowed flag rather than reading null. It "
                         "is the same read the run's capability probe takes, which is what routes "
                         "the extension acts to skipped(machining_extension not entitled) instead "
                         "of into the refusal an unlicensed installation answers them with"),
    "sys_capability_map": "survey the server's tool families at cold start",
    "sys_find_tool": ("search the surface for the revolve verb - a registry read, so it carries no "
                      "'active_document' stamp (design_get's final read is the other half)"),
    "sys_get_api_doc": "read the RevolveFeatures API doc",
    "sys_get_guidance": ("read the packaged design guidance as a tool-only client does - the section "
                        "index, then the assemble section's rule records beside the content hash "
                        "that versions them"),
    "view_list_workspaces": "list the workspaces available",
    "view_set": ("orient the camera to the iso hero angle, with the perspective angle carried "
                 "through to the camera and read back; then the whole verb set on the finished "
                 "fixture - snapshot, a turntable through every camera preset (one framed orient "
                 "sets the subject, the rest rotate about it with fit=false), every visual style, "
                 "isolate/hide/show/clear_isolation, a persistent Named View saved, found in the "
                 "document's own list and re-applied, and restore putting camera, style and every "
                 "bulb back where the tour started. SKIPPED(rig): the snapshot/restore "
                 "truncation beats (truncated + occurrence_cap) need an assembly with more "
                 "occurrences than the cap, and the story document stays well under it"),
    "sys_get_selection": "expected refusal: nothing is selected yet",
    "sys_get_preferences": ("read the application's own configuration - the default projection, two "
                            "decoded enum families, the compatibility group, and all three members "
                            "the census found RAISING on this build, each published null and named "
                            "in 'unreadable' with none of them miscategorised as a member the build "
                            "does not carry"),
    "sys_set_preferences": ("round-trip one invisible preference and restore it in the same act; "
                            "the below-minimum value and a tier-R member refused"),
    "param_add": ("add PartLen (the driving length), PartWid and PartHt (independent extents), and "
                  "the feature parameters derived from them - step, bore, pocket depth and corner "
                  "radius, boss, mounting bore, edge break"),
    "param_set_favorite": "mark PartLen the favorite driving dimension",
    "param_get": ("read the parameter table; then the pocket radius and the edge break the fillet "
                  "and chamfer features are built at, and a fresh driver read sizes the CAM stock"),
    "model_create_component": ("cast the part, the billet, the three vise parts and the lead screw; "
                               "then the HUB, the turned flange the CAM competence beats machine"),
    "design_activate_component": ("step into each part to build its sketch, and back out to the "
                                  "root once the hub's last feature has landed"),
    "sketch_create": ("draw each part's sketch on its plane; then the hub's five - its turned "
                      "outline and its keyway on the axis plane, the bolt circle and the flange "
                      "pocket on XY, and the inclined flat on a datum swung 30 deg off XZ"),
    "sketch_add_geometry": ("draw the part's footprint, its step, its pocket and its boss; then the SLOT family, "
                            "one scratch sketch per shape - a three-point arc slot with its five "
                            "arcs and its profile, the centre-point arc slot in both its short and "
                            "its full ladder with each dimension flag gated independently, an "
                            "overall slot whose cap centres prove the tip-to-tip measure, the "
                            "length/angle tail that adds the fourth line and its own dimensions, "
                            "the centre-point slot's HALF length landing a cap on its second point, "
                            "and the legacy centre-to-centre form; the angle with no length, the "
                            "angle FLAG on a straight slot, each cross-kind input pointed at the "
                            "kind that carries it, and a tail on the legacy form all refused - the "
                            "legacy form's own census read twice over (its note names the 2 solid "
                            "lines, the 1 construction line and the 2 arc caps behind a "
                            "'curves_added' of 3, and sketch_get finds exactly that); plus the "
                            "line/rectangle/polygon floors the composite counts are read against"),
    "sketch_add_3d_line": "draw the vertical axis as the skeleton's 3D line",
    "sketch_constrain": ("constrain the skeleton's X axis horizontal; then autoConstrain a loose "
                         "rectangle to fully constrained and re-run it as a no-op, lay a "
                         "rectangular pattern by total EXTENT with the landed centre measured, "
                         "suppress two instances of a 3x2 pattern with the landed flags and the "
                         "curve count both read back; the N-1 flag length, a knob on the wrong "
                         "constraint, and the dimensioning strategies this build does not carry "
                         "all refused. Then the second bench, carrying the kinds the first has no "
                         "geometry for: vertical, collinear and concentric; a spline made "
                         "curvature-continuous with the line it continues; the two point-pair "
                         "kinds; a square told it is a polygon; fix and unfix on one curve; and "
                         "the three CREATOR kinds - one-sided and two-sided offset, and a "
                         "six-around circular pattern - each in a sketch of its own with the "
                         "curves it drew counted"),
    "sketch_move": ("shift a line by a known offset and read the new coordinates back, then spin it "
                    "180 deg about its own midpoint - the swap only the endpoints show; the "
                    "negative-scale mirror and the empty transform refused"),
    "sketch_copy": ("copy a two-line chain to an offset position, its new refs and the endpoints in "
                    "the returned collection both accounted for, then across into a second sketch, "
                    "and again inside a COMPONENT sketch where the refs cross the occurrence-proxy "
                    "seam"),
    "sketch_insert_svg": ("import the logo art into a fresh sketch, its landed width measured "
                          "against the 1/96-inch-per-user-unit convention, then a 96-user-unit "
                          "square at scale 1 whose measured extent pins BOTH halves of that "
                          "landing - one inch square, and Y-DOWN from the sketch origin (min y "
                          "-25.4 mm); the missing file refused"),
    "sketch_dimension": ("drive the part's length, width and boss diameter by parameter expression; the wedge angle facing "
                         "the sketch origin; offset against a non-parallel line (rotated, and the "
                         "note says so) with linear_diameter refusing the same shape; line and "
                         "point measured to a model face; then the dimension bench - a slanted "
                         "line's horizontal span, a diameter, the gap between two circles on one "
                         "centre, a line to a circle's near tangent, and an ellipse's two radii - "
                         "each read back as a measured number, not a call that returned ok"),
    "sketch_get": ("read the skeleton and the part's own profiles back; then the hub's three "
                   "PROOFS, each taken before a feature consumes the sketch - is_fully_constrained "
                   "true with the constraint and dimension counts that closed it, which is the "
                   "sketch recipes' own bar; and the keyway slot's own census, where WHICH of its "
                   "three lines is the construction spine is read off the sketch rather than "
                   "counted on (a solid side line is vertical too and spans the same length, so a "
                   "dimension addressed at one lands the slot half a width off)"),
    "sketch_delete_entity": ("delete a helper constraint; count drops - then a sketch text by its "
                             "index, the deleted string reported back, and the empty index refused"),
    "model_construction": ("offset the step floor the pocket is cut from and the top the boss stands on; an AXIS on a cameo bore whose published handle the circular pattern turns about; a plane at 30 deg about a bench bore's own axis (origin pinned to the axis) and a plane through a cap vertex; then the ON-PATH surface on one measured 30 mm cap edge - a proportional plane and point reading their ratio back with no extent published, an absolute placement inside the path, one before the start and one far past the end (both accepted, both disclosed against the measured length), the boundary exactly at the length, an expression placement whose model parameter is named for param_set, a to-object plane carrying distance AND offset off the path and a second one landing inside a two-edge chained path, and the summed length of that chain; the out-of-range proportional value and to_object on the point kind refused. Then the datum bench - one bored block carrying every reference the remaining modes read: a plane swung 30 deg about a top edge, one spanning three corners, one splitting the block at mid-height, one spanning two coplanar edges and one resting tangent on the bore wall; an axis on an edge, one spanning two corners and one along the top face's own normal; and points at the bore centre, at a corner where two edges meet, at the three world planes' shared origin and where an edge pierces XY. The world axis and the coordinate point are refused up front - both are setByLine/setByPoint, direct-edit-only, and this design is parametric. And the hub's own datum: the axis plane swung 30 deg about the SHAFT WALL, whose frame maps its +X onto world Z alone - which is what lets the inclined flat be drawn in depths and radii measured from the hub's axis rather than from the datum's parametric origin"),
    "sketch_set_text": ("engrave the FUSION ESSENTIALS nameplate; then the path layouts - text "
                        "along a line and wrapped around a closed circle, and fitted to a line - "
                        "each checked against the created text's own definition objectType; a model "
                        "edge as the path, the three cross-mode inputs, and a layout input on an "
                        "edit all refused; then the FONT - named on a create and on an along-path "
                        "create, read back off the landed text both times, then changed on an edit "
                        "beside the string; an unknown name and its case variant refused on create "
                        "with the text count proving nothing landed, the same name refused on an "
                        "edit with the following read showing the string untouched, and a call "
                        "with no font_name publishing no font key at all. Then the hub's own "
                        "label - 'HUB' drawn 3.5 mm high on the flange-top outline sketch, its "
                        "width measured off the created text's own bounding box; nothing cuts it"),
    "model_extrude": ("extrude the block, JOIN the stepped half and the boss onto it and CUT the "
                      "pocket down from the step floor, each depth an expression off the driver; "
                      "then a three-bay frame with 'all' whose payload NAMES the regions enclosed "
                      "by another selected one - the bays that filled with material. Then the "
                      "hub's three cuts: the keyway symmetric about the axis plane, the inclined "
                      "flat cut from the tilted sketch, and the flange pocket where 'all' takes "
                      "TWO regions and neither encloses the other - the sector and the round "
                      "pocket beside it, both cutting something a viewer can see"),
    "model_revolve": ("revolve a torus ring about the world z axis, then a ring about an off-origin "
                      "cylinder FACE - the resolved label reads BRepFace and the ring's measured "
                      "bounding box stands around x=30, not around the origin; a planar face as "
                      "the axis refused. Then the HUB itself: its thirteen-segment outline turned "
                      "360 deg about the construction line the outline closes on, off a profile "
                      "handle rather than a guessed index"),
    "model_loft": "loft a base-to-post cameo between two profiles on stacked planes",
    "model_sweep": "sweep a round section along its own path",
    "model_draft": "draft a cameo face",
    "model_mirror": ("mirror a cameo body, then the emboss block's own timeline FEATURE with the "
                     "body/volume census read back, then a join whose isCombine is read off the "
                     "created feature, and meet the join-is-bodies-only refusal"),
    "model_emboss": ("raise then engrave a circular profile on a scratch block's top face, each "
                     "checked against the volume direction; a zero depth refused"),
    "model_replace_face": ("replace a scratch block's top face with an open sheet above it, the "
                           "measured volume move pinning the effect; a solid face as the target "
                           "refused"),
    "model_pipe": ("run a hollow pipe along its path with the wall read back off the feature (and "
                   "NO capped_ends claim - the face collections that would answer that read empty "
                   "on this build), then a half-path pipe whose bounding box proves the extent is a "
                   "FRACTION, a CUT scoped to a named body where the note says the scope is what "
                   "was REQUESTED because participantBodies cannot be read back, and the two "
                   "chaining fixtures: ONE seed handle chains across TANGENT junctions and stops "
                   "where that continuity breaks - several edges on an open run bounded by sharp "
                   "corners, and all eight of a closed tangent loop (which reports itself closed) "
                   "- so what a seed produces is the BUILT path's own count and nothing about the "
                   "request predicts it; the reverse extent refused on an open path"),
    "model_pattern_rectangular": "rectangular-pattern a cameo body",
    "model_pattern_circular": ("circular-pattern a cameo body about a world axis, then about a "
                               "construction axis by handle and by name with the resolved label "
                               "read back, then about the bore FACE itself; the datum name reached "
                               "from the root refused"),
    "model_pattern_path": ("pattern the feature cameo along its own edge with the count read back "
                           "off the feature's own patternElements, then along TWO connected edge "
                           "handles - a list is used EXACTLY, with no chaining, and the label says "
                           "which of the two path rules ran"),
    "assembly_edit_relations": ("suppress/unsuppress a scratch rigid group, re-value the bench link with was_reversed disclosed, and meet the measured set_occurrences refusal in the words that make it a fact - the build it was measured on and the platform sentence it would raise - with the group's members re-read unchanged afterwards"),
    "assembly_edit_contacts": ("build a contact set from two story parts, meet the single-member refusal, re-member it, rename it reading the landed name back, suppress round-trip, switch contact analysis on and back off, then delete it"),
    "model_hole": ("drill a cameo mounting hole, then the three additive placements - centred on "
                   "its rim, on an edge at middle and at start, and by plane offsets; a circular "
                   "offset edge refused. Then the hub's bolt circle: four counterbored through "
                   "holes at WORLD points on the 32 mm circle its own reference sketch dimensions, "
                   "which is the pattern the census aims its hole families at"),
    "model_combine": "join two overlapping cameo pads",
    "appearance_set": ("give the part, the billet and each vise component its own colour, with the "
                       "billet half translucent so the part inside it stays visible; then the occurrence FAN-OUT in the "
                       "shape that discriminates - one body coloured directly, then the occurrence "
                       "written in a DIFFERENT colour, so the body holding its own override comes "
                       "back under 'bodies_not_reached' and not under applied_to (both colours are "
                       "minted from one base asset and share an Appearance.id, so only comparing "
                       "the id AND the name separates reached from kept); and the same shape where "
                       "that body is the occurrence's only one, refused naming it"),
    "model_set_material": "assign the bracket a physical steel material",
    "find_geometry": "acquire the face/edge/body handles the build consumes",
    "model_measure_between": ("measure the boss wall to the bore inside it; then each jaw's grip "
                              "face to the billet, which is what says the vise is closed"),
    "model_measure_relation": ("read the boss and its own bore coaxial; then the rest of the vocabulary on "
                               "the datum bench, each reporting its OWN measurement - the top face "
                               "perpendicular to a wall it meets and touching it along that edge, "
                               "flush with itself, the bore concentric with itself, and 20 mm "
                               "clear of the floor below"),
    "model_inspect": ("read the part's own extent back, and the billet's against the allowance it "
                      "was sized with; and a MESH addressed as "
                      "'<occurrence>:<mesh>' inside a singly-placed component, answering with its "
                      "own triangle count and volume rather than the solid it was cast from; and "
                      "the hub's turned envelope read back after EVERY cut, which is what says the "
                      "keyway, the flat, the pocket and the thread all took material from inside "
                      "it rather than off its outside"),
    "pmi_create": ("author a flatness note on the pocket floor and a hole note on a mounting bore, "
                   "each read back by name and markup"),
    "pmi_get": ("read the PMI back with segments and detail, and again with an over-cap "
                "max_results - pmi_get's own contract CLAMPS it rather than refusing, since every "
                "record it returns crosses the wire whole. SKIPPED(rig): the imported-row beats "
                "(no 'text' key on an imported annotation, no 'is_hole' when isHoleAnnotation will "
                "not read) need a PMI-BEARING import; the STEP this sweep round-trips carries none"),
    "pmi_edit": ("rewrite the flatness note to a perpendicularity callout and read the markup back; "
                 "the blank-name guard refuses"),
    "pmi_delete": "delete the note and read the remaining PMI count",
    "assembly_ground": ("ground the vise base the jaw slides on, and the bench base every station's "
                        "motion is read against"),
    "assembly_rigid_group": ("rigid-group the vise base with its fixed jaw - one body as far as the "
                             "machine is concerned - and a scratch pair for the relations lifecycle"),
    "joint_create_origin": ("place the stock-center WCS at the part's origin, a station per bench "
                            "motion, the setup's own WCS at the billet's measured centre, and the "
                            "flip setup's at the part's; then the hub's rotary WCS, computed at "
                            "its bounding-box centre - the layout deals the hub a cell a metre out "
                            "in the field, so the axis the rotary passes wrap about runs through "
                            "that origin and not through the world's"),
    "joint_create": ("slide the moving jaw on the vise base and turn the lead screw on it; then a "
                     "station joint per bench motion, and the link's own fresh revolute"),
    "joint_at_geometry": ("joint a pin in its bore via cylinder faces; then the motion vocabulary on "
                          "its own cameo tree - a BALL on a real sphere face (the centre key point "
                          "named in the payload, no axis and no axis sentence), a revolute on an "
                          "explicit axis whose note says frame, NOT world, and points at the tool "
                          "that sets a true world axis, and a rigid pair with the same axis-free "
                          "report; an axis outside the Choice refused. A TORUS face joints at its "
                          "own centre on a PARAMETRIC body, which is the case the keypoint guard "
                          "must let through. SKIPPED(rig): the two base-feature halves of that "
                          "guard (a torus inside a base feature hands back its component origin "
                          "with no error) need a torus built INSIDE a base feature, and no tool on "
                          "this surface builds one unattended"),
    "joint_create_as_built": ("seat the part inside the billet it is cut from, then the billet in "
                              "the jaw that closed on it - both as-built, both where they stand; then a REVOLUTE "
                              "as-built pair anchored on their shared face, read back through "
                              "assembly_get and driven to prove the DOF, with the missing-anchor "
                              "and rigid-plus-anchor refusals"),
    "joint_edit": ("set travel limits on the vise's jaw slide and rotation limits on the bench's "
                   "revolute station; then walk one scratch joint through every "
                   "motion the tool offers - rigid to revolute, slider, cylindrical, planar, ball "
                   "and pin_slot - each retype witnessed by the design's own joint walk rather "
                   "than by the writer, the mismatched pin_slot axis pair refused, and the bench "
                   "left on a revolute that actually drives"),
    "joint_motion_link": ("couple the vise's lead screw to its jaw at 45 deg of handle per mm of "
                          "travel (6 mm of closing reads 270 deg - a full turn would read as no "
                          "turn); and a bench revolute pair at 2:1"),
    "joint_drive": ("drive the vise jaw OPEN and then closed onto the billet, the screw turning "
                    "with it through the link; then the bench's revolute and slider stations, and "
                    "every station that has a degree of freedom, home again afterwards"),
    "assembly_get": "read the joint wiring, driven angles, and the StockCenter anchor back",
    "assembly_move": "pose a scratch cameo occurrence",
    "assembly_capture_position": "status, discard the pending pose, re-arm and capture",
    "assembly_constrain": ("flush-constrain a scratch cameo pair through the single-pair shorthand; "
                           "then the SET form - one constraint feature carrying two relationship "
                           "rows of different inferred types, a face-to-face mate at a 2 mm offset "
                           "with the normals flipped plus a concentric one on the same two discs, "
                           "which is how Fusion's own Constrain dialog locates a part. The count "
                           "read off the CREATED constraint is what says both rows live in the one "
                           "feature - the tool refuses a constraint holding fewer than submitted"),
    "design_add_instance": ("place two more pin-cameo instances and read the landed paths back, the "
                            "second naming the component while two of it already stand; the "
                            "self-nesting target refused"),
    "design_move_occurrence": ("re-parent one of those instances under the frame, the new path and "
                               "the held world position both read back; the root target and the "
                               "self-nesting target refused"),
    "assembly_inspect_interference": "check interference at rest and driven",
    "design_recompute": "recompute the assembly after motion",
    "model_fillet": ("round the pocket's four corners at the radius the parameter states, break "
                     "the step's leading edge, blend the boss rim; then the two path fixtures - one box corner "
                     "rounded into an OPEN tangent run, and all four rounded into a CLOSED tangent "
                     "loop - that the chaining beats read their edge counts off; and the EDGE "
                     "FILTER, on an L prism whose 18 edges are 17 convex and one concave by hand "
                     "count - the census partitions them, the filter requests the concave one "
                     "alone, and the volume grows because filling a corner adds material; then the "
                     "SMOOTH branch on the body that round curved - 21 edges reading 19 convex, no "
                     "concave one left and the two tangent joins smooth, published in the refusal a "
                     "filter matching nothing carries. And the hub's flange RIM, rounded 1.5 mm on "
                     "the one circular edge told from its twin below it by its own centre"),
    "model_chamfer": ("break the step's outboard edge and a through-bore rim, then a mounting-bore "
                      "rim by distance-and-angle with a miter corner, each read back off the "
                      "created feature"),
    "model_shell": "shell a scratch cap cameo",
    "model_offset_face": "push a scratch block's top face outward",
    "model_thread": ("thread a scratch post M10x1.5 over part of its length with the extent read "
                     "back, an explicit thread standard with its alternatives disclosed, and a "
                     "modeled thread on a second post proving it cut material; then the INTERNAL "
                     "side on a real bore - 'internal' derived from the face's own out-of-material "
                     "normal and checked against the face by the API at add() - and a partial "
                     "thread measured from the LOW end, reading that end back off the feature; an "
                     "unknown call-out, an offset with no length, and a modeled call-out too big "
                     "for the cylinder all refused. And the hub's stub, M30x2 over 15 mm from its "
                     "low end, on the ONE cylinder face of its radius the whole part carries - "
                     "which is what says nothing has been cut across it"),
    "sketch_edit_curve": ("trim, extend, split, fillet, chamfer and offset on one scratch sketch "
                          "per action, with length read-backs; split's two halves must carry "
                          "distinct ids; a chamfer across an offset pair refused"),
    "model_scale": ("uniform x8 and per-axis x*y*z scales with ratio read-backs; unresolvable, "
                    "length, and angle expressions refused; a bare unitless parameter accepted; "
                    "a vertex-anchored scale"),
    "model_move": ("translate, along-axis, rotate and point-to-point move features on a scratch "
                   "block in a SINGLY placed component, each checked against the distance it was "
                   "asked for; the same along-axis move on a component placed TWICE refused naming "
                   "the count and both paths (each instance holds that body somewhere else, and no "
                   "read-back tells a right instance from a wrong one); a face as the axis and any "
                   "faces selection refused"),
    "design_delete_feature": "add a wart feature then delete it; health diff",
    "design_remove_feature": "remove a scratch body and its occurrence; deleting each Remove brings them back",
    "design_delete_occurrence": "delete a scratch occurrence",
    "view_section": "section cut along the part's bore axis",
    "view_screenshot": ("capture the sectioned part, and the vise open before it closes; write the same path twice to show the "
                        "overwrite, and refuse a write against a document that is not active; and "
                        "shoot view='current' - the no-move capture, the only way to keep a frame "
                        "the camera already holds, since a NAMED view refits the whole model"),
    "view_screenshot_multi": ("capture the front and top beauty shots; then a four-view contact "
                              "sheet, the camera restored afterwards"),
    "surface_revolve": ("revolve a prep sheet; and the half-disc that closes into the ball joint's "
                        "sphere"),
    "surface_fill": ("seal a closed revolved sphere surface into a solid, the volume measured "
                     "off the result and every tool accounted for; and seal the joint cameo's "
                     "sphere the same way, so the ball beat has a real sphere face to joint at; a "
                     "cell index one past the end refused NAMING the range that exists, with the "
                     "computing input cancelled and nothing created"),
    "surface_thicken": ("thicken the prep sheet, then a four-walled sheet with 'rounded' corners "
                        "read back off the input"),
    "surface_extrude": "extrude prep sheets",
    "surface_offset": "offset a sheet face zero and nonzero",
    "surface_extend": ("extend a sheet edge with no alignment given (the payload carries no key, so "
                       "nothing was written), then a second sheet extended with 'align_edges' read "
                       "back"),
    "surface_reverse_normal": "flip a sheet normal",
    "surface_delete_face": "open a bore by deleting a face",
    "surface_patch": ("close the opened bore with a patch, then the same rim at 'tangent' "
                      "continuity and again through one interior RAIL whose landed count is read "
                      "off the input; rails paired with the multi-loop form refused"),
    "sketch_project": ("project the machining boundary; then section the cap on a datum plane with per-source attribution naming the parallel face that contributed nothing, project the cap sketch's line onto the top face reading the reference linkage back, and meet the same-sketch and missing-direction refusals. SKIPPED(rig): the cross-document to_surface identity fixture is not scripted"),
    "surface_trim": "trim a sheet with a cylinder cutter",
    "surface_untrim": "untrim the internal hole loop",
    "surface_create_ruled": ("rule off a sheet's top rim - tangent, normal, along a direction "
                             "entity and at an angle read off the feature - then off a SOLID box "
                             "edge, where only the new sheet is the result; both misuse refusals"),
    "model_split": "split a scratch pin by a plane",
    "model_unstitch": "unstitch a scratch box's faces",
    "model_stitch": "re-stitch two faces",
    "model_base_feature": "open and close a base-feature scope",
    "model_arrange": ("nest a square pad, a bar, a disc and a second pad inside a HEXAGON boundary, "
                      "then scale the boundary and solve again - the same four parts re-nest, which "
                      "is the arrangement being a function of the boundary rather than a one-time "
                      "placement"),
    "model_compute_holder": "compute a CAM tool holder (read)",
    "save_as_mesh": ("mesh a scratch solid (one per destructive op); then the same solid reached "
                     "through the qualified '<occurrence>:<body>' address, with the bare 'Body1' "
                     "refused as ambiguous and the refusal offering that very spelling"),
    "mesh_get": "read the mesh back",
    "mesh_generate_face_groups": "group the mesh faces",
    "mesh_to_brep": "convert a mesh to a base-feature BRep",
    "mesh_reduce": "reduce a dense mesh",
    "mesh_remesh": "remesh a copy",
    "mesh_plane_cut": "plane-cut a mesh copy",
    "mesh_combine": ("combine two mesh copies, the parametric mode and the base feature the write "
                     "ran in both named; then merge two disjoint meshes into one body"),
    "mesh_delete": "delete a scratch mesh body with the design-wide survivor re-scan",
    "mesh_export": "export a mesh to STL",
    "mesh_insert": "re-import the STL mesh",
    "mesh_repair": ("one-touch-fix a healthy mesh (an honest no-op, not a failure), rebuild it with "
                    "the density read back off the feature, stitch-and-remove a fresh mesh TWICE - "
                    "the first welds its duplicate vertices, the second finds nothing of its kind "
                    "to fix and the note has to say so rather than claim a repair - and refuse "
                    "density on a non-rebuild; then every other rebuild method with its own "
                    "density read back off the feature, 'offset' accepted by the accurate method "
                    "and refused on the rest, and a shrink-wrap close. SKIPPED(rig): the "
                    "close_holes refusal on a mesh that "
                    "stays open needs an UNFIXABLE open mesh, which nothing in this document can "
                    "build - every mesh here is watertight by construction"),
    "mesh_shell": ("hollow a scratch mesh - the volume DROPS, the body still reads watertight and "
                   "the thickness is read back off the feature, never echoed - then meet the "
                   "platform's own MESH_FAILED_HOLLOW refusal at a thickness past the half-wall "
                   "(measured: an over-thick shell does not quietly cut through, it fails)"),
    "mesh_smooth": "smooth a scan-quality mesh: the triangle count HOLDS STILL and the node coordinates move, which is why a count census cannot judge it",
    "mesh_separate": "split a two-shell mesh into its lumps - the pieces are the auto-named bodies read back from the component",
    "mesh_reverse_normal": "flip an inside-out mesh - confirmed by the signed volume changing sign, not by is_closed",
    "design_edit_timeline": ("roll the marker back a step and to the end, refuse a discard without "
                             "the confirmation, refuse an unknown feature and a bad group range, "
                             "and tag a feature with an attribute then delete it - the value and "
                             "the design-wide count are read back both ways, and a second delete is "
                             "refused; then the 'name@index' form a FeatureRef refusal hands back, "
                             "resolved against each object's OWN .index (the index design_get "
                             "publishes), with the neighbouring index refused as a miss. "
                             "SKIPPED(rig): the AMBIGUOUS-name refusal itself needs two same-named "
                             "timeline features, and no tool on this surface renames a feature, so "
                             "the sweep cannot mint the pair"),
    "param_set": "bump PartLen +33%, measure the part grew with it, then restore it",
    "param_delete": "delete a scratch parameter",
    "view_switch_workspace": "switch to Manufacture, then back to Design",
    "cam_get": ("read the CAM job structure, and the recorded-probing slice on a job nothing has "
                "probed: the empty state with its reason named, a scope that invents no measure, "
                "and the units refusal. Then the three reads the machining acts stand on - the "
                "strategy slice, read on every setup before a single operation is created (its "
                "allowed/blocked/unreadable tallies PARTITION the setup's own vocabulary, and each "
                "strategy the act goes on to create is named in it reading allowed, so a spelling "
                "this build does not carry reds on the read rather than inside a create); the time "
                "slice as the non-empty oracle, taken over the WHOLE job - every operation's own "
                "machining time above zero, which is the one read that tells a cutting toolpath "
                "from an empty one with hasToolpath reading true on both; and the operations "
                "census where the states tally and active_count partition one row set across a "
                "suppression. Plus the templates slice at the SHIPPED location, where the url the "
                "hole-drilling bundle is applied by comes from, with url_basis naming how it "
                "addresses the asset. And the turning setup's own two reads: the setups slice, "
                "where its operation_type and its blocked_by are read off the Setup on both sides "
                "of the machine assignment, and the parameters slice for the stock mode and the "
                "turning WCS origin it turns from"),
    "cam_edit_tools": ("stock the document library with the shop set this part is cut with - a "
                       "50 mm face mill, a 10 mm flat mill (10, not 12, because it has to fit "
                       "inside the 12 mm bore it finishes), a 6 mm ball, a 6 mm drill and a "
                       "chamfer mill, each given its own tool number because two sample clones "
                       "that share one make the post refuse - then the turning and center-drill "
                       "pair the from_type census stands on; preset add/remove round-trip "
                       "with unit, refusal, and rollback gates; the summary census and the same "
                       "census narrowed by tool type, one tool's full parameter list, and the "
                       "LOCAL scope answering with libraries instead of tools; a fifth tool added "
                       "and removed with the count read back; and once the job is generated, "
                       "where_used naming the operations that cut with the mill and reporting NONE "
                       "for the turning tool nothing selected. The document library refuses to "
                       "host a new library and where_used refuses a shared scope - a shared "
                       "library has no operations, so an empty list there would read as 'none'. "
                       "Then the two tools the extension acts cut with - a 12 mm flat mill "
                       "carrying its own preset and a 6 mm ball, added at the index the library's "
                       "own count named - and the flute LENGTHENED on the mill with the "
                       "expression read back off the tool, which is where a cutting-tool dimension "
                       "is edited (the operation refuses that write). And the PROBE the probing "
                       "cycle needs, cloned by from_type from the shipped 'Probes' library at the "
                       "index the document library's own count named. Then the HUB's own shop set "
                       "- ten cutters in ONE add, milling and turning, read back off the library "
                       "in the order they were handed in, since that ORDER is the contract every "
                       "hub create row picks its cutter by"),
    "cam_create_setup": ("create the milling setup on the bracket in the vise, and the FLIP setup "
                         "that turns it over on its own WCS - the pair one NC program ends the "
                         "sweep on; then the two on the drafted cameo, one for the rails and one "
                         "for the simultaneous strategies; and the HUB's three - the TURNING one "
                         "whose operation_type is read back off the Setup itself, the milling one "
                         "that cuts what the lathe leaves, and the one the 4-axis wrap turns in - "
                         "each created holding ZERO operations, which is what says the row made "
                         "the setup it names"),
    "cam_create_operation": ("build the job a shop would run on the bracket: face the top, rough it "
                             "with the 3D adaptive, open the pocket with 2D offset roughing, "
                             "contour the boss, break the stepped top's edges with a 2D chamfer, "
                             "then the hole making - spot the two through bores, drill the "
                             "counterbored mounting pattern, and bore the through holes to size - "
                             "then face the underside and contour the "
                             "counterbore backsides in the flip setup. The extension's own "
                             "steep-and-shallow pass cuts the boss beside the 2D one where the "
                             "licence allows it and is skipped where it does not, so the job reads "
                             "the same either way. Then the three extension strategies on the "
                             "drafted cameo - swarf, deburr and geodesic - and the refusal for a "
                             "strategy the setup offers that this installation reads "
                             "isGenerationAllowed false on, which creates nothing rather than "
                             "minting an operation that never generates. Then the families the "
                             "bracket's job has no geometry for: an engraving on the part itself "
                             "and the three simultaneous passes (multi-axis finishing and "
                             "roughing, flow) in a setup of their own. And the PROBING cycle on "
                             "the part itself: a Probe WCS pass created with the cloned probe, "
                             "which is the tool kind a probing strategy is refused without. On the "
                             "HUB: the four lathe cycles in the order a shop turns them - face, "
                             "profile roughing, profile finishing, then the part-off on the "
                             "grooving insert - and the three rotary families wrapped about its "
                             "own axis, every one created with no selection at all, because the "
                             "geometry each of them cuts IS the setup's model"),
    "cam_select_geometry": ("aim every operation at the feature it cuts: the stock-top face, the "
                            "boss top and the stepped top through the FACE kind (which takes the "
                            "loops that bound the face), the pocket FLOOR through the POCKET kind "
                            "made for it, both silhouette branches (setup models and named "
                            "bodies), a whole scratch sketch, and the holes three ways - the "
                            "counterbored mounting bores through the diameter filter, the two "
                            "through bores with no filter at all (the branch that machines exactly "
                            "what it was given), and those same two faces again to the BORE, where "
                            "one selection input reaches 'circularFaces' instead of the drill's "
                            "'holeFaces'; refusals for a knob on the wrong kind, geometry through the wrong "
                            "input, and an edge where a face belongs. The pocket-recognition "
                            "selection is NOT driven unattended (running it coincides with the "
                            "Fusion process terminating); its 'pocket_filter_applied' publishes the "
                            "diameter/depth bounds in the CALLER'S own units, with "
                            "'pocket_filter_units' naming them beside the numbers. Then the two "
                            "routes a 'chain' takes on the drafted cameo: a swarf RAIL PAIR (one "
                            "selection per rail, the open state read off the collection the "
                            "operation hands back, the drive mode engaged in the same call and the "
                            "rail order published, with a single contour refused as a pair) and a "
                            "deburr edge whose payload carries none of those keys; plus the "
                            "'surfaces' selection with its face count read back off the named "
                            "surface set, a role the operation does not carry refused naming the "
                            "ones it does, and an omitted role refused where there is no drive set "
                            "to default to. Then the COMPONENT SCOPE, on the scratch job's own "
                            "silhouette: 'Body1' unscoped refused with the candidates and with the "
                            "input that narrows them, then scoped to one component, where "
                            "'selected' is the qualified name of the body that reached "
                            "inputGeometry. The sketch kind drives the engraving, and the surfaces "
                            "kind lands ONE drafted wall on the multi-axis finishing pass's FLOOR "
                            "set and on the flow's DRIVE set - the same face, two roles, each read "
                            "back off the parameter it landed on. The PROBE kind lands the stepped "
                            "top on the probing cycle's own probe_selection input, named in the "
                            "payload beside the count"),
    "cam_edit_operation": ("edit the face operation's feed; then park the drill operation and "
                           "restore it - the suppression WRITE, with hasToolpath read back on both "
                           "sides of the set so the discarded toolpath is reported, not implied. "
                           "Then on the swarf operation: the PRESET arm, with the name read back "
                           "off Operation.toolPreset beside the preset it ran before and a miss "
                           "refused listing the tool's own; the cutting side written and read back "
                           "off its parameter; and the isEditable pre-guard refusing a cutting-TOOL "
                           "dimension by name before anything is applied. Then the deburr's "
                           "multi-pass in ONE call, the stepover row named FIRST though it is "
                           "settable only once the flag above it is true - written last, its own "
                           "flag re-read, and marked unlocked_here in the row it reads back; and "
                           "the TOOL arm on EVERY operation the shipped template landed with none - "
                           "a spot drill, a drill and a mill that fits the counterbore, each with "
                           "Operation.tool read back beside a null was_tool. The rail operation "
                           "parked for the 3-axis program is restored at the end, so the tree the "
                           "run leaves holds nothing suppressed. And on the hub's lathe job, the "
                           "stock-to-leave switch and its two allowance rows in ONE call on the "
                           "roughing cycle, and doLeadOut off on the finishing cycle it feeds - "
                           "what the no-warning read of that setup stands on"),
    "cam_create_machine": ("build a run-stamped 3-axis machine into the Local library, find it in "
                           "the catalog, assign it to the setup, and refuse the duplicate name; "
                           "then a run-stamped generic_4_axis for the hub's rotary setup, which is "
                           "what a rotary wrap needs before it will generate"),
    "cam_delete_machine": ("take the run's own machine back out of the Local library: the "
                           "confirm_name mismatch refused while it still exists, then the delete "
                           "proved by the library walk, the name re-resolve, and the catalog read "
                           "that listed it when it arrived; and the hub's 4-axis one taken out the "
                           "same way as soon as the setup holds its own copy - Setup.machine takes "
                           "a COPY, so the run leaves the Local library as it found it"),
    "cam_edit_setup": ("real stock + vise fixture bodies; WCS bound to the stock-center JO (bound "
                       "read back); Haas VF-2 assigned - then the same three on the FLIP setup, "
                       "whose WCS binds a Joint Origin of its own rather than sharing the first "
                       "setup's, which would be the same fixture twice. Then the hub's three "
                       "setups: the lathe's mill-turn machine assigned through the simulation "
                       "strip, its frame flipped onto world -Z so the flange sits in the chuck, "
                       "its zero taken from the MODEL's front face; the milling setup's stock "
                       "switched to the lathe's rest with the mode read back beside the one it "
                       "held; and the rotary setup's WCS bound to that computed Joint Origin"),
    "cam_edit_folders": ("organize the job the way a shop sheet reads - the six milling passes "
                         "into Milling and the three hole-making cycles into Drilling, each move "
                         "counted by the destination folder's own re-read"),
    "cam_reorder": "reorder the adaptive before the face op",
    "cam_activate_setup": "activate the setup",
    "cam_compare_operations": "compare the two operations",
    "cam_show_toolpath": ("list the job's own bulbs; then ONE choreography, which every CAM act "
                          "shows its toolpaths with: the subject framed once, every path off, then "
                          "each operation alone in that standing frame - shown with fit false, the "
                          "resolved name read back, hidden again - and every path off at the end. "
                          "No act refits the camera or leaves the paths on. Then ISOLATE, once, on "
                          "one operation of the finished job: the action that hides every other "
                          "path and keeps the read-backs, so an operation whose bulb would not go "
                          "dark is named in hide_failures - asserted absent"),
    "cam_generate": ("generate the toolpaths against the real part in the real fixture. The tool "
                     "takes no 'pump_seconds': CAM-7 confirms the kernel refuses to be pumped while "
                     "a generation runs, so completion is certified by the bounded cam_get_status "
                     "poll after this act, never by a sleep inside the call. Then one launch per "
                     "cameo setup - each act's launches taken back to back behind its writes, and "
                     "certified one setup at a time by the boundary poll, with the resolved node's "
                     "KIND read back beside the name asked for. The last CAM act relaunches every "
                     "setup the job's later edits left stale, taking either answer the tool gives "
                     "that scope - a launch, or the skipped one naming what needed none. On the "
                     "hub, one launch per setup behind that act's own writes: the lathe job, the "
                     "milling census, the two LONG families in an act whose poll budget is sized "
                     "to their measured 105 s and 71 s, and the rotary wrap"),
    "cam_inspect_toolpaths": ("verdict false with named ops before generation, scoped check, "
                              "bogus-scope refusal, an over-cap max_results clamped to the tool's "
                              "own row ceiling, verdict true after generation, include_suppressed "
                              "widening the tally while the verdict's own set is reported apart "
                              "from it, a document-level answer taken with an empty setup present, "
                              "and the FILTERING measured against a real suppression - the tally "
                              "one operation shorter with the excluded count naming what it left "
                              "out, then the same read widened to count it in its own bucket"),
    "cam_get_status": ("poll the generation to completion (empty toolpaths fail); the read taken "
                       "on the finished extension job, whose per-state tally and health counts "
                       "describe the scope named beside them; and the LAST CAM step - the tree the "
                       "run leaves a watcher, read off the document and off the setup whose "
                       "operations arrived tool-less: nothing out of date, errored or suppressed. "
                       "And the hub's lathe job read TURNED CLEAN - every cycle finished with no "
                       "warning and none of them cutting air, which is the bar a solid of "
                       "revolution turned from a cylinder of stock has to meet"),
    "cam_post": ("post the setup's NC program to disk; then the LAST thing the sweep does - ONE "
                 "program over the part's TWO setups, the job and its flip, with the setups asked "
                 "for beside the program's own stored operations re-read after the post and every "
                 "file stat'd on disk, the reconfigure down to one setup refused naming every "
                 "input to omit, and the as-is re-post that changes nothing. The rail program is "
                 "the other multi-setup post, and the one that meets the 5-axis refusal: it is "
                 "REFUSED while the swarf toolpath is active, naming the machine configuration a "
                 "5-axis simultaneous toolpath needs, and posts once that operation is parked. "
                 "Then the LOCATION oracle: the hub's flange-wall contour posted through the "
                 "shipped dump post, whose file carries every motion event - the cuts read back "
                 "against the stock box the dump states and down on the rim they were aimed at, "
                 "and the simultaneous job through the same post, where the tool axis is judged"),
    "cam_generate_setup_sheet": "write the machinist setup sheet with the file-landed gate",
    "cam_set_nc_comment": "stamp the NC program comment",
    "cam_save_template": ("save the setup as a run-stamped local CAM template - the stamp is what "
                          "keeps two overlapping runs off one name, since this tool always writes "
                          "a NEW template"),
    "cam_apply_template": ("apply the run's own saved template to a second setup by NAME; then "
                           "Fusion's shipped spotdrill/drill/counterbore hole bundle by the URL "
                           "the templates slice published for it, with the name passed too - the "
                           "tool's cross-check (the url must LOAD the template the name says) is "
                           "what makes the pair one measurement instead of two hopes. Both applies "
                           "publish a row per operation the SETUP gained, and the two are asserted "
                           "apart: the run's own template arrives TOOLED (tool_unselected empty, "
                           "'ready' true), while every operation of the shipped bundle arrives "
                           "tool-less - all of them named in tool_unselected, with 'ready' false, "
                           "and cam_edit_operation giving each of them a cutter and "
                           "cam_select_geometry the bores it cycles, so the setup ends valid"),
    "cam_delete_template": ("take the run's own template back out of the Local library: the "
                            "confirm_name mismatch refused while it still exists, then the delete "
                            "proved by the library's asset walk and by nothing loading from the "
                            "deleted url, and the templates slice that listed it when it arrived "
                            "read back as no longer holding it"),
    "cam_delete": "delete a scratch operation; count diff",
    "design_export": ("export the machined part to STEP, then the whole design SPLIT per component "
                      "to STL with stl_binary read back off the options object the split path "
                      "created for each file - the branch that would otherwise report a clean "
                      "export while dropping the format knob; then every remaining format one file "
                      "at a time, each measured ON DISK rather than trusted to the API's success "
                      "bool, USD publishing the .usdz path Fusion appended for itself, STL with "
                      "its units baked in, and a sketch out through the 2D DXF branch"),
    "doc_insert_import": ("re-import that STEP from disk into the live design; then the DXF back "
                          "onto a plane as sketches, an SVG into a sketch made for it, and IGES / "
                          "SMT / f3d as solids - each with the format named explicitly, "
                          "which is what makes the last row (a format contradicting its file's "
                          "extension) a refusal instead of a silent mis-read"),
    "design_set_name": ("rename a cameo component and re-find it by the name that landed, rename a "
                        "cameo occurrence with its instance name following, give a twin body the "
                        "name its sibling holds so the deduped '(1)' is what gets published, and "
                        "rename a MESH body - the kind reads 'mesh' and a fresh read of the "
                        "component's meshes carries the new name; the empty target and the root "
                        "component refused"),
    "design_get": ("the timeline read once the hub's last feature has landed - nothing in the "
                   "design computed into an error or a warning; then the final design read: the "
                   "whole cast, stamped with the DOCUMENT it was read from "
                   "(sys_find_tool, which never touches the design, carries no such stamp), the "
                   "timeline slice the 'name@index' feature form is addressed from, plus the "
                   "material/appearance catalog at both zoom levels - the library census and one "
                   "paged library; an unloaded library name refused"),
    "drawing_create": ("meet every guard the drawing generator sits behind, each settled before the "
                       "tool reaches for a cloud source: the shaded style with no member to set, "
                       "the two centre annotations with no enum family on this build, a tangent-edge "
                       "value outside the Choice, manual creation with no template, and a sheet size "
                       "from the other standard - none of them creating anything, and the session "
                       "healthy afterwards. Then the CREATION path, on the cloud tier: an A3 ISO "
                       "drawing generated from the saved source, its own name and lineage URN read "
                       "off the DataFile that landed"),
    "doc_get": ("read the document identity before discarding; and, on the cloud tier, which "
                "document the session is on before each write acts on it, plus the version history "
                "the save/milestone/restore beats built"),
    "doc_close": ("discard the document on camera - clean teardown; and, on the cloud tier, close "
                  "every cloud document this run opened, by lineage URN, before deleting it"),
    "sys_reload_addin": ("the last beat of the run, once the document is discarded: reload the "
                         "add-in, watch /health stop answering and answer again as this server, "
                         "then read the restarted registry back through sys_find_tool. It "
                         "restarts the server, so nothing can be dispatched after it - which is "
                         "why it is a post-run beat and not a row in an act"),
    # THE OPT-IN CLOUD TIER (ACT 11a-c). Every row below runs only where cloud_config names a hub,
    # and an unconfigured run reports each of these tools skipped(cloud_tier not entitled).
    "data_get": ("the hub this run is signed in to and the configured project listed in it - the "
                 "row that puts on the ledger WHICH hub the artifacts were made in; then the "
                 "uploaded file's own record (where it sits, and whether the cloud has finished "
                 "with it - what the drawing generator needs true of its source); and the two "
                 "read-backs standing apart from the deletes' own reports: the run folder addressed "
                 "DIRECTLY, where the project answers that it holds no such subfolder - a refusal a "
                 "budget cut cannot produce - and the configured folder's own file listing, scoped "
                 "to that folder and failing on any listing that did not fully read"),
    "data_create_folder": ("a run-stamped folder under the configured one and a move target inside "
                           "it, each re-listed under the parent asked for with NOTHING auto-created "
                           "on the way there - a run that had to invent the operator's folder is "
                           "addressing a project the config does not describe"),
    "data_upload_file": ("upload the run's marker PNG into that folder - a non-Fusion file, which "
                         "is the only kind that can come back down again - and mint the poll handle "
                         "the status read below asks with"),
    "data_get_upload_status": ("poll that handle to 'complete': transfer AND cloud processing "
                               "finished, with the lineage URN every later step addresses the file "
                               "by. Nothing downstream runs against a file that has not landed"),
    "data_move_file": ("move the landed file into the subfolder, with the parent RE-READ off the "
                       "re-resolved file - DataFile.move returns a bool, and the bool is not the "
                       "evidence"),
    "data_download_file": ("bring the same file back to local disk with its bytes stat'd there; and "
                           "meet the Fusion-native refusal on the saved design, which is what sends "
                           "a caller to design_export"),
    "data_delete_file": ("take every document this tier made back out by URN with confirm_name "
                         "matching, each delete read back - the referencing file first, since a "
                         "referenced one is refused rather than orphaned"),
    "data_delete_folder": ("meet the non-empty refusal naming the subtree a recursive wipe would "
                           "take, then delete the two folders once emptied, each with the census "
                           "read before the delete saying it took nothing else with it"),
    "doc_save_as": ("write the plate into the configured folder as this run's source, and the host "
                    "beside it - each with no name collision, since a second file of one name is a "
                    "fork this run could not then clean up by name"),
    "doc_save": ("version the source in place, twice: once after the parameter is added, and once "
                 "for the edit the drawing refresh is read against"),
    "doc_save_milestone": ("mark a named milestone on the source - the two INDEPENDENT read-backs "
                           "kept apart, a fresh fetch showing a NEW version number, and the mark on "
                           "it, which lags the version by seconds and is reported not asserted"),
    "doc_restore_version": ("promote the FIRST version, one the tip has moved past - promoting the "
                            "latest takes the tool's early return, with no promote call and no tip "
                            "either side to compare. The open session keeps its parameters through "
                            "it, and the save after it writes them as the new tip; a pending tip is "
                            "reported, never called a failure"),
    "doc_copy": ("copy the source cloud-to-cloud into the same folder under its own name, the copy "
                 "read back off the created DataFile on a DIFFERENT lineage from its source"),
    "doc_insert_occurrence": ("insert the saved source into the host as an XREF - 'is_reference' is "
                              "what gives doc_update_xref something to walk"),
    "doc_update_xref": ("walk the host's one reference with only_out_of_date true, THREE times: "
                        "straight after the insert, where the bucket is reported (measured: a "
                        "just-inserted xref read out of date, bound a version behind the source's "
                        "stream); again immediately, where nothing is stale and it is SKIPPED; and "
                        "once more after the source is edited and saved, where it is UPDATED. A row "
                        "in each bucket is what makes the partition a measurement - only_out_of_date "
                        "false can never fill 'skipped'"),
    "doc_open": ("open the copy and then the generated drawing by lineage URN, each named back with "
                 "the URN it resolved; the switch is async, so the doc_get after it is what says "
                 "the session is on it"),
    "doc_activate": ("the overture opens a SECOND unsaved scratch document beside the story one and "
                     "switches both ways between them by the 'open:N' index doc_get published - the "
                     "address an UNSAVED document has instead of a URN, and the only one that "
                     "reaches it when two documents share the name 'Untitled'; the cloud tier then "
                     "switches between the source, the host, the copy and the drawing by lineage "
                     "URN and comes home the same way"),
    "drawing_get": ("the sheet read every write and both exports are taken behind: export_index "
                    "1-based and contiguous (the numbering drawing_export's sheet_range takes, "
                    "obtainable nowhere else) with exactly one sheet active"),
    "drawing_update": ("a drawing generated moments ago reports itself already up to date; then, "
                       "after the source is edited and saved, the refresh that ran with the count "
                       "of references stale before it - the number separating it from a no-op"),
    "drawing_dimension": "dimension the generated view with the baseline strategy",
    "drawing_insert_image": ("place the run's marker PNG on the sheet, with "
                             "'position_bounds_checked' saying the anchor was compared against the "
                             "sheet BEFORE anything was placed - an image cannot be read back or "
                             "moved afterwards, so the check having run is the read-back"),
    "drawing_add_sketch": "draw a line chain, a rectangle and a circle on the sheet - four curves",
    "drawing_edit_sheet": ("add a named sheet with the count read either side; it comes after every "
                           "beat needing the generated views, since an add makes the NEW sheet "
                           "active"),
    "drawing_export": "write the drawing to PDF and to DXF, each measured by its bytes on disk",
}

# Tools deliberately not swept unattended, each with its reason (the ledger's skipped rows). This is
# the policy-excluded bucket; PENDING (below) is the separate "not scripted yet" bucket - the ledger
# keeps that distinction honest.
EXCLUDED = {
    "sys_execute_script": ("gated off by design; the sweep proves the typed surface suffices - and "
                           "with it the beat for its DRAWING-document error tail (a raise inside a "
                           "drawing ends with 'Re-read the sheets before assuming this call changed "
                           "nothing.', a design one does not), which would need this tool driven "
                           "against two document kinds"),
    "sys_reload_addin": ("restarts the server, so no STEP can follow it - the run drives it as a "
                         "post-run beat instead (reload_smoke), and this row is what stands when "
                         "that beat could not confirm the restart: the call, the /health "
                         "down-then-up watch or the sys_find_tool smoke did not come back"),
    "sys_request_selection": "waits on a human pick (user-present tier)",
    "design_set_mode": "irreversible parametric->direct conversion; not run unattended",
    "design_configure": "configuration table needs a SAVED document (a DataFile to carry it); opt-in tier - the appearance/material columns need that document too, and a body's material reads back only after the geometry catches up with the activation",
    # The rest of the cloud tier. The tools that write into an operator's hub are DRIVEN by ACT 11a-c
    # behind the cloud_tier capability, so an unconfigured run reports them skipped(cloud_tier not
    # entitled) rather than listed here; these two are not driven even with a config.
    "data_create_project": ("mints a real project in the operator's hub - the config names an "
                            "EXISTING project, and a project is not this tier's to create"),
    "data_switch_hub": ("closes every open document, the story document among them - the cloud_tier "
                        "probe refuses a config naming a hub other than the active one instead of "
                        "switching to it"),
}

# Registered tools NOT yet scripted into STEPS - the honest "todo" ledger. SHRINK-ONLY: scripting a
# tool moves it out of here into STEPS. test_tool_verify_complete.py enforces that every
# registered tool is covered, excluded, or listed here, so a NEWLY added tool can't decay coverage
# silently - it fails the gate until someone scripts it, excuses it, or adds it here deliberately.
PENDING = frozenset({"drawing_get_status"})
