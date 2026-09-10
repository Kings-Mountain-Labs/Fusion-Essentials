# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""ACT rows: the milling job on the real part in the real fixture, and its deliverables.

The setup, the tools, the operations and the generation of a four-operation job, then what the job
produces - the posted NC, the setup sheet, the template - each with the teardown of the two assets
that live outside the discarded document. `poll_generation` is the post-act hook run() fires
between the two acts: a bounded read of the generation state, which an empty toolpath fails.

Then the Machining Extension's own strategies, which machine geometry the bracket has none of: a
drafted frustum cameo carries a swarf rail pair, a deburr chain and a face-driven geodesic, and the
two setups one NC program spans. Their non-empty oracle is each operation's own machining time -
hasToolpath reads TRUE on an empty swarf toolpath, so it cannot answer that question.
"""

import re
import time

from verify_core import (
    EXPORT_DIR, MACHINE_NAME, NOTE_MAX, Parked, TEMPLATE_NAME, _RECALL, _box, _ctx_get, _dwell,
    _extruded, _face_up_at, _fg, _fgn, _imported, _joint_origin_computed, _made_component,
    _matched, _measured, _near, _needs, _num, _param_read, _recall, _refused, _watch, facade)

# THE NAMES THE STORY BUILDS UNDER, in one place for the acts that have to agree on them: the
# modelling acts create the part and the parameter that drives it, the vise act creates the billet
# and the three fixture components, and this act names the setup that machines them. Every row
# addresses a setup through the constant, so one spelling reaches the whole program.
PART_COMP = "Bracket"          # the machined part - the setup's model, and what the silhouette cuts
PART_DRIVER = "PartLen"        # the user parameter the part's LENGTH follows - what the CAM stock is sized against
STOCK_COMP = "STOCK"           # the billet: the part's box plus its allowance
VISE_BASE = "ViseBase"         # the fixture bodies the setup is told about
JAW_FIXED = "JawFixed"
JAW_MOVING = "JawMoving"
CAM_SETUP = "DemoSetup"        # the milling setup the job, the post and the template ride on
FLIP_SETUP = "FlipSetup"       # the second setup: the part turned over, machined from underneath
FLIP_WCS = "FlipWCS"           # that setup's own origin, a Joint Origin at the part's own centre

# The capability the rail/surface strategies declare. A base licence is not entitled to generate
# them, so the acts and steps that create them ride the tier rather than reddening the run.
MACHINING_EXTENSION = "machining_extension"

# THE DOCUMENT TOOL LIBRARY, in the order the adds land: every operation below selects its cutter
# by INDEX, so this order is the contract between the add rows and the create rows.
_FACE_MILL, _FLAT_MILL, _BALL_MILL, _DRILL, _CHAMFER_MILL = 0, 1, 2, 3, 4
_TURNING, _CENTER_DRILL = 5, 6

# The operations this job is built from, named explicitly where a later row addresses them (the
# platform picks its own default name, so a literal for one of those would be a guess).
_POCKET_OP = "PocketRough"     # the 2D offset roughing that opens the radiused pocket
_SPOT_OP = "SpotDrill"         # the centre drill spotting the through bores before they are opened
_BORE_OP = "BoreThrough"       # the bore that finishes them to size
_CHAMFER_OP = "EdgeChamfer"    # the 2D chamfer that breaks the stepped top's edges
_BOSS_OP = "BossFinish"        # the base-licence finishing pass round the boss
_BOSS3D_OP = "BossSteep"       # the Machining Extension's own 3D finish on the same feature
_FLIP_FACE_OP = "FlipFace"     # the second setup: facing the underside
_FLIP_BACK_OP = "FlipCbores"   # and the contour round the counterbore backsides
_ENGRAVE_OP = "SketchEngrave"  # the engraving, driven by the scratch sketch
_PROBE_OP = "ProbeStepTop"     # the Probe WCS cycle that touches off the stepped top

# The shipped hole-drilling template one act applies by its folder-position url: a spot drill, a
# drill and a counterbore in one bundle, which is the counterbored mounting pattern's whole cycle.
_FUSION_HOLE_TEMPLATE = "Spotdrill, Drill, & Counterbore Hole"
# The ctx label for that bundle's operations - the platform names them, so every row that addresses
# one reads the name the apply published rather than a literal.
_TOOL_LESS = "the shipped bundle's operations by cycle"
# The three cycles it carries. The platform names each applied operation '<cycle> - <template>', so
# the cycle word is what pairs a cutter with its operation whatever ORDER the apply lands them in.
_CYCLES = ("Spotdrill", "Drill", "Counterbore")


def _cycle_of(name):
    """The bundle cycle an applied operation's name begins with, or None."""
    head = str(name or "").split(" - ")[0].strip()
    return head if head in _CYCLES else None


def _bundle_ops(payload):
    """{cycle: {name, strategy}} off a cam_apply_template payload - the pairing every later row
    reaches the bundle's operations through, so none of them is addressed by list position."""
    found = {}
    for r in (payload.get("operations") or []):
        cycle = _cycle_of(r.get("name"))
        if cycle:
            found[cycle] = {"name": r.get("name"), "strategy": r.get("strategy")}
    return found


def _op_created(setup, strategy):
    """A created CAM operation. 'operation' is a read-back: op.name off the operation the platform
    added, read after the setup's own count went up. 'generation_mode_note' is a disclosure the tool
    publishes only when the input's generationMode read back something other than the assigned
    SkipGeneration, so its ABSENCE is the pass. 'setup', 'strategy' and the constant-False
    'generation_started' are the call's own arguments echoed back - asserted because a payload that
    mismatches the request is a wrong payload."""
    def check(p):
        return _measured(f"a '{strategy}' operation created in '{setup}'",
                         {"operation": p.get("operation"), "setup": p.get("setup"),
                          "strategy": p.get("strategy"),
                          "generation_started": p.get("generation_started"),
                          "generation_mode_note": p.get("generation_mode_note")},
                         bool(p.get("operation")) and p.get("setup") == setup
                         and p.get("strategy") == strategy
                         and p.get("generation_started") is False
                         and "generation_mode_note" not in p)
    return check


def _toolpath_shown(action, key, fit=False):
    """One operation's toolpath displayed: the operation NAME the tool resolved (compared with the
    name the PLATFORM published when the op was created - a literal here would be a guess) and
    whether the camera fit applied ('fit' reads true only after the viewport call returned)."""
    def check(p):
        want = _RECALL.get(key)
        return _measured(f"{action} the operation created as {want!r}",
                         {"action": p.get("action"), "operation": p.get("operation"),
                          "fit": p.get("fit")},
                         p.get("action") == action and want is not None
                         and p.get("operation") == want and p.get("fit") is fit)
    return check


def _bulb(action, name, fit=None):
    """cam_show_toolpath(show|hide): the operation NAME the tool resolved. 'fit' is asserted FALSE on
    a show - the subject is framed once and the camera holds, so a fit here would be a camera move."""
    def check(p):
        return _measured(f"'{name}' {'shown' if action == 'show' else 'hidden'}, "
                         "the standing frame kept",
                         {"action": p.get("action"), "operation": p.get("operation"),
                          "fit": p.get("fit"), "setup": p.get("setup")},
                         p.get("action") == action and p.get("operation") == name
                         and p.get("fit") is fit)
    return check


def _hide_all():
    """Every generated path off: hidden_count counts the bulbs that read back false, and a bulb that
    did not take is reported as a toggle_failure rather than counted."""
    return ("cam_show_toolpath", {"action": "hide_all"},
            lambda p: p["action"] == "hide_all" and p["hidden_count"] >= 1
            and "toggle_failures" not in p, None)


def _isolated(name):
    """cam_show_toolpath(isolate): the one action that promises nothing ELSE is drawn, so the
    mass-hide's own read-backs are the assertion - 'hide_failures' names every operation that would
    not go dark (matched by operationId, so the target's own refused hide is not among them), and
    its ABSENCE is what says the isolate really isolated."""
    def check(p):
        return _measured(f"'{name}' alone on screen, every other path dark",
                         {"action": p.get("action"), "operation": p.get("operation"),
                          "fit": p.get("fit"), "setup": p.get("setup"),
                          "hide_failures": p.get("hide_failures"),
                          "has_toolpath": p.get("has_toolpath")},
                         p.get("action") == "isolate" and p.get("operation") == name
                         and p.get("fit") is False and "hide_failures" not in p
                         and p.get("has_toolpath") is not False)
    return check


def _one_bulb(entry, action, fit):
    """(args, predicate) for one bulb of a reveal. An entry is the name the act CHOSE, or a
    (ctx key, label) pair for a name the PLATFORM picked at create time - a literal for one of
    those would be a guess."""
    if isinstance(entry, str):
        return {"action": action, "operation": entry}, _bulb(action, entry, fit=fit)
    key, label = entry
    return ((lambda c, k=key, w=label: {"action": action, "operation": _ctx_get(c, k, w)}),
            _toolpath_shown(action, key, fit=fit))


def _reveal(names):
    """The choreography EVERY CAM act shows its toolpaths with, inside the frame the act already
    set: every path off, then each operation alone - on, held, off - and every path off again at the
    end. A generated toolpath nobody shows is a beat the watcher never sees."""
    rows = [_hide_all()]
    for n in names:
        show_args, show_check = _one_bulb(n, "show", False)
        hide_args, hide_check = _one_bulb(n, "hide", None)
        rows += [("cam_show_toolpath", show_args, show_check, None),
                 _dwell(1.0),
                 ("cam_show_toolpath", hide_args, hide_check, None)]
    return rows + [_hide_all()]


def _launched_on(setup, skip_valid=False):
    """cam_generate over one setup: 'target' is the RESOLVED node's kind beside the name asked for,
    so it says the name reached a setup rather than an operation of the same name, 'skip_valid'
    echoes the step's own choice, and 'handle' is what the act-boundary poll reads."""
    def check(p):
        return _measured(f"generation launched over setup '{setup}' (skip_valid {skip_valid})",
                         {"launched": p.get("launched"), "target": p.get("target"),
                          "skip_valid": p.get("skip_valid"), "handle": p.get("handle")},
                         p.get("launched") is True and p.get("target") == f"setup '{setup}'"
                         and p.get("skip_valid") is skip_valid and bool(p.get("handle")))
    return check


def _settled(scope):
    """cam_get_status as the SETTLE before a compare: cam_compare_operations refuses while a named
    operation still has generating to do, so the beat asserts the scope has none in flight. Not
    _all_current - this runs before the act's own cam_generate, so stale operations are expected."""
    def check(p):
        live = p.get("live_states") or {}
        return _measured(f"nothing in {scope} has generating left to do",
                         {"completed": p.get("completed"), "live_states": live},
                         p.get("completed") is True and _num(live.get("total")))
    return check


def _relaunched(setup):
    """cam_generate(skip_valid=true) over a setup the job's later edits may have left stale: a
    launch carrying the count it covers, or the ALREADY-VALID skip. The skip's reason has to be that
    one - a scope holding no operations at all skips too, saying 'nothing to generate', and that
    answer over a setup this run filled would mean the name reached an empty one."""
    def check(p):
        reason = p.get("reason") or ""
        covered = p.get("operations_to_generate")
        return _measured(f"'{setup}' relaunched, or already valid throughout",
                         {"launched": p.get("launched"), "target": p.get("target"),
                          "skipped": p.get("skipped"), "reason": reason,
                          "operations_to_generate": covered},
                         p.get("target") == f"setup '{setup}'"
                         and ((p.get("launched") is True and _num(covered) and covered >= 1)
                              or (p.get("skipped") is True and "already valid" in reason)))
    return check


def _probe_applied(count):
    """cam_select_geometry(selection='probe'): the face count the OPERATION holds after the
    selection, beside the PARAMETER it landed on - 'probe_selection' is what says the probing input
    took the faces rather than some other input of the operation, which the count cannot."""
    def check(p):
        return _measured(f"{count} face(s) on the probing input",
                         {"selections": p.get("selections"),
                          "selection_param": p.get("selection_param")},
                         p.get("selections") == count
                         and p.get("selection_param") == "probe_selection")
    return check


def _op_valid(setup, name):
    """cam_get(include=['operations']) read after the act boundary certified the generation: ONE
    operation's row, valid, carrying no blocker, and NOT flagged empty_toolpath - the flag the
    slice puts on a row that cuts nothing, which a state of 'valid' does not exclude."""
    def check(p):
        recs = ((p.get("operations") or {}).get("setups") or [])
        rec = next((r for r in recs if r.get("setup") == setup), None)
        row = next((r for r in ((rec or {}).get("operations") or [])
                    if r.get("name") == name), None)
        return _measured(f"'{name}' reads valid in '{setup}', with a toolpath that is not empty",
                         {"row": row},
                         bool(row) and row.get("state") == "valid"
                         and row.get("blocked_by") == [] and "empty_toolpath" not in row)
    return check


def _op_named(setup, strategy, name):
    """A created operation that landed under the name it ASKED for. 'operation' is a read-back:
    op.name off the operation the platform added, so a deduped name reads back a different string
    here. 'generation_mode_note' is a disclosure the tool publishes only when the input's
    generationMode read back something other than the assigned SkipGeneration, so its ABSENCE is the
    pass. 'setup' and 'strategy' are the call's own arguments echoed - a payload that mismatches the
    request is a wrong payload."""
    def check(p):
        return _measured(f"a '{strategy}' operation created as {name!r} in '{setup}'",
                         {"operation": p.get("operation"), "setup": p.get("setup"),
                          "strategy": p.get("strategy"),
                          "generation_mode_note": p.get("generation_mode_note")},
                         p.get("operation") == name and p.get("setup") == setup
                         and p.get("strategy") == strategy
                         and "generation_mode_note" not in p)
    return check


def _paths_address_every_operation(setup):
    """cam_get(include=['operations']): every row's 'path' IS its address - the setup, the folder it
    sits in where it sits in one, then its own name - and no two rows share a name.

    Both halves are asserted because either alone admits the ambiguity: a path that dropped its
    folder segment would address two same-named rows identically, and two rows sharing a name would
    force the ordinal '<name>#<n>' form no read here publishes."""
    def check(p):
        rows = [r for s in ((p.get("operations") or {}).get("setups") or [])
                if s.get("setup") == setup for r in (s.get("operations") or [])]
        mismatched = []
        for r in rows:
            want = " / ".join([setup] + ([r["folder"]] if r.get("folder") else []) + [r["name"]])
            if r.get("path") != want:
                mismatched.append({"name": r.get("name"), "path": r.get("path"), "want": want})
        names = [r.get("name") for r in rows]
        return _measured(f"every operation of '{setup}' is addressed by its own path",
                         {"operations": len(rows), "mismatched": mismatched[:3],
                          "foldered": sum(1 for r in rows if r.get("folder")),
                          "duplicate_names": sorted({n for n in names if names.count(n) > 1})},
                         bool(rows) and not mismatched and len(set(names)) == len(names))
    return check


def _machining_capabilities(p):
    """workspace_orient's entitlement block: one observed_generation entry per sentinel strategy and
    nothing else, each a flag that ANSWERED - null there is 'the probe could not read it', which is
    not an entitlement. Which way the flags read is this installation's licence to state, not this
    beat's, so the values are reported rather than asserted - the capability tier is what acts on
    them."""
    obs = (p.get("machining_capabilities") or {}).get("observed_generation") or {}
    return _measured("machining capability sentinels",
                     {"observed_generation": obs},
                     set(obs) == {"steep_and_shallow", "multiaxis_finishing", "swarf",
                                  "probe_geometry"}
                     and all(isinstance(v, bool) for v in obs.values()))


def _types_offered(*names):
    """cam_edit_tools(action='list_types'): the from_type vocabulary the bundled sample libraries
    carry, read against the spellings the add rows below clone by. A type that is not offered is
    reported BY NAME, so one red names the spelling instead of the add that used it."""
    def check(p):
        offered = p.get("types") or []
        missing = [n for n in names if n not in offered]
        return _measured(f"the from_type vocabulary offers each of {len(names)} named types",
                         {"type_count": p.get("type_count"), "missing": missing},
                         not missing and (p.get("type_count") or 0) >= 10)
    return check


def _offers(setup, *names):
    """cam_get(include=['strategies']) on one setup, read twice over: the entitlement tallies
    PARTITION the vocabulary - allowed plus blocked plus the unreadable flags (a key present only
    when there are any) account for every row compatibleStrategies offered - and every strategy
    NAMED here is in that vocabulary reading allowed. The counts themselves differ by licence, so
    the partition is what is asserted and the numbers ride the evidence; a name that is missing or
    blocked is reported as such, so one red names the strategy rather than the create row below
    that used it. A setup that did not report its list carries every tally as null, so there is no
    partition to compute - a red observation, not an arithmetic error."""
    def check(p):
        rows = ((p.get("strategies") or {}).get("setups") or [])
        row = next((r for r in rows if r.get("setup") == setup), None)
        offered = {r.get("name"): r.get("allowed") for r in ((row or {}).get("strategies") or [])}
        missing = [n for n in names if n not in offered]
        blocked = [n for n in names if n not in missing and offered.get(n) is not True]
        label = (f"strategy tallies partition the vocabulary of '{setup}'"
                 + (f", which offers {', '.join(names)} allowed" if names else ""))
        got = {"tallies": row and {k: row.get(k) for k in
                                   ("strategies_read", "strategy_count", "allowed_count",
                                    "blocked_count", "unreadable_count")},
               "missing": missing, "blocked": blocked}
        if row is None or row.get("strategies_read") is False:
            return _measured(label, got, False)
        parts = ((row.get("allowed_count") or 0) + (row.get("blocked_count") or 0)
                 + (row.get("unreadable_count") or 0))
        return _measured(label, got,
                         not missing and not blocked and _num(row.get("strategy_count"))
                         and row["strategy_count"] > 0 and row["strategy_count"] == parts)
    return check


def _face_down_at(x, y, z, tol=0.5):
    """find_geometry(kind='planar_face') for the UNDERSIDE: the same measure as _face_up_at with
    the normal read the other way. The flip setup machines the face whose material is ABOVE it, and
    'nearest_to' answers with the nearest face whether or not it is that one - on a part whose top
    and bottom share a centroid in x and y, the normal is the only thing that tells them apart."""
    def check(p):
        ms = p.get("matches") or []
        m = ms[0] if ms else {}
        pos, nrm = m.get("position"), m.get("normal")
        return _measured(f"one down-facing planar face centred near {[x, y, z]}",
                         {"count": len(ms), "position": pos, "normal": nrm, "kind": m.get("kind")},
                         len(ms) == 1 and m.get("kind") == "planar_face"
                         and isinstance(pos, list) and len(pos) == 3
                         and all(_num(v) and abs(v - w) < tol for v, w in zip(pos, (x, y, z)))
                         and isinstance(nrm, list) and len(nrm) == 3 and nrm[2] < -0.999)
    return check


def _selected(count, on=None):
    """cam_select_geometry: 'selections' is the count the OPERATION holds after the selection was
    applied - re-read off the operation, never the number of references handed in (the tool errors
    on a call that leaves zero). 'on' additionally pins WHICH operation the geometry landed on,
    against the name the apply published for that bundle cycle."""
    def check(p):
        want = (_RECALL.get("tmpl_ops") or {}).get(on) or {} if on else {}
        return _measured(f"{count} selection(s) held by the operation"
                         + (f" the apply named for {on}" if on else ""),
                         {"selections": p.get("selections"), "resolved": p.get("resolved"),
                          "operation": p.get("operation"), "expected": want},
                         p.get("selections") == count
                         and (on is None
                              or (bool(want) and p.get("operation") == want.get("name"))))
    return check


def _setup_bodies(**counts):
    """cam_edit_setup(stock=/fixtures=/models=): each '<arg>_set' is that collection's own count
    RE-READ off the Setup after the assignment - the tool errors when the re-read disagrees with
    what it was handed, so the number here is what the setup holds."""
    def check(p):
        return _measured(f"the setup holds {counts}",
                         {f"{arg}_set": p.get(f"{arg}_set") for arg in counts},
                         all(p.get(f"{arg}_set") == n for arg, n in counts.items()))
    return check


def _tool_param_landed(name, fragment):
    """cam_edit_tools(action='edit'): the tool parameter's own read-back - 'after' is the expression
    the TOOL holds once the write is committed, never the expression that was sent."""
    def check(p):
        rows = p.get("changed") or []
        row = next((r for r in rows if r.get("name") == name), None)
        return _measured(f"tool parameter '{name}' reads back carrying '{fragment}'",
                         {"edited": p.get("edited"), "changed": rows},
                         p.get("edited") == 1 and bool(row) and fragment in str(row.get("after")))
    return check


# ACT 10a: CAM on the REAL part in the REAL fixture - job built and generated. The scratch-stock
# rows remain as this act's fallback, so the CAM family stays covered when the story world
# could not build.
_CAM_STORY = [
    # the machining region: the part seated in the vise, which is what every CAM beat acts on.
    _watch([STOCK_COMP + ":1"]),
    # a scratch sketch inside the part's footprint, drawn while Design is still the active
    # workspace: the geometry the 'sketch' selection takes, which is a WHOLE sketch (SketchSelection
    # accepts sketches, not their curves and not their profiles).
    ("sketch_create", {"plane": "xy", "name": "CamContourSketch"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 0, "cy": 0, "radius": 10}],
                             "sketch_name": "CamContourSketch"}, "ok", None),
    ("view_switch_workspace", {"workspace": "manufacture"}, "ok", None),
    # CAM is looked at, not sketched in: drop the sketch clutter design-wide for the whole
    # machining movement. The FOLDER bulb, so no entity's own visibility is disturbed and a
    # sketch the CAM selection already holds by name is unaffected. Put back in the FINALE.
    ("view_set", {"action": "display", "categories": ["sketches"], "visible": False},
     lambda p: p.get("visible") is False, None),
    ("cam_get", {}, "ok", None),
    # THE ENTITLEMENT READ, before any CAM structure exists: four sentinel strategies, each
    # answering its own isGenerationAllowed flag. The flags need no CAM product and no setup - the
    # read itself needs an active document - and the capability probe takes this same read.
    ("workspace_orient", {}, _machining_capabilities, None),
    # THE SHOP'S TOOL SET, sized for this part: a 50 mm face mill for the top, a 10 mm flat mill
    # for the roughing, the 2D walls and the bore (it has to fit INSIDE the 12 mm BoreDia hole it
    # finishes), a 6 mm ball for the boss finish, a 6 mm drill for the MountDia bores, and a
    # chamfer mill for the broken edges.
    ("cam_edit_tools", {"action": "add", "scope": "document",
                        "add_tools": [{"from_type": "face mill", "diameter": "50 mm"},
                                      {"from_type": "flat end mill", "diameter": "10 mm"},
                                      {"from_type": "ball end mill", "diameter": "6 mm"},
                                      {"from_type": "drill", "diameter": "6 mm"},
                                      {"from_type": "chamfer mill"}]},
     lambda p: p.get("added") == 5 and _num(p.get("tool_count")) and p["tool_count"] >= 5, None),
    # sample clones keep the sample's tool number; two clones can collide and the post refuses
    # ("Different tools have the same tool number") - assign distinct numbers explicitly. Every
    # tool an operation below cuts with gets one, read back off the tool it was written to.
    ("cam_edit_tools", {"action": "edit", "scope": "document", "tool": _FACE_MILL,
                        "parameters": {"tool_number": "1"}},
     _tool_param_landed("tool_number", "1"), None),
    ("cam_edit_tools", {"action": "edit", "scope": "document", "tool": _FLAT_MILL,
                        "parameters": {"tool_number": "2"}},
     _tool_param_landed("tool_number", "2"), None),
    ("cam_edit_tools", {"action": "edit", "scope": "document", "tool": _BALL_MILL,
                        "parameters": {"tool_number": "3"}},
     _tool_param_landed("tool_number", "3"), None),
    ("cam_edit_tools", {"action": "edit", "scope": "document", "tool": _DRILL,
                        "parameters": {"tool_number": "4"}},
     _tool_param_landed("tool_number", "4"), None),
    ("cam_edit_tools", {"action": "edit", "scope": "document", "tool": _CHAMFER_MILL,
                        "parameters": {"tool_number": "6"}},
     _tool_param_landed("tool_number", "6"), None),
    # THE VOCABULARY CENSUS, read before the two remaining adds: it comes from the sample libraries
    # themselves (the spread runs well past ten kinds, and center drill lives only in Hole Making
    # Tools (Inch)), and it names every from_type spelling this act clones by - so a spelling this
    # installation does not carry reds HERE, naming it, rather than inside an add.
    # Then the preset round trip, with read-back, unit, and refusal gates.
    ("cam_edit_tools", {"action": "list_types", "scope": "document"},
     _types_offered("face mill", "flat end mill", "ball end mill", "drill", "chamfer mill",
                    "center drill", "turning general"), None),
    ("cam_edit_tools", {"action": "add", "scope": "document",
                        "add_tools": [{"from_type": "turning general"},
                                      {"from_type": "center drill"}]},
     lambda p: p.get("added") == 2 and _num(p.get("tool_count")) and p["tool_count"] >= 7, None),
    ("cam_edit_tools", {"action": "edit", "scope": "document", "tool": _CENTER_DRILL,
                        "parameters": {"tool_number": "5"}},
     _tool_param_landed("tool_number", "5"), None),
    # a turning-general preset carries surface speed, not spindle speed - the refusal names what
    # the preset actually has instead of applying nothing.
    ("cam_edit_tools", {"action": "add_preset", "scope": "document", "tool": _TURNING,
                        "preset": {"name": "SweepTurn", "spindle_speed": 400}}, "refused", None),
    ("cam_edit_tools", {"action": "add_preset", "scope": "document", "tool": _FLAT_MILL,
                        "preset": {"name": "SweepMM", "feed": 900, "spindle_speed": 12000}},
     lambda p: "SweepMM" in p["presets"], None),
    # a units-carrying expression is stored verbatim and evaluated (35in/min -> 889 mm/min).
    ("cam_edit_tools", {"action": "add_preset", "scope": "document", "tool": _FLAT_MILL,
                        "preset": {"name": "Sweep35", "feed": "35in/min"}},
     lambda p: "Sweep35" in p["presets"], None),
    # a numeric-leading expression that fails evaluation is refused and rolled back, never a
    # silent zero.
    ("cam_edit_tools", {"action": "add_preset", "scope": "document", "tool": _FLAT_MILL,
                        "preset": {"name": "SweepBad", "spindle_speed": "900 * NoSuchParamXyz"}},
     "refused", None),
    ("cam_edit_tools", {"action": "remove_preset", "scope": "document", "tool": _FLAT_MILL,
                        "preset": {"name": "SweepMM"}},
     lambda p: "SweepMM" not in p["presets"] and isinstance(p.get("removed_index"), int), None),
    ("cam_edit_tools", {"action": "remove_preset", "scope": "document", "tool": _FLAT_MILL,
                        "preset": {"name": "Sweep35"}},
     lambda p: "Sweep35" not in p["presets"], None),
    # THE READ SIDE of the same library: the summary census, the same census NARROWED by tool type
    # (the filter has to actually drop rows, not just be echoed back), and one tool's full parameter
    # list - the deeper read the summary points at.
    ("cam_edit_tools", {"action": "list", "scope": "document"},
     lambda p: p.get("tool_count", 0) >= 4 and "filtered_by_type" not in p,
     ("doc_tool_count", _recall("doc_tool_count", lambda p: p["tool_count"]))),
    ("cam_edit_tools", {"action": "list", "scope": "document", "tool_type": "drill"},
     lambda p: p.get("filtered_by_type") == "drill" and p.get("tool_count", 0) >= 1
     and all("drill" in str(t.get("type") or "").lower() for t in p["tools"]), None),
    ("cam_edit_tools", {"action": "parameters", "scope": "document", "tool": 0},
     lambda p: p.get("tool") == 0 and p.get("parameter_count", 0) > 0
     and all(r.get("name") for r in p["parameters"]), None),
    # the LOCAL scope with no 'library' named lists the libraries THERE, not tools - the same action
    # answering a different question because the scope changed.
    ("cam_edit_tools", {"action": "list", "scope": "local"},
     lambda p: p.get("scope") == "local" and isinstance(p.get("libraries"), list), None),
    # the document library is the document's own and cannot host a NEW one - refused by name rather
    # than quietly creating it somewhere else.
    ("cam_edit_tools", {"action": "create_library", "scope": "document", "library": "SweepLib"},
     "refused", None),
    # one more tool added and taken straight back out. 'remove' renumbers everything after the index
    # it takes, so it takes the LAST one - behind every tool the operations below select by number -
    # and the library's own count is what says the removal landed.
    ("cam_edit_tools", {"action": "add", "scope": "document",
                        "add_tools": [{"from_type": "ball end mill"}]}, "ok", None),
    ("cam_edit_tools", lambda c: {"action": "remove", "scope": "document",
                                  "remove_indices": [_ctx_get(c, "doc_tool_count",
                                                              "the document tool count")]},
     lambda p: p.get("removed") == 1, None),
    ("cam_edit_tools", lambda c: {"action": "list", "scope": "document"},
     lambda p: p.get("tool_count") == _RECALL.get("doc_tool_count"), None),
    # the setup lands under the name it was asked for (read back off the created Setup, and it is
    # re-listed before the payload is built), holding the model it was given and no operations yet.
    ("cam_create_setup", {"models": [PART_COMP], "name": CAM_SETUP},
     lambda p: p["created"] is True and p["setup_name"] == CAM_SETUP
     and p["operation_type"] == "milling" and p["model_count"] >= 1
     and p["operation_count"] == 0, None),
    # the REAL stock solid and the REAL fixture bodies - the shop-template selection shape, each
    # collection's count read back off the setup.
    ("cam_edit_setup", {"setup": CAM_SETUP, "stock": [STOCK_COMP],
                        "fixtures": [VISE_BASE, JAW_FIXED, JAW_MOVING]},
     _setup_bodies(stock=1, fixtures=3), None),
    # THE ASSOCIATIVE SEAM ON CAMERA: a Joint Origin at the real stock's center becomes the
    # setup WCS; the bound_entities read-back lands in ctx as the receipt's evidence.
    ("joint_create_origin", {"anchor": "bbox_center", "bbox_target": STOCK_COMP + ":1",
                             "orient_axis": "z", "name": "StockWCS"},
     _joint_origin_computed("StockWCS"), None),
    ("cam_edit_setup", {"setup": CAM_SETUP, "wcs": {"origin": "StockWCS"}}, "ok",
     ("wcs_bound_entities", lambda p: p["wcs_set"]["origin"]["bound_entities"])),
    # THE STRATEGY VOCABULARY THIS SETUP OFFERS, read before a single operation is created: the
    # tallies partition it, and every strategy the creates below name is in it reading allowed. A
    # spelling this build does not carry, or one this licence will not generate, reds here with the
    # name in the row - not eight rows later inside a create.
    ("cam_get", {"include": ["strategies"], "setup": CAM_SETUP},
     _offers(CAM_SETUP, "face", "adaptive", "contour2d", "pocket2d", "bore", "chamfer2d", "drill",
             "engrave"),
     None),
    # THE JOB, in the order a shop would cut it: face the top, rough the whole part with the 3D
    # adaptive, open the pocket with 2D offset roughing, finish the boss, break the stepped top's
    # edges, then the hole-making - spot the two through bores, drill the counterbored mounting
    # pattern, and bore the through holes to size. The default name of a created operation is the
    # PLATFORM'S to pick, so the ones a later row addresses are either NAMED here (and read back
    # off the created operation) or taken from what the create PUBLISHED and carried in ctx - a
    # literal for one of those would be a guess.
    ("cam_create_operation", {"setup": CAM_SETUP, "strategy": "face",
                              "tool_scope": "document", "tool_index": _FACE_MILL,
                              "generate": False},
     _op_created(CAM_SETUP, "face"),
     ("face_op", _recall("face_op", lambda p: p["operation"]))),
    # the stock's own top face, MEASURED before the facing pass is aimed at it: the billet spans
    # z[-8,48], so its top centroid is [0,0,48] and its normal faces +z - which is what separates it
    # from the underside 'nearest_to' would answer with just as readily.
    ("find_geometry", {"target": STOCK_COMP, "kind": "planar_face", "nearest_to": [0, 0, 48],
                       "max_results": 1}, _face_up_at(0, 0, 48, tol=2.0), _fg("stock_top")),
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "face_op", "the face op"),
                                       "selection": "face",
                                       "handles": [_ctx_get(c, "stock_top", "stock top")],
                                       "generate": False}, _selected(1), None),
    ("cam_create_operation", {"setup": CAM_SETUP, "strategy": "adaptive",
                              "tool_scope": "document", "tool_index": _FLAT_MILL,
                              "generate": False},
     _op_created(CAM_SETUP, "adaptive"),
     ("adaptive_op", _recall("adaptive_op", lambda p: p["operation"]))),
    ("cam_create_operation", {"setup": CAM_SETUP, "strategy": "contour2d",
                              "tool_scope": "document", "tool_index": _FLAT_MILL,
                              "generate": False},
     _op_created(CAM_SETUP, "contour2d"),
     ("contour_op", _recall("contour_op", lambda p: p["operation"]))),
    # zero-handle silhouette: applies against the setup's model (live-verified mechanism), and says
    # so - the setup-model flag is set explicitly, never left to a default.
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "contour_op", "the contour op"),
                                       "selection": "silhouette", "generate": False},
     lambda p: p["setup_models_selected"] is True, None),
    # the pocket, opened by the strategy made for it - 2D offset roughing, which follows the
    # radiused corners the 10 mm cutter clears. Named because three later rows address it.
    ("cam_create_operation", {"setup": CAM_SETUP, "strategy": "pocket2d", "name": _POCKET_OP,
                              "tool_scope": "document", "tool_index": _FLAT_MILL,
                              "generate": False},
     _op_named(CAM_SETUP, "pocket2d", _POCKET_OP), None),
    # THE POCKET: the 'pocket' selection kind, driven by the pocket FLOOR itself - the face at z=14
    # the pocket cut opened - which is the kind made for this strategy and the one the refusal beat
    # below meets with an EDGE where that face belongs. The floor is MEASURED before it is
    # machined: a pocket aimed at the wrong face is a pocket every later read still calls a pocket.
    ("find_geometry", {"target": PART_COMP, "kind": "planar_face", "nearest_to": [-35, 0, 14],
                       "max_results": 1}, _face_up_at(-35, 0, 14, tol=2.0), _fg("pocket_floor")),
    ("cam_select_geometry", lambda c: {"operation": _POCKET_OP, "selection": "pocket",
                                       "handles": [_ctx_get(c, "pocket_floor", "the pocket floor")],
                                       "generate": False}, _selected(1), None),
    # THE BOSS, finished twice over so the job reads the same on either licence: the Machining
    # Extension's own 3D steep-and-shallow pass rides the capability tier, and the 2D contour under
    # it is the base-licence variant of the same feature. Where the extension is entitled both cut;
    # where it is not, the contour alone does and the extension row lands in the receipt's
    # skipped(machining_extension not entitled) bucket.
    ("cam_create_operation", {"setup": CAM_SETUP, "strategy": "steep_and_shallow",
                              "name": _BOSS3D_OP, "tool_scope": "document",
                              "tool_index": _BALL_MILL, "generate": False},
     _needs(MACHINING_EXTENSION, _op_named(CAM_SETUP, "steep_and_shallow", _BOSS3D_OP)), None),
    ("cam_create_operation", {"setup": CAM_SETUP, "strategy": "contour2d", "name": _BOSS_OP,
                              "tool_scope": "document", "tool_index": _BALL_MILL,
                              "generate": False},
     _op_named(CAM_SETUP, "contour2d", _BOSS_OP), None),
    # THE BOSS: the same face kind on its top, which is the circle the finishing pass runs round.
    ("find_geometry", {"target": PART_COMP, "kind": "planar_face", "nearest_to": [45, 0, 45],
                       "max_results": 1}, _face_up_at(45, 0, 45, tol=0.5), _fg("boss_top")),
    ("cam_select_geometry", lambda c: {"operation": _BOSS_OP, "selection": "face",
                                       "handles": [_ctx_get(c, "boss_top", "the boss top")],
                                       "generate": False}, _selected(1), None),
    # the stepped top's edges, broken with the chamfer mill - the one operation that cuts with it.
    ("cam_create_operation", {"setup": CAM_SETUP, "strategy": "chamfer2d", "name": _CHAMFER_OP,
                              "tool_scope": "document", "tool_index": _CHAMFER_MILL,
                              "generate": False},
     _op_named(CAM_SETUP, "chamfer2d", _CHAMFER_OP), None),
    # THE STEPPED TOP, whose loops the chamfer runs round: the high half's own face, measured at
    # the centroid the boss pulls slightly back along -x, which the band absorbs.
    ("find_geometry", {"target": PART_COMP, "kind": "planar_face", "nearest_to": [25, 0, 40],
                       "max_results": 1}, _face_up_at(25, 0, 40, tol=2.0), _fg("step_top_face")),
    ("cam_select_geometry", lambda c: {"operation": _CHAMFER_OP, "selection": "face",
                                       "handles": [_ctx_get(c, "step_top_face", "the stepped top")],
                                       "generate": False}, _selected(1), None),
    # THE HOLE MAKING, in its own order: the centre drill spots the two BoreDia through bores, the
    # drill opens the four MountDia mounting holes, and the bore finishes the through bores to size
    # with the 10 mm mill - which is why that mill is 10 mm and not 12.
    ("cam_create_operation", {"setup": CAM_SETUP, "strategy": "drill", "name": _SPOT_OP,
                              "tool_scope": "document", "tool_index": _CENTER_DRILL,
                              "generate": False},
     _op_named(CAM_SETUP, "drill", _SPOT_OP), None),
    # THE TWO THROUGH BORES, spotted before anything opens them. BoreDia is 12 mm, so a radius-6
    # search names exactly those two and nothing else in the part - a count other than two means it
    # found a face this act did not mean, which is why the query is measured rather than saved
    # blind. They are handed to the holes selection with NO diameter filter: the branch that
    # machines exactly the faces it was given, beside the filtered branch the drill below takes.
    ("find_geometry", {"target": PART_COMP, "kind": "cylinder_face", "radius": 6,
                       "max_results": 4}, _matched(2, "cylinder_face"), _fgn("through_bores")),
    ("cam_select_geometry",
     lambda c: {"operation": _SPOT_OP, "selection": "holes",
                "handles": _ctx_get(c, "through_bores", "the two through bores"),
                "generate": False}, _selected(2), None),
    # the drill: the counterbored mounting bores selected by handle + diameter filter (6mm +/- 0.1).
    # MountDia is 6 mm, so a radius-3 query finds the four mounting bores AND the EdgeBreak fillet
    # on the step (measured: five faces). The fillet passes a diameter filter too, so the bores are
    # picked by their Z axis before the handles reach the drill, and the drill reads back four.
    ("cam_create_operation", {"setup": CAM_SETUP, "strategy": "drill",
                              "tool_scope": "document", "tool_index": _DRILL, "generate": False},
     _op_created(CAM_SETUP, "drill"),
     ("drill_op", _recall("drill_op", lambda p: p["operation"]))),
    ("find_geometry", {"target": PART_COMP, "kind": "cylinder_face", "radius": 3,
                       "max_results": 8}, _matched(5, "cylinder_face"),
     ("mount_bores", _recall("mount_bores", lambda p: [
         m["handle"] for m in p["matches"] if abs(m["axis"][2]) > 0.99]))),
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "drill_op", "the drill op"),
                                       "selection": "holes",
                                       "handles": _ctx_get(c, "mount_bores", "the mounting bores"),
                                       "min_diameter": 5.9, "max_diameter": 6.1,
                                       "generate": False}, _selected(4), None),
    ("cam_create_operation", {"setup": CAM_SETUP, "strategy": "bore", "name": _BORE_OP,
                              "tool_scope": "document", "tool_index": _FLAT_MILL,
                              "generate": False},
     _op_named(CAM_SETUP, "bore", _BORE_OP), None),
    # and the SAME two faces to the bore, which is the other half of the holes kind: a drill
    # operation takes them through 'holeFaces' and the bore family through 'circularFaces', so one
    # selection input reaches two different parameters and the count read back off each says so.
    ("cam_select_geometry",
     lambda c: {"operation": _BORE_OP, "selection": "holes",
                "handles": _ctx_get(c, "through_bores", "the two through bores"),
                "generate": False}, _selected(2), None),
    ("cam_get", {"include": ["operations"], "setup": CAM_SETUP}, "ok", None),
    # SKETCH: the scratch circle drawn at the top of this act, asserted on the entity set the applied
    # selection reports rather than on outputGeometry (a curve path count on an ungenerated op is not
    # a measured claim).
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "contour_op", "the contour op"),
                                       "selection": "sketch",
                                       "sketches": ["CamContourSketch"], "generate": False},
     lambda p: p["resolved"]["entities"] >= 1, None),
    # THE ENGRAVING, driven by the same scratch sketch through the SKETCH kind - the second route
    # that selection takes, on the strategy made for a sketch line, cut with the chamfer mill.
    ("cam_create_operation", {"setup": CAM_SETUP, "strategy": "engrave", "name": _ENGRAVE_OP,
                              "tool_scope": "document", "tool_index": _CHAMFER_MILL,
                              "generate": False},
     _op_named(CAM_SETUP, "engrave", _ENGRAVE_OP), None),
    ("cam_select_geometry", {"operation": _ENGRAVE_OP, "selection": "sketch",
                             "sketches": ["CamContourSketch"], "generate": False},
     lambda p: p["resolved"]["entities"] >= 1, None),
    # No pocket_recognition beat: running that selection here coincides with the Fusion process
    # terminating, and a routine sweep must not risk the host. The other selection kinds above and
    # below carry cam_select_geometry's coverage.
    # REFUSED: a knob whose property does not exist on that selection's class - dropping it silently
    # would leave the caller believing an option applied. The guard fires before any CAM read.
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "drill_op", "the drill op"),
                                       "selection": "holes",
                                       "handles": _ctx_get(c, "mount_bores", "the mounting bores"),
                                       "loop_type": "outside"}, "refused", None),
    # REFUSED: the geometry offered through the wrong input - silhouette machines BODIES, and the
    # error names the input to move them to.
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "contour_op", "the contour op"),
                                       "selection": "silhouette",
                                       "handles": [_ctx_get(c, "stock_top", "stock top")]},
     "refused", None),
    # REFUSED: an edge where a pocket floor face belongs. Three gates can catch it - the handle kind,
    # Fusion's own rejection channel, or the 0-selections check - and the ledger note records which
    # message came back. generate stays false so an unexpected pass cannot launch a toolpath off it.
    ("find_geometry", {"target": PART_COMP, "kind": "circular_edge", "max_results": 1}, "ok",
     _fg("part_edge")),
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "contour_op", "the contour op"),
                                       "selection": "pocket",
                                       "handles": [_ctx_get(c, "part_edge", "a part edge")],
                                       "generate": False}, "refused", None),
    # and back to the silhouette this contour is generated from, now through NAMED bodies - the
    # branch that does NOT ride the setup's own models, and the last selection the generate acts on.
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "contour_op", "the contour op"),
                                       "selection": "silhouette",
                                       "bodies": [PART_COMP], "generate": False},
     lambda p: p["setup_models_selected"] is False, None),
    # INSPECTION: the recorded probing results on a job nothing has ever probed. The platform holds
    # this two ways - inspectionResults reads None on some documents, an EMPTY collection on others
    # (this story doc measures the latter) - and both are a real zero-measure answer, never a bare
    # error. The predicate pins the zero, not which of the two shapes carried it.
    ("cam_get", {"include": ["inspection"]},
     lambda p: p["inspection"]["measure_count"] == 0 and p["inspection"]["measures"] == [], None),
    # A scoped read on a document with zero measures is REFUSED naming the count - on this doc the
    # collection exists and empty, so the out-of-range gate answers (the absent-state ok is the
    # None-collection documents' answer).
    ("cam_get", {"include": ["inspection"], "measure": "0"}, "refused", None),
    # REFUSED: the slice's units guard - the one refusal that fires whether or not results exist,
    # since every length in a point row crosses the wire scaled out of CM.
    ("cam_get", {"include": ["inspection"], "units": "furlongs"}, "refused", None),
    # THE PROBING CYCLE, on the part itself: a Probe WCS pass touching off the stepped top. Its
    # tool is a PROBE, cloned by from_type from the shipped 'Probes' library at the index the
    # document library's own count names - cam_create_operation refuses a probing strategy handed
    # a cutting tool, so no other tool in this library can carry the beat.
    ("cam_edit_tools", {"action": "list", "scope": "document"},
     _needs(MACHINING_EXTENSION,
            lambda p: _num(p.get("tool_count")) and p["tool_count"] >= 1),
     ("probe_tool", _recall("probe_tool", lambda p: p["tool_count"]))),
    ("cam_edit_tools", {"action": "add", "scope": "document",
                        "add_tools": [{"from_type": "probe"}]},
     _needs(MACHINING_EXTENSION,
            lambda p: p.get("added") == 1
            and p.get("tool_count") == _RECALL.get("probe_tool") + 1), None),
    ("cam_create_operation", lambda c: {"setup": CAM_SETUP, "strategy": "probe",
                                        "name": _PROBE_OP, "tool_scope": "document",
                                        "tool_index": _ctx_get(c, "probe_tool",
                                                               "the cloned probe's index"),
                                        "generate": False},
     _needs(MACHINING_EXTENSION, _op_named(CAM_SETUP, "probe", _PROBE_OP)), None),
    # the stepped top, measured again for this beat rather than carried from the chamfer's row: a
    # handle is short-lived, and this row is dropped whole where the capability tier holds the
    # beat back.
    ("find_geometry", {"target": PART_COMP, "kind": "planar_face", "nearest_to": [25, 0, 40],
                       "max_results": 1},
     _needs(MACHINING_EXTENSION, _face_up_at(25, 0, 40, tol=2.0)), _fg("probe_face")),
    ("cam_select_geometry", lambda c: {"operation": _PROBE_OP, "selection": "probe",
                                       "handles": [_ctx_get(c, "probe_face", "the stepped top")],
                                       "generate": False},
     _needs(MACHINING_EXTENSION, _probe_applied(1)), None),
    # the feed edit is read BACK off the parameter: 'after' is the expression the platform stored,
    # which a set that did not take leaves at the tool's default.
    ("cam_edit_operation", lambda c: {"operation": _ctx_get(c, "face_op", "the face op"),
                                      "parameters": {"tool_feedCutting": "1200"}},
     lambda p: p["edited"] is True and p["updated_count"] == 1
     and p["changed"][0]["name"] == "tool_feedCutting"
     and "1200" in str(p["changed"][0]["after"]), None),
    # The payload's order/entity_index/reference_index are the destination collection re-read off
    # the parent AFTER the move - the moved item must sit at entity_index on the asked side.
    ("cam_reorder", lambda c: {"entity": _ctx_get(c, "adaptive_op", "the created adaptive op"),
                               "position": "before",
                               "reference": _ctx_get(c, "face_op", "the face op")},
     lambda p: p["order"][p["entity_index"]] == p["moved"]
     and p["order"][p["reference_index"]] == _RECALL.get("face_op")
     and p["entity_index"] < p["reference_index"], None),
    # 'activated' is Setup.name read back AFTER the isActive gate - the tool errors when the setup
    # reads inactive, so the name here is the setup that actually became active.
    ("cam_activate_setup", {"setup": CAM_SETUP},
     lambda p: p["activated"] == CAM_SETUP, None),
    # the compare's precondition, read rather than waited out: it refuses while either named
    # operation still has generating to do, and the selections it walks are what that guards.
    ("cam_get_status", {"target": CAM_SETUP}, _settled(f"setup '{CAM_SETUP}'"), None),
    # two DIFFERENT strategies must differ somewhere: a zero-difference diff would mean the two
    # names resolved to one operation. Both names are read back off the resolved operations.
    ("cam_compare_operations", lambda c: {"operation_a": _ctx_get(c, "face_op", "the face op"),
                                          "operation_b": _ctx_get(c, "adaptive_op",
                                                                  "the created adaptive op")},
     lambda p: _compare_geometry(_RECALL.get("face_op"), _RECALL.get("adaptive_op"))(p)
     and all(d["parameter"] for d in p["differences"]), None),
    # The listed rows are not the operation's whole set: the read counts what its own filter dropped.
    ("cam_get", lambda c: {"include": ["parameters"],
                           "operation": _ctx_get(c, "adaptive_op", "the created adaptive op")},
     lambda p: _params_counted(_RECALL.get("adaptive_op"))(p), None),
    # FOLDERS: organize the job the way a shop sheet reads - milling vs drilling.
    ("cam_edit_folders", {"action": "create", "setup": CAM_SETUP, "name": "Milling"},
     lambda p: p["created"] is True and p["folder"] == "Milling" and p["setup"] == CAM_SETUP,
     None),
    ("cam_edit_folders", {"action": "create", "setup": CAM_SETUP, "name": "Drilling"},
     lambda p: p["created"] is True and p["folder"] == "Drilling" and p["setup"] == CAM_SETUP,
     None),
    # 'moved' counts the moves whose destination-membership re-read GREW the folder under that
    # name; an item the folder already listed lands in 'unattributed', and the first failed move
    # errors naming what had landed - so the count IS the operations measured into the folder.
    # The extension's boss pass is deliberately NOT in either list: it exists only where the
    # capability tier let its create row run, and a move list is a static one.
    ("cam_edit_folders", lambda c: {"action": "move", "setup": CAM_SETUP, "folder": "Milling",
                                    "operations": [_ctx_get(c, "face_op", "the face op"),
                                                   _ctx_get(c, "adaptive_op",
                                                            "the created adaptive op"),
                                                   _ctx_get(c, "contour_op", "the contour op"),
                                                   _POCKET_OP, _BOSS_OP, _CHAMFER_OP]},
     lambda p: p["moved"] == 6 and p["into"] == "Milling" and len(p["operations"]) == 6, None),
    ("cam_edit_folders", lambda c: {"action": "move", "setup": CAM_SETUP, "folder": "Drilling",
                                    "operations": [_SPOT_OP,
                                                   _ctx_get(c, "drill_op", "the drill op"),
                                                   _BORE_OP]},
     lambda p: p["moved"] == 3 and p["into"] == "Drilling", None),
    # WALK-ORDER ADDRESSING, now that the job is split across the setup and two folders. Operation
    # .name DEDUPES rather than refusing, which would mint a second 'Face1' nothing asked for and
    # leave the two tellable apart only by walk position. Both arms that could reach that refuse
    # BEFORE mutating - the create here, the rename below - each naming the count it collided with.
    ("cam_create_operation", lambda c: {"setup": CAM_SETUP, "strategy": "face",
                                        "name": _ctx_get(c, "face_op", "the face op"),
                                        "tool_scope": "document", "tool_index": _FACE_MILL,
                                        "generate": False},
     _refused("already answer to", "dedupes rather than refusing", "cam_get"), None),
    # the rename arm, on an operation in one FOLDER aimed at a name an operation in ANOTHER carries
    # - the refusal is document-wide, not per folder. Nothing is written, so no rename reaches the
    # platform: setting Operation.name is itself a generation trigger (ledger BORE-PARK-REPRO-1).
    ("cam_edit_operation", lambda c: {"operation": _ctx_get(c, "drill_op", "the drill op"),
                                      "rename": _ctx_get(c, "face_op", "the face op")},
     _refused("already answer to", "dedupes rather than refusing"), None),
    ("cam_get", {"include": ["operations"], "setup": CAM_SETUP},
     _paths_address_every_operation(CAM_SETUP), None),
    # A MACHINE OF OUR OWN before the library one: built from a template into the Local library and
    # then proven usable three ways - the create's own re-resolve, the catalog it must list in, and a
    # real assignment. The name carries the run stamp because the library keeps it (see MACHINE_NAME).
    ("cam_create_machine", {"name": MACHINE_NAME, "template": "generic_3_axis",
                            "vendor": "SweepCo"},
     lambda p: p["created"] is True and p["name"] == MACHINE_NAME and bool(p["url"])
     and "milling" in (p["kind"] or []), None),
    # the catalog is the independent witness: the assignment surface lists it under its own vendor.
    ("cam_get", {"include": ["machines"], "vendor": "SweepCo"},
     lambda p: any(m["name"] == MACHINE_NAME for m in p["machines"]["machines"]), None),
    ("cam_edit_setup", {"setup": CAM_SETUP, "machine": MACHINE_NAME},
     lambda p: p.get("machine_set") == MACHINE_NAME, None),
    # the name is now how the library reaches a machine, so a second create is refused naming the
    # machine it collides with and the library holding it.
    ("cam_create_machine", {"name": MACHINE_NAME, "template": "generic_3_axis"}, "refused", None),
    # a real machine, and the assignment the post and setup sheet run on: assigning a
    # simulation-ready machine can be REFUSED - measured on the library machine the measuring run
    # picks, when that machine carries a simulation model - so this step assigns through
    # machine_strip_simulation. Asserted by read-back, not call success.
    ("cam_edit_setup", {"setup": CAM_SETUP, "machine": "Haas VF-2",
                        "machine_strip_simulation": True},
     lambda p: p.get("machine_set") == "Haas VF-2", None),
    ("cam_get", {"include": ["machines"], "vendor": "Haas", "machine_type": "milling"},
     lambda p: p.get("machines", {}).get("count", 0) > 0, None),
    ("cam_show_toolpath", {"action": "list"},
     lambda p: p["action"] == "list" and p["operation_count"] >= 9
     and len(p["operations"]) == p["operation_count"]
     and all(r["op"] for r in p["operations"]), None),
    # the validity verdict BEFORE generation: false, with the not-yet-generated ops named; a scoped
    # check resolves through the shared resolver and a bogus scope is refused listing what exists.
    ("cam_inspect_toolpaths", {},
     lambda p: p["passed"] is False and len(p["measured"]["not_valid"]) > 0, None),
    ("cam_inspect_toolpaths", {"scope": CAM_SETUP}, lambda p: p["passed"] is False, None),
    ("cam_inspect_toolpaths", {"scope": "NoSuchScopeXyz"}, "refused", None),
    # max_results cannot lift the tool's own ceiling: every not_valid row crosses the wire, so an
    # over-cap request is CLAMPED to it (200) rather than answered with a flood.
    ("cam_inspect_toolpaths", {"max_results": 10000},
     lambda p: len(p["measured"]["not_valid"]) <= 200, None),
    # 'target' is the RESOLVED node's kind beside the name asked for, so it is what says the name
    # reached a setup rather than an operation of the same name; the handle is what the poll below
    # would read, and skip_valid is the flag this launch actually ran under.
    ("cam_generate", {"target": CAM_SETUP, "skip_valid": False},
     lambda p: p["launched"] is True and p["target"] == f"setup '{CAM_SETUP}'"
     and p["skip_valid"] is False and bool(p["handle"]), None),
    # generation completion is gated by the bounded poll run() performs after this act (an
    # errored op or an EMPTY toolpath - a 'valid' op that cuts nothing - fails the run).
]

def _tmpl_rows(node, folder=None):
    """Every template ROW in a cam_get(include=['templates']) tree, folders recursed, each paired
    with the folder it was LISTED UNDER. Where a row was found is part of reading it: a shipped
    library lists a template at the root as well as inside its folder, and only the folder's
    listing carries a url (a CAMTemplate has none of its own - the url is the folder's child asset
    at that row's index, which is what 'folder_position' names)."""
    rows = [(folder, t) for t in (node.get("templates") or [])]
    for sub in (node.get("folders") or []):
        rows.extend(_tmpl_rows(sub, sub.get("folder")))
    return rows


def _tmpl_names(node):
    """Every template NAME in that tree - the witness a template teardown is read against."""
    return [t.get("name") for _folder, t in _tmpl_rows(node)]


def _applicable_listing(payload, name):
    """The ONE listing of `name` in a templates tree that cam_apply_template(template_url=...) can
    be given: the row carrying a url, which is the one under the template's own folder. Returns
    (row, folder, every listing found) so a miss reports where the name WAS found and with what."""
    rows = [(f, t) for f, t in _tmpl_rows((payload.get("templates") or {}).get("tree") or {})
            if t.get("name") == name]
    addressable = [(f, t) for f, t in rows if t.get("url")]
    row, folder = (addressable[0][1], addressable[0][0]) if len(addressable) == 1 else (None, None)
    listings = [{"folder": f, "url": t.get("url"), "url_basis": t.get("url_basis")} for f, t in rows]
    return row, folder, listings


def _shipped_template(name):
    """cam_get(include=['templates'], template_location='fusion'): the shipped template of that
    name, taken from the listing that can actually be APPLIED. Exactly one of its listings carries
    a url, and 'url_basis' says how that url addresses the asset - 'folder_position' is the asset
    at that row's INDEX in its folder. The listings are all reported, so a run where the url moved
    to a different one names both."""
    def check(p):
        row, folder, listings = _applicable_listing(p, name)
        return _measured(f"one addressable listing of the shipped template {name!r}",
                         {"listings": listings, "folder": folder},
                         bool(row) and row.get("url_basis") == "folder_position")
    return check


def _shipped_template_url(name):
    """The save-slot extractor beside _shipped_template: the url the apply row addresses it by."""
    return lambda p: _applicable_listing(p, name)[0]["url"]


def _applied_rows_account(p, rows, tool_less):
    """The per-op census every apply publishes: one row per operation the SETUP gained, and
    'tool_unselected' naming exactly the rows carrying NO tool - which excludes a row whose tool
    object is there and only its description did not read (tool_description_unread)."""
    return (len(rows) == p.get("operations_added")
            and tool_less == [r.get("name") for r in rows
                              if r.get("tool") is None and not r.get("tool_description_unread")])


def _template_applied(name, setup, minimum):
    """cam_apply_template of a template bundled from an operation that CARRIES a tool: the row count
    accounts for operations_added, nothing is named in 'tool_unselected', and 'ready' reads true -
    the opposite reading from the shipped bundle below, which is why neither is asserted
    symmetrically."""
    def check(p):
        rows = p.get("operations") or []
        tool_less = p.get("tool_unselected")
        return _measured(f"{name!r} applied to '{setup}', every added operation carrying a tool",
                         {"applied": p.get("applied"), "template": p.get("template"),
                          "setup": p.get("setup"), "operations_added": p.get("operations_added"),
                          "operations": rows, "tool_unselected": tool_less, "ready": p.get("ready")},
                         p.get("applied") is True and p.get("template") == name
                         and p.get("setup") == setup and (p.get("operations_added") or 0) >= minimum
                         and _applied_rows_account(p, rows, tool_less)
                         and tool_less == [] and p.get("ready") is True)
    return check


def _template_applied_tool_less(name, setup, minimum):
    """The same census on the SHIPPED hole bundle, whose operations arrive carrying no tool: every
    row reads a null tool, 'tool_unselected' names all of them, and 'ready' is false."""
    def check(p):
        rows = p.get("operations") or []
        tool_less = p.get("tool_unselected")
        cycles = _bundle_ops(p)
        return _measured(f"{name!r} applied to '{setup}' with every operation tool-less",
                         {"applied": p.get("applied"), "operations_added": p.get("operations_added"),
                          "operations": rows, "tool_unselected": tool_less, "ready": p.get("ready"),
                          "cycles": sorted(cycles)},
                         p.get("applied") is True and p.get("template") == name
                         and p.get("setup") == setup and (p.get("operations_added") or 0) >= minimum
                         and _applied_rows_account(p, rows, tool_less)
                         and p.get("ready") is False and bool(rows)
                         and len(tool_less) == len(rows)
                         and set(cycles) == set(_CYCLES)
                         and all(r.get("tool") is None and not r.get("tool_description_unread")
                                 for r in rows))
    return check


def _tool_assigned(index, cycle):
    """cam_edit_operation(tool_scope='document', tool_index=...): the cutter landed on the operation
    the apply published for THAT cycle. 'operation' and 'strategy' are read back off the edited
    Operation and both compared with the apply's own row, so a bundle landing in another tree order
    reddens here instead of pairing a centre drill with a counterbore."""
    def check(p):
        want = (_RECALL.get("tmpl_ops") or {}).get(cycle) or {}
        return _measured(f"document tool {index} on the bundle's {cycle} cycle",
                         {"operation": p.get("operation"), "strategy": p.get("strategy"),
                          "expected": want, "tool": p.get("tool"),
                          "was_tool": p.get("was_tool"), "tool_number": p.get("tool_number"),
                          "tool_index": p.get("tool_index"),
                          "is_toolpath_valid": p.get("is_toolpath_valid")},
                         p.get("tool_index") == index and bool(p.get("tool"))
                         and p.get("was_tool") is None and bool(want)
                         and p.get("operation") == want.get("name")
                         and p.get("strategy") == want.get("strategy"))
    return check


def _all_cut(setup, minimum, names=None):
    """cam_get(include=['time']) over a setup: every operation carries its own getMachiningTime
    figure above zero - or, with 'names', every operation NAMED does, which is the same reading
    scoped to the rows a beat created rather than to whatever else the setup holds.

    That is the read that tells a cutting toolpath from an empty one - hasToolpath reads TRUE on
    both - so a job whose every row clears zero is a job where nothing generated into air. An
    operation reading no figure at all is named on the row, since an absent figure and a zero one
    are the same failure to this beat."""
    def check(p):
        recs = ((p.get("time") or {}).get("setups") or [])
        rec = next((r for r in recs if r.get("setup") == setup), None)
        rows = (rec or {}).get("operations") or []
        ops = ([r for r in rows if r.get("operation") in names] if names is not None else rows)
        missing = ([n for n in names if n not in {r.get("operation") for r in rows}]
                   if names is not None else [])
        idle = [r.get("operation") for r in ops
                if not (_num(r.get("machining_time_seconds"))
                        and r["machining_time_seconds"] > 0)]
        label = (f"all {len(names)} named operations in '{setup}' cut" if names is not None
                 else f"every one of '{setup}'s operations cuts (machining time above zero)")
        return _measured(label,
                         {"operation_count": len(ops), "idle": idle, "missing": missing,
                          "seconds": {r.get("operation"): r.get("machining_time_seconds")
                                      for r in ops}},
                         len(ops) >= minimum and not idle and not missing)
    return check


def _op_deleted(name_or_key):
    """cam_delete: the entity went, and 'entity_type' is the RESOLVED node's kind - which is what
    says an OPERATION was deleted rather than the setup or folder a shared name could have reached.
    A key that _RECALL holds resolves to the platform-picked name it stored; anything else is the
    literal name asked for."""
    def check(p):
        want = _RECALL.get(name_or_key, name_or_key)
        return _measured(f"{want!r} deleted, and the node deleted was an operation",
                         {"deleted": p.get("deleted"), "entity": p.get("entity"),
                          "entity_type": p.get("entity_type")},
                         p.get("deleted") is True and p.get("entity") == want
                         and p.get("entity_type") == "operation")
    return check


def _setup_ready(setup, count):
    """cam_get(include=['operations']) on one generated setup: the readiness verdict is published
    either way, and a setup carrying a blocked_by changes its WORDING to "'ready to post' is NOT
    established" - so the postable wording is what says the machine assignment cleared the
    blocker, and the exceptions list is asserted empty beside it."""
    def check(p):
        recs = ((p.get("operations") or {}).get("setups") or [])
        rec = next((r for r in recs if r.get("setup") == setup), None)
        summary = (rec or {}).get("summary") or {}
        verdict = str(summary.get("readiness") or "")
        return _measured(f"'{setup}' reads {count} active operations and a postable verdict",
                         {"summary": summary},
                         summary.get("active_count") == count
                         and summary.get("exceptions") == []
                         and ("ready to post." in verdict or "postable, but" in verdict))
    return check


# ACT 10b: CAM read-back + deliverables on the generated job - toolpath shown, NC posted,
# template saved and re-applied.
_CAM_DELIVER = [
    # THE NON-EMPTY ORACLE OVER THE WHOLE JOB, taken first, before any row here suppresses or
    # deletes an operation: every operation the setup holds reports its own machining time above
    # zero. The act-boundary poll fails on an EMPTY toolpath; this says the same thing per
    # operation, with the seconds on the row.
    ("cam_get", {"include": ["time"], "setup": CAM_SETUP}, _all_cut(CAM_SETUP, 9), None),
    # THE PROBING PASS the job act created, read once the act boundary's poll has certified the
    # generation: the Probe WCS cycle reads valid on its own row. It is gated with the rows that
    # made it - where the extension is not entitled there is no such operation to read.
    ("cam_get", {"include": ["operations"], "setup": CAM_SETUP},
     _needs(MACHINING_EXTENSION, _op_valid(CAM_SETUP, _PROBE_OP)), None),
    # the validity verdict flips true once generation completed (the act boundary's poll certified
    # it); the not-valid breakdown is empty.
    ("cam_inspect_toolpaths", {"scope": CAM_SETUP},
     lambda p: p["passed"] is True and p["measured"]["not_valid"] == [], None),
    # the tally's scope is an INPUT: include_suppressed=true widens it back to every operation.
    # tolerance_used names the tally's set and the VERDICT's set separately because they differ -
    # CAM's own check counts suppressed operations whatever this flag says - so a scoped call whose
    # verdict came from checkToolpath reports the verdict as covering all of them either way.
    # Nothing is suppressed yet, so this pins the flag's plumbing; the FILTERING itself is exercised
    # at the end of this act, where a real suppression exists to filter.
    ("cam_inspect_toolpaths", {"scope": CAM_SETUP, "include_suppressed": True},
     lambda p: p["tolerance_used"]["tally_counts"] == "all_operations"
     and p["tolerance_used"]["verdict_counts"] == "all_operations"
     and p["measured"]["suppressed_excluded"] == 0, None),
    ("cam_get", {"include": ["operations"], "setup": CAM_SETUP}, "ok", None),
    # the reverse lookup, which only has an answer once operations exist: which of them use the mill
    # this job was cut with. It is document-scope ONLY - a shared library has no operations - so the
    # local scope is refused rather than answered with an empty list that would read as "none use
    # it". The turning tool, added for the from_type census and never selected, is the other half:
    # its own answer must be zero, or 'where_used' is not looking at operations at all.
    ("cam_edit_tools", {"action": "where_used", "scope": "document", "tool": 0},
     lambda p: p.get("tool") == 0 and p.get("operation_count", 0) >= 1
     and len(p.get("operations") or []) == p["operation_count"], None),
    ("cam_edit_tools", {"action": "where_used", "scope": "document", "tool": _TURNING},
     lambda p: p.get("operation_count") == 0 and "not used" in (p.get("note") or ""), None),
    ("cam_edit_tools", {"action": "where_used", "scope": "local", "tool": 0}, "refused", None),
    # THE TOOLPATH REVEAL, in the choreography every CAM act shares: the machining region framed
    # ONCE, then _reveal's on-held-off pass over each operation. The four names the PLATFORM picked
    # are addressed through ctx; the passes this act named are addressed directly.
    _watch([STOCK_COMP + ":1"]),
    ("view_screenshot", {"width": 500, "height": 400}, "ok", None),
] + _reveal([("face_op", "the face op"), ("adaptive_op", "the adaptive op"),
             ("contour_op", "the contour op"), _POCKET_OP, _BOSS_OP, _CHAMFER_OP, _ENGRAVE_OP,
             _SPOT_OP, ("drill_op", "the drill op"), _BORE_OP]) + [
    # ISOLATE, the third action and the only one that promises nothing else is drawn: it hides every
    # operation in the document and KEEPS the read-backs, so a bulb that would not go dark is
    # published as a hide_failure. fit stays false - the frame above it is the one this act set.
    ("cam_show_toolpath", {"action": "isolate", "operation": _POCKET_OP, "fit": False},
     _isolated(_POCKET_OP), None),
    _dwell(1.5),
    _hide_all(),
    # the deliverable itself: the tool errors unless a non-stub file LANDED, so the payload's file
    # rows are the proof - each one stat'd on disk - and 'scope' is the resolved node's kind.
    ("cam_post", {"scope": CAM_SETUP, "post": "haas", "post_scope": "local",
                  "output_folder": EXPORT_DIR + "/nc", "program_name": "1001"},
     lambda p: p["posted"] is True and p["scope"] == "setup" and p["program_name"] == "1001"
     and p["file_count"] == len(p["files"]) and p["file_count"] >= 1
     and all(f["size_bytes"] > 0 for f in p["files"]), None),
    # the sheet file must LAND (the API's bool answers before the async write completes)
    ("cam_generate_setup_sheet", {"scope": CAM_SETUP, "output_folder": EXPORT_DIR + "/sheets"},
     lambda p: p.get("generated") is True and p.get("size_bytes", 0) > 0, None),
    # comment_after is the parameter re-read after the write, per program - the value the G-code
    # header will carry, not the value the call was handed.
    ("cam_set_nc_comment", {"comment": "BRACKET sweep"},
     lambda p: p["set"] is True and p["programs_changed"] >= 1
     and all(r["comment_after"] == "BRACKET sweep" for r in p["programs"]), None),
    # the saved template names the operation it was bundled from (read off the Operation objects the
    # names resolved to) and carries the url a template loaded back from - the tool refuses the save
    # when nothing loads from what importTemplate returned.
    ("cam_save_template", lambda c: {"template_name": TEMPLATE_NAME, "setup": CAM_SETUP,
                                     "operations": _ctx_get(c, "face_op", "the face op"),
                                     "location": "local"},
     lambda p: p["saved"] is True and p["template"] == TEMPLATE_NAME
     and p["operation_count"] == 1 and p["operations"] == [_RECALL.get("face_op")]
     and bool(p["template_url"]), None),
    ("cam_create_setup", {"models": [PART_COMP], "name": "Setup2"},
     lambda p: p["created"] is True and p["setup_name"] == "Setup2"
     and p["operation_count"] == 0, None),
    # Setup2 holds NO operations at this instant - the template lands on it in the next beat. A
    # setup with zero operations is the one CAM.checkToolpath raises on, so this is the document
    # -level read taken with an empty setup present: it must ANSWER, carrying the disclosure key
    # for what it left out, instead of failing the whole read on the one empty setup. The count
    # itself is not asserted - it is only non-zero when checkAllToolpaths raised and the per-setup
    # fallback ran, which is the document's business, not this beat's.
    ("cam_inspect_toolpaths", {},
     lambda p: isinstance(p["passed"], bool) and "empty_setups_excluded" in p["measured"], None),
    # operations_added is the setup's OWN allOperations count across the apply - the read the tool
    # refuses on when it does not rise - beside the template and setup names read off the resolved
    # objects, which is what says the by-name search reached the template this run saved.
    ("cam_apply_template", {"setup": "Setup2", "template_name": TEMPLATE_NAME,
                            "location": "local", "generate": "skip"},
     _template_applied(TEMPLATE_NAME, "Setup2", 1), None),
    # THE SHIPPED LIBRARY, the other half of the same tool: Fusion's own hole-drilling bundle,
    # reached by the url the templates slice publishes for it rather than by a name search. The
    # read is what says the url exists and how it addresses the asset; the apply hands BOTH the url
    # and the name, so the tool's own cross-check - the url must LOAD the template the name says -
    # is what makes the pair a measurement instead of two independent hopes.
    ("cam_get", {"include": ["templates"], "template_location": "fusion", "template_depth": 6},
     _shipped_template(_FUSION_HOLE_TEMPLATE),
     ("hole_template_url", _shipped_template_url(_FUSION_HOLE_TEMPLATE))),
    ("cam_apply_template",
     lambda c: {"setup": "Setup2",
                "template_url": _ctx_get(c, "hole_template_url", "the shipped hole template url"),
                "template_name": _FUSION_HOLE_TEMPLATE, "location": "fusion",
                "generate": "skip"},
     _template_applied_tool_less(_FUSION_HOLE_TEMPLATE, "Setup2", 2),
     ("tmpl_ops", _recall("tmpl_ops", _bundle_ops))),
    # THE REMEDY the apply's note names, run on EVERY one of those operations: a document tool by
    # index, with Operation.tool read back and 'was_tool' null - which is what says the operation
    # reached here carrying none. Each row addresses its operation by the bundle CYCLE the apply
    # filed it under, so the cutter cannot land on a neighbour if the apply orders them differently.
    ("cam_edit_operation",
     lambda c: {"operation": _ctx_get(c, "tmpl_ops", _TOOL_LESS)["Spotdrill"]["name"],
                "tool_scope": "document", "tool_index": _CENTER_DRILL},
     _tool_assigned(_CENTER_DRILL, "Spotdrill"), None),
    ("cam_edit_operation",
     lambda c: {"operation": _ctx_get(c, "tmpl_ops", _TOOL_LESS)["Drill"]["name"],
                "tool_scope": "document", "tool_index": _DRILL},
     _tool_assigned(_DRILL, "Drill"), None),
    ("cam_edit_operation",
     lambda c: {"operation": _ctx_get(c, "tmpl_ops", _TOOL_LESS)["Counterbore"]["name"],
                "tool_scope": "document", "tool_index": _FLAT_MILL},
     _tool_assigned(_FLAT_MILL, "Counterbore"), None),
    # A template carries no geometry, so the three cycles are aimed at the part's own bores here.
    # The radius-3 query finds the four mounting bores AND the EdgeBreak fillet on the step
    # (measured: five faces), so the bores are picked by their Z axis. MountDia * 1.8 is 10.8 mm.
    ("find_geometry", {"target": PART_COMP, "kind": "cylinder_face", "radius": 3,
                       "max_results": 8}, _matched(5, "cylinder_face"),
     ("tmpl_bores", _recall("tmpl_bores", lambda p: [
         m["handle"] for m in p["matches"] if abs(m["axis"][2]) > 0.99]))),
    ("find_geometry", {"target": PART_COMP, "kind": "cylinder_face", "radius": 5.4,
                       "max_results": 8}, _matched(4, "cylinder_face"), _fgn("tmpl_cbores")),
    ("cam_select_geometry",
     lambda c: {"operation": _ctx_get(c, "tmpl_ops", _TOOL_LESS)["Spotdrill"]["name"],
                "selection": "holes",
                "handles": _ctx_get(c, "tmpl_bores", "the mounting bores"),
                "generate": False}, _selected(4, on="Spotdrill"), None),
    ("cam_select_geometry",
     lambda c: {"operation": _ctx_get(c, "tmpl_ops", _TOOL_LESS)["Drill"]["name"],
                "selection": "holes",
                "handles": _ctx_get(c, "tmpl_bores", "the mounting bores"),
                "generate": False}, _selected(4, on="Drill"), None),
    ("cam_select_geometry",
     lambda c: {"operation": _ctx_get(c, "tmpl_ops", _TOOL_LESS)["Counterbore"]["name"],
                "selection": "holes",
                "handles": _ctx_get(c, "tmpl_cbores", "the counterbores"),
                "generate": False}, _selected(4, on="Counterbore"), None),
    ("cam_delete", lambda c: {"entity": _ctx_get(c, "adaptive_op", "the created adaptive op")},
     _op_deleted("adaptive_op"), None),
    # SUPPRESSION, last of the job edits: the flag is a WRITE here, and it is what gives
    # include_suppressed's FILTERING its live reading. It sits after the post, the setup sheet and
    # the template because suppressing DISCARDS the operation's toolpath - here that costs no later
    # beat - and the restore below reads the SAME ctx name the suppression did and asserts an
    # end-state, so it runs and passes whatever the three steps in between did.
    ("cam_inspect_toolpaths", {"scope": CAM_SETUP},
     lambda p: p["measured"]["suppressed_excluded"] == 0
     and p["measured"]["states"]["suppressed"] == 0,
     ("active_ops_before", _recall("active_ops_before",
                                   lambda p: p["measured"]["states"]["total"]))),
    ("cam_edit_operation", lambda c: {"operation": _ctx_get(c, "drill_op", "the drill op"),
                                      "suppressed": True},
     lambda p: p["is_suppressed"] is True and p["was_suppressed"] is False
     and p["had_toolpath"] is True and p["has_toolpath"] is False, None),
    # the FILTERED read: one operation fewer in the tally than the baseline counted, the suppressed
    # bucket empty because the suppressed op was left OUT of the tally, and the excluded count
    # naming what it left out.
    ("cam_inspect_toolpaths", {"scope": CAM_SETUP},
     lambda p: p["measured"]["suppressed_excluded"] == 1
     and p["measured"]["states"]["suppressed"] == 0
     and p["measured"]["states"]["total"] == _RECALL.get("active_ops_before") - 1
     and p["tolerance_used"]["tally_counts"] == "active_operations", None),
    # the same read WIDENED: every operation back in the tally, the suppressed one counted in its
    # own bucket. The pair is the filter - one flag, two different sets over one job.
    ("cam_inspect_toolpaths", {"scope": CAM_SETUP, "include_suppressed": True},
     lambda p: p["measured"]["states"]["total"] == _RECALL.get("active_ops_before")
     and p["measured"]["states"]["suppressed"] == 1
     and p["measured"]["suppressed_excluded"] == 0, None),
    ("cam_edit_operation", lambda c: {"operation": _ctx_get(c, "drill_op", "the drill op"),
                                      "suppressed": False},
     lambda p: p["is_suppressed"] is False, None),
    # TEARDOWN of the two assets this run leaves outside the document, each after the beats that use
    # it: the confirm_name guard while the asset exists, the delete on its own read-backs, then the
    # library read as witness. 10a's narrative with 10b's fallback leaves the run-stamped machine.
    ("cam_delete_machine", {"name": MACHINE_NAME, "confirm_name": "NotThisMachine"},
     "refused", None),
    ("cam_delete_machine", {"name": MACHINE_NAME, "confirm_name": MACHINE_NAME},
     lambda p: p["deleted"] is True and p["machine"] == MACHINE_NAME
     and p["resolves_after_delete"] is False, None),
    ("cam_get", {"include": ["machines"], "vendor": "SweepCo"},
     lambda p: not any(m["name"] == MACHINE_NAME for m in p["machines"]["machines"]), None),
    ("cam_delete_template", {"name": TEMPLATE_NAME, "confirm_name": "NotThisTemplate"},
     "refused", None),
    ("cam_delete_template", {"name": TEMPLATE_NAME, "confirm_name": TEMPLATE_NAME},
     lambda p: p["deleted"] is True and p["template"] == TEMPLATE_NAME
     and p["loads_after_delete"] is False and p["location"] == "local", None),
    ("cam_get", {"include": ["templates"], "template_location": "local"},
     lambda p: TEMPLATE_NAME not in _tmpl_names(p["templates"]["tree"]), None),
    ("design_export", {"format": "step", "file_path": EXPORT_DIR + "/bracket_export",
                       "target": PART_COMP}, "ok", None),
    # the SPLIT path writes one file per top-level occurrence, each through its OWN options object -
    # so the format knob is read back PER FILE: every files[] record carries its own
    # 'options_applied' (the value that LANDED on that file, null when it did not), beside the
    # top-level 'options_requested'. Every file must read the knob back true, not just the first.
    ("design_export", {"format": "stl", "file_path": EXPORT_DIR + "/bracket_split",
                       "split_by_component": True, "stl_binary": True},
     lambda p: p.get("exported") is True and p.get("split_by_component") is True
     and p.get("options_requested", {}).get("stl_binary") is True
     and bool(p.get("files")) and all((f.get("options_applied") or {}).get("stl_binary") is True
                                      for f in p["files"]), None),
    ("doc_insert_import", {"file_path": EXPORT_DIR + "/bracket_export.step"}, _imported, None),
    # Setup2 launched last, once every operation it holds carries a cutter AND read a selection
    # back: a generate over an operation Fusion refuses parks the process behind a modal dialog.
    ("cam_generate", {"target": "Setup2", "skip_valid": False}, _launched_on("Setup2"), None),
]


def poll_generation(rows, notes, setup, max_polls=40, valued=None):
    """Read cam_get_status until completed (bounded). Generation free-runs in the background and a
    status read returns immediately, so real wall-clock sits between reads. An errored op, an EMPTY
    toolpath (a 'valid' op that cuts nothing - the silent version of wrong), or an exhausted budget
    FAILs.

    Its pass reads VALUES off the payload (completed, live_states, empty_toolpaths) rather than call
    success, so the passing row registers cam_get_status in 'valued' - the receipt's covered bucket -
    the same way a STEPS value predicate does."""
    # the wire read and the shot-list note as the facade holds them - see verify_core.facade
    call, STORY = facade("call"), facade("STORY")
    for i in range(max_polls):
        if i:
            time.sleep(5)
        is_error, payload = call("cam_get_status", {"target": setup})
        if is_error:
            rows.append(("cam_get_status", "FAIL", str(payload)[:NOTE_MAX]))
            return
        states = payload.get("live_states", {})
        if states.get("errored"):
            rows.append(("cam_get_status", "FAIL",
                         f"{states['errored']} operation(s) errored during generation"))
            return
        if payload.get("completed"):
            empty = payload.get("empty_toolpaths") or []
            if empty:
                rows.append(("cam_get_status", "FAIL",
                             f"empty toolpath(s) - nothing to machine: {', '.join(empty)}"))
            else:
                rows.append(("cam_get_status", "pass",
                             f"{states.get('valid', '?')} valid, non-empty toolpaths"))
                notes["cam_get_status"] = STORY.get("cam_get_status", "")
                if valued is not None:
                    valued.add("cam_get_status")
            return
    rows.append(("cam_get_status", "FAIL", f"not complete after {max_polls} polls"))


# ACT 10a fallback: the scratch-stock milling job (kept whole so the CAM family stays covered
# when the story world could not build).
_CAM = (
    _box("ScratchStock", ox=700)
    + [
        _watch("ScratchStock:1"),
        ("view_switch_workspace", {"workspace": "manufacture"}, "ok", None),
        # CAM is looked at, not sketched in: drop the sketch clutter design-wide for the whole
        # machining movement. The FOLDER bulb, so no entity's own visibility is disturbed and a
        # sketch the CAM selection already holds by name is unaffected. Put back in the FINALE.
        ("view_set", {"action": "display", "categories": ["sketches"], "visible": False},
         lambda p: p.get("visible") is False, None),
        ("cam_get", {}, "ok", None),
        ("cam_edit_tools", {"action": "add", "scope": "document", "add_tools": [{"from_type": "flat end mill"}]}, "ok", None),
        ("cam_create_setup", {"models": ["ScratchStock"], "name": "Setup1"},
         lambda p: p["created"] is True and p["setup_name"] == "Setup1"
         and p["operation_type"] == "milling" and p["model_count"] >= 1
         and p["operation_count"] == 0, None),
        # THE ASSOCIATIVE SEAM ON CAMERA: bind the setup's WCS to the StockCenter Joint Origin (ACT 3
        # created it; either path). The row is hard-gated - cam_edit_setup errors when the JO binds
        # zero entities - and the bound_entities read-back lands in ctx as the receipt's evidence.
        ("cam_edit_setup", {"setup": "Setup1", "wcs": {"origin": "StockCenter"}}, "ok",
         ("wcs_bound_entities", lambda p: p["wcs_set"]["origin"]["bound_entities"])),
        # STOCK SIZED FROM PARAMETERS: the driver is read FRESH and the fixed-box stock dims are
        # COMPUTED from it (PartLen/4 square, PartLen/8 tall - encloses the 20x20x10 stock part).
        # Computed-numbers-from-a-fresh-read is the verifiable shape: the CAM parameter store accepts
        # any expression TEXT unevaluated (a bogus name stores fine), so a CAD-param expression string
        # cannot be trusted to evaluate - a live-probed fact.
        # PartLen is created at 120 mm in ACT 1 (which always runs its narrative) and the resize
        # act puts it back to 120 - so this reads the driver at the value the story left it.
        # param_get publishes 'value' already in the parameter's own unit (mm here: 120, not 12 cm),
        # so it is read as-is - a x10 would size the stock at 300 mm for a 30 mm box.
        ("param_get", {"name": PART_DRIVER}, _param_read(PART_DRIVER, 120),
         ("driver_mm", lambda p: p["parameter"]["value"])),
        ("cam_edit_setup", lambda c: {"setup": "Setup1", "parameters": {
            "job_stockMode": "'fixedbox'",
            "job_stockFixedX": "{0} mm".format(_ctx_get(c, "driver_mm", "the driver in mm") / 4),
            "job_stockFixedY": "{0} mm".format(_ctx_get(c, "driver_mm", "the driver in mm") / 4),
            "job_stockFixedZ": "{0} mm".format(_ctx_get(c, "driver_mm", "the driver in mm") / 8)}},
         "ok", None),
        # the face op's name is the platform's to pick here too, and two later beats address it -
        # so it rides ctx exactly as its adaptive sibling does.
        ("cam_create_operation", {"setup": "Setup1", "strategy": "face", "tool_scope": "document", "tool_index": 0, "generate": False},
         _op_created("Setup1", "face"),
         ("face_op", _recall("face_op", lambda p: p["operation"]))),
        # the adaptive's default name comes from the platform, so it rides ctx here too.
        ("cam_create_operation", {"setup": "Setup1", "strategy": "adaptive", "tool_scope": "document", "tool_index": 0, "generate": False},
         _op_created("Setup1", "adaptive"),
         ("adaptive_op", _recall("adaptive_op", lambda p: p["operation"]))),
        ("cam_get", {"include": ["operations"], "setup": "Setup1"}, "ok", None),
        ("find_geometry", {"target": "ScratchStock", "kind": "planar_face", "nearest_to": [710, 10, 10], "max_results": 1}, "ok", _fg("cam_top")),
        ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "face_op", "the face op"), "selection": "face", "handles": [_ctx_get(c, "cam_top", "cam top face")], "generate": False}, _selected(1), None),
        ("cam_edit_operation", lambda c: {"operation": _ctx_get(c, "face_op", "the face op"),
                                          "parameters": {"tool_feedCutting": "1200"}},
         lambda p: p["edited"] is True and p["updated_count"] == 1
         and p["changed"][0]["name"] == "tool_feedCutting"
         and "1200" in str(p["changed"][0]["after"]), None),
        ("cam_edit_setup", {"setup": "Setup1", "models": ["ScratchStock"]}, "ok", None),
        ("cam_edit_folders", {"action": "create", "setup": "Setup1", "name": "Folder1"},
         lambda p: p["created"] is True and p["folder"] == "Folder1"
         and p["setup"] == "Setup1", None),
        ("cam_reorder", lambda c: {"entity": _ctx_get(c, "adaptive_op", "the created adaptive op"),
                                   "position": "before",
                                   "reference": _ctx_get(c, "face_op", "the face op")},
         lambda p: p["order"][p["entity_index"]] == p["moved"]
         and p["order"][p["reference_index"]] == _RECALL.get("face_op")
         and p["entity_index"] < p["reference_index"], None),
        ("cam_activate_setup", {"setup": "Setup1"},
         lambda p: p["activated"] == "Setup1", None),
        # the compare's precondition, read rather than waited out - see _settled.
        ("cam_get_status", {"target": "Setup1"}, _settled("setup 'Setup1'"), None),
        ("cam_compare_operations", lambda c: {"operation_a": _ctx_get(c, "face_op", "the face op"),
                                              "operation_b": _ctx_get(c, "adaptive_op",
                                                                      "the created adaptive op")},
         lambda p: p["operation_a"] == _RECALL.get("face_op")
         and p["operation_b"] == _RECALL.get("adaptive_op")
         and p["difference_count"] >= 1 and all(d["parameter"] for d in p["differences"]), None),
        ("cam_show_toolpath", {"action": "list"},
         lambda p: p["action"] == "list" and p["operation_count"] >= 2
         and len(p["operations"]) == p["operation_count"]
         and all(r["op"] for r in p["operations"]), None),
        ("cam_generate", {"target": "Setup1", "skip_valid": False},
         lambda p: p["launched"] is True and p["target"] == "setup 'Setup1'"
         and p["skip_valid"] is False and bool(p["handle"]), None),
        ("cam_get_status", {"target": "Setup1"}, "ok", None),
    ]
)

# ACT 10b fallback: deliverables on the scratch job.
_CAM_FB_DELIVER = [
    ("cam_post", {"scope": "Setup1", "post": "haas", "post_scope": "local", "output_folder": EXPORT_DIR + "/nc", "program_name": "1001"},
     lambda p: p["posted"] is True and p["scope"] == "setup" and p["program_name"] == "1001"
     and p["file_count"] == len(p["files"]) and p["file_count"] >= 1
     and all(f["size_bytes"] > 0 for f in p["files"]), None),
    ("cam_generate_setup_sheet", {"scope": "Setup1", "output_folder": EXPORT_DIR + "/sheets"},
     lambda p: p.get("generated") is True and p.get("size_bytes", 0) > 0, None),
    ("cam_set_nc_comment", {"comment": "BRACKET sweep"},
     lambda p: p["set"] is True and p["programs_changed"] >= 1
     and all(r["comment_after"] == "BRACKET sweep" for r in p["programs"]), None),
    ("cam_save_template", lambda c: {"template_name": TEMPLATE_NAME, "setup": "Setup1",
                                     "operations": _ctx_get(c, "face_op", "the face op"),
                                     "location": "local"},
     lambda p: p["saved"] is True and p["template"] == TEMPLATE_NAME
     and p["operation_count"] == 1 and p["operations"] == [_RECALL.get("face_op")]
     and bool(p["template_url"]), None),
    ("cam_create_setup", {"models": ["ScratchStock"], "name": "Setup2"},
     lambda p: p["created"] is True and p["setup_name"] == "Setup2"
     and p["operation_count"] == 0, None),
    ("cam_apply_template", {"setup": "Setup2", "template_name": TEMPLATE_NAME, "location": "local", "generate": "skip"},
     _template_applied(TEMPLATE_NAME, "Setup2", 1), None),
    ("cam_delete", lambda c: {"entity": _ctx_get(c, "adaptive_op", "the created adaptive op")},
     lambda p: p["deleted"] is True and p["entity"] == _RECALL.get("adaptive_op")
     and p["entity_type"] == "operation", None),
    # the same teardown as the narrative branch, in the branch that saved the template: the guard
    # while the asset still exists, the delete on its own read-backs, then the library as witness.
    ("cam_delete_template", {"name": TEMPLATE_NAME, "confirm_name": "NotThisTemplate"},
     "refused", None),
    ("cam_delete_template", {"name": TEMPLATE_NAME, "confirm_name": TEMPLATE_NAME},
     lambda p: p["deleted"] is True and p["template"] == TEMPLATE_NAME
     and p["loads_after_delete"] is False, None),
    ("cam_get", {"include": ["templates"], "template_location": "local"},
     lambda p: TEMPLATE_NAME not in _tmpl_names(p["templates"]["tree"]), None),
    ("design_export", {"format": "step", "file_path": EXPORT_DIR + "/bracket_export", "target": "ScratchStock"}, "ok", None),
    ("doc_insert_import", {"file_path": EXPORT_DIR + "/bracket_export.step"}, _imported, None),
]


# --- the Machining Extension's own strategies, on a drafted cameo ------------------------------
# The rail and surface strategies machine a DRAFTED wall, and the bracket has none - so
# they ride a cameo fixture of their own, in the same document: a 60 x 40 mm rectangle extruded
# 25 mm at -12 deg, which leans every wall in over the rise and leaves a 49.4 x 29.4 mm top face.
# The layout pass deals the component its own cell and carries every world point in these rows with
# it, so the numbers below are where the geometry is AUTHORED, not where it ends up.
_SW_COMP = "SwarfFrustum"
_SW_SKETCH = "FrustumSketch"
_SW_SETUP = "SwarfSetup"
_SW_SETUP2 = "SwarfSetup2"
# Authored clear of the origin band: a chunk whose footprint reaches the origin is PINNED where it
# was drawn, which here is on top of the part, the vise and the CAM stock.
_SW_CX, _SW_CY = 1200.0, 400.0
_SW_BASE_X, _SW_BASE_Y, _SW_H = 60.0, 40.0, 25.0
# tan(12 deg) x 25 mm - what each wall leans in over the rise, so the top face's own X edge is
# _SW_TOP_LEN and its Y edges sit _SW_INSET inside the base rectangle.
_SW_INSET = 5.314
_SW_TOP_LEN = _SW_BASE_X - 2 * _SW_INSET
# Swarf's 'otherSide' is relative to RAIL DIRECTION, not to a world side: which value cuts is a
# property of the pair that was selected, and this pair is the frustum's own -Y wall taken
# bottom-edge-first (measured on that pair: true cuts, and the other side links no passes at all).
# A run whose swarf toolpath comes back EMPTY is this constant reading the wrong way round.
_SW_CUT_SIDE = "true"

# The cameo's other setup, machining the same drafted block along an axis the bracket's own job
# never uses. It is kept OUT of the rail program's two setups: the multi-axis strategies are refused
# by the 3-axis post that program is written with.
_MX_SETUP = "MultiAxisSetup"     # the extension's simultaneous strategies
# The mill-turn machine and the enum name a turning setup reads back, both addressed by the hub's
# own lathe job - the turning families ride a solid of revolution, not a drafted block.
_TURN_MACHINE = "Brother SPEEDIO M300Xd1"
# Setup.operationType crosses the wire as the API's own enum NAME, not the create call's key.
_TURNING_TYPE = "TurningOperation"


# The dihedral bench beside the cameo: an L prism, 60 x 40 outer with 20 mm arms, 20 mm tall. Its
# 18 edges are 17 convex and the single vertical one at the reflex corner, which is what makes a
# hand-counted census possible at all.
_L_COMP = "DihedralL"
_L_SKETCH = "DihedralLSketch"
_L_X0, _L_Y0 = 1200.0, 600.0
_L_LONG, _L_SHORT, _L_ARM, _L_H = 60.0, 40.0, 20.0, 20.0


def _concave_filtered(convex, concave):
    """model_fillet(edge_filter='concave'): the convex/concave/smooth census the filter chose from
    (an unclassifiable edge refuses the call instead), the count it requested, and the volume it
    MOVED. Filling a concave corner ADDS material, so a classifier reading the L's 17 convex
    edges as concave lands a negative delta here."""
    def check(p):
        delta = p.get("volume_delta_cm3")
        counts = {k: p.get(k) for k in ("edges_convex", "edges_concave", "edges_smooth",
                                        "edges_requested")}
        return _measured(f"{concave} of {convex + concave} edges read concave, and the fillet "
                         "added material",
                         dict(counts, faces_created=p.get("faces_created"),
                              volume_delta_cm3=delta),
                         counts["edges_convex"] == convex and counts["edges_concave"] == concave
                         and counts["edges_smooth"] == 0
                         and counts["edges_requested"] == concave
                         and _num(p.get("faces_created")) and p["faces_created"] >= 1
                         and _num(delta) and delta > 0)
    return check


def _frustum_measured(p):
    """model_inspect on the cameo: the drafted block's bounding box, which is its BASE rectangle and
    its rise - the extent the taper leaves alone, since it shrinks the top face and nothing else."""
    return _measured(f"frustum bbox {_SW_BASE_X} x {_SW_BASE_Y} x {_SW_H} mm",
                     {"x": p.get("x"), "y": p.get("y"), "z": p.get("z"), "units": p.get("units")},
                     _near(p.get("x"), _SW_BASE_X, 0.5) and _near(p.get("y"), _SW_BASE_Y, 0.5)
                     and _near(p.get("z"), _SW_H, 0.5))


def _line_edge(length, tol=0.5):
    """find_geometry(kind='line_edge'): the one edge nearest the point, MEASURED. Length is what
    separates the frustum's full-width base rail from the shorter top rail the draft leans in to -
    a nearest_to that landed on the wrong one of the two reads a different number here."""
    def check(p):
        matches = p.get("matches") or []
        m = matches[0] if matches else None
        return _measured(f"a {length} mm line edge (+/-{tol})",
                         {"matches": len(matches), "match": m},
                         bool(m) and m.get("kind") == "line_edge"
                         and _near(m.get("length"), length, tol))
    return check


def _planar_faces(count):
    """find_geometry(kind='planar_face') sorted from a point above the frustum: the top face and the
    four drafted walls. The block has six planar faces and the base is the farthest from that point,
    so a max_results of five is what leaves it out."""
    def check(p):
        ms = p.get("matches") or []
        return _measured(f"{count} planar faces above the base",
                         {"count": len(ms), "kinds": [m.get("kind") for m in ms]},
                         len(ms) == count and all(m.get("kind") == "planar_face" for m in ms))
    return check


def _face_facing(label, normal_reads):
    """find_geometry(kind='planar_face'): the ONE face found, told apart by its own outward normal.
    The cameo is dealt a cell by the layout pass, so a centroid would not travel with it - the
    normal is the reading that says which of the six planar faces answered."""
    def check(p):
        ms = p.get("matches") or []
        m = ms[0] if ms else {}
        n = m.get("normal")
        return _measured(f"one planar face {label}",
                         {"count": len(ms), "normal": n, "position": m.get("position")},
                         len(ms) == 1 and m.get("kind") == "planar_face"
                         and isinstance(n, list) and len(n) == 3 and normal_reads(n))
    return check


def _setup_row(setup, blocked_by, operation_type=None):
    """cam_get's setups slice: ONE setup's row, whose operationType, machine and blocked_by are read
    off the Setup rather than echoed from the call that made it. A blocker and a machine are
    complementary - the code IS 'no machine' - so both are asserted together."""
    def check(p):
        row = next((s for s in (p.get("setups") or []) if s.get("name") == setup), None)
        got = {k: (row or {}).get(k) for k in ("name", "operation_type", "machine", "blocked_by")}
        return _measured(f"'{setup}' reads blocked_by {list(blocked_by)}", got,
                         bool(row) and row.get("blocked_by") == list(blocked_by)
                         and bool(row.get("machine")) == (not blocked_by)
                         and (operation_type is None
                              or row.get("operation_type") == operation_type))
    return check


def _turning_stock(setup):
    """cam_get(include=['parameters'], setup=...): the turning setup's own stock mode and turning
    WCS origin, read off the setup's parameter rows. The expression is what the platform stored, so
    the mode is asserted as CARRIED rather than equal, and the origin row is reported as it reads."""
    def check(p):
        params = p.get("parameters") or {}
        rows = [r for section in (params.get("sections") or {}).values() for r in section]
        by = {r.get("name"): r.get("expression") for r in rows}
        origin = by.get("wcs_origin_turning")
        return _measured(f"'{setup}' turns a fixed cylinder off its own WCS",
                         {"job_stockMode": by.get("job_stockMode"), "wcs_origin_turning": origin,
                          "parameter_count": params.get("parameter_count")},
                         params.get("setup") == setup
                         and "fixedcylinder" in str(by.get("job_stockMode") or "").lower()
                         and isinstance(origin, str) and origin.strip() != "")
    return check


def _param_landed(name, fragment):
    """cam_edit_operation(parameters=...): the named parameter read BACK off the operation. The
    platform normalizes what it stores, so the read-back is asserted to CARRY the value rather than
    to equal it - a write that did not move an expression it was asked to move is an error there,
    never a row here."""
    def check(p):
        rows = p.get("changed") or []
        row = next((r for r in rows if r.get("name") == name), None)
        return _measured(f"operation parameter '{name}' reads back carrying '{fragment}'",
                         {"edited": p.get("edited"), "changed": rows},
                         p.get("edited") is True and bool(row)
                         and fragment in str(row.get("after")).lower())
    return check


def _param_value(name, value):
    """The same read-back on a NUMERIC parameter, compared for EQUALITY through the ONE
    _leading_number parse: the carry test above reads a stored '13' as carrying the 3 that was asked
    for, and a length read-back states its unit ('0.5 mm')."""
    def check(p):
        rows = p.get("changed") or []
        row = next((r for r in rows if r.get("name") == name), None)
        got = _leading_number((row or {}).get("after"))
        return _measured(f"operation parameter '{name}' reads back {value}",
                         {"edited": p.get("edited"), "changed": rows},
                         p.get("edited") is True and got is not None
                         and got == _leading_number(value))
    return check


_LEADING_NUMBER = re.compile(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")


def _leading_number(text):
    """The number a read-back opens with, any unit suffix dropped - None where it opens with
    none."""
    match = _LEADING_NUMBER.match(str(text))
    return float(match.group(1)) if match else None


def _reads_back(after, value):
    """True where a read-back states `value`: a number compared as a number even where the
    expression states its unit ('0.5mm'), anything else compared as text."""
    number = _leading_number(value)
    return (_leading_number(after) == number if number is not None
            else str(after).strip().lower() == str(value).strip().lower())


def _landed_in_one_call(values):
    """cam_edit_operation carrying several parameters at once: every one of {name: value} reads
    back what it was asked for. Nothing here is gated - the gated shape is _unlocked_in_one_call."""
    def check(p):
        rows = {r.get("name"): r for r in (p.get("changed") or [])}
        return _measured(f"{'/'.join(values)} landed in ONE call",
                         {"edited": p.get("edited"), "changed": p.get("changed")},
                         p.get("edited") is True and len(rows) >= len(values)
                         and all(_reads_back((rows.get(n) or {}).get("after"), v)
                                 for n, v in values.items()))
    return check


def _unlocked_in_one_call(switch, gated, value):
    """cam_edit_operation carrying BOTH a switch and the row it unlocks: the gated row reads back
    the value asked for and carries 'unlocked_here', which is the call saying it read isEditable
    false at the start and true once the switch had landed. The switch itself carries no such key."""
    def check(p):
        rows = {r.get("name"): r for r in (p.get("changed") or [])}
        gated_row = rows.get(gated) or {}
        return _measured(f"'{switch}' and '{gated}'={value} in ONE call, the gated row unlocked",
                         {"edited": p.get("edited"), "changed": p.get("changed")},
                         p.get("edited") is True and _reads_back(gated_row.get("after"), value)
                         and gated_row.get("unlocked_here") is True
                         and "unlocked_here" not in (rows.get(switch) or {}))
    return check


# The selection-property keys cam_compare_operations decodes and scales. An enum crossing as an int
# or a length crossing in internal cm is what these two sets catch.
_ENUM_KEYS = ("loopType", "sideType", "extensionType", "extensionMethod")
_LENGTH_KEYS = ("startExtensionLength", "endExtensionLength", "silhouetteTolerance",
                "minimumHoleDiameter", "minimumCornerRadius", "maximumCornerRadius",
                "minimumPocketDepth", "maximumPocketDepth")


def _compare_geometry(op_a, op_b):
    """cam_compare_operations: the parameter diff PLUS the geometry half - the selection sets read,
    every enum decoded to a spelling and every length scaled into the units the payload names."""
    def check(p):
        rows = [row for side in ("operation_a", "operation_b")
                for d in (p.get("geometry_differences") or [])
                if isinstance(d.get(side), dict)
                for row in (d[side].get("properties") or [])]
        enums = [(k, v) for row in rows for k, v in row.items() if k in _ENUM_KEYS]
        lengths = [(k, v) for row in rows for k, v in row.items() if k in _LENGTH_KEYS]
        return _measured(
            f"'{op_a}' vs '{op_b}': parameters differ and the geometry half reads decoded",
            {"difference_count": p.get("difference_count"),
             "geometry_difference_count": p.get("geometry_difference_count"),
             "geometry_units": p.get("geometry_units"),
             "enums": enums[:6], "lengths": lengths[:6]},
            p.get("operation_a") == op_a and p.get("operation_b") == op_b
            and p.get("difference_count", 0) >= 1
            and p.get("geometry_units") == "mm"
            and all(isinstance(v, str) for _k, v in enums)
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for _k, v in lengths))
    return check


def _params_counted(operation):
    """cam_get(include=['parameters']): the LISTED rows and the count of the ones the visible+enabled
    filter dropped, so the listed count is not read as the operation's whole set."""
    def check(p):
        params = p.get("parameters") or {}
        return _measured(f"'{operation}' parameters listed beside hidden_count",
                         {"parameter_count": params.get("parameter_count"),
                          "hidden_count": params.get("hidden_count")},
                         params.get("operation") == operation
                         and params.get("parameter_count", 0) >= 1
                         and params.get("hidden_count", 0) >= 1
                         and "hidden_count" in (params.get("note") or ""))
    return check


def _preset_applied(name):
    """cam_edit_operation(preset=...): 'preset' is Operation.toolPreset read BACK after the
    assignment (the tool errors when the name it reads does not match the preset it assigned), and
    'was_preset' is what the operation ran before - null when it ran none, which is a real answer."""
    def check(p):
        was = p.get("was_preset")
        return _measured(f"preset read-back (want '{name}')",
                         {"preset": p.get("preset"), "preset_index": p.get("preset_index"),
                          "was_preset": was},
                         p.get("preset") == name and _num(p.get("preset_index"))
                         and (was is None or isinstance(was, str)))
    return check


def _rails_applied(p):
    """cam_select_geometry(chain) routed to a SWARF operation's rail pair: one CurveSelection per
    rail, the open/closed state read off the collection the OPERATION hands back, the drive mode
    engaged in the same call, and the order the references were taken in - which is published
    because getting it wrong fails silently (an upper-first pair generates valid and EMPTY)."""
    return _measured("swarf rails applied and the drive mode engaged",
                     {"selections": p.get("selections"), "rails_open": p.get("rails_open"),
                      "swarf_engaged": p.get("swarf_engaged"), "swarf_mode": p.get("swarf_mode"),
                      "rails_order": p.get("rails_order")},
                     p.get("selections") == 2 and p.get("rails_open") is True
                     and p.get("swarf_engaged") is True and p.get("swarf_mode") == "contours"
                     and bool(p.get("rails_order")))


def _edges_applied(count):
    """The SAME 'chain' selection routed to a drive input that is not a rail pair: one selection over
    every edge passed, and none of the rail keys - there is no pair order to publish and no mode to
    engage. The absent keys are what tell the two routes apart from the payload alone."""
    def check(p):
        return _measured(f"{count} edge(s) applied, no rail pair",
                         {"selections": p.get("selections"), "rails_order": p.get("rails_order"),
                          "swarf_engaged": p.get("swarf_engaged"), "selected": p.get("selected")},
                         p.get("selections") == count and "rails_order" not in p
                         and "swarf_engaged" not in p)
    return check


def _surfaces_applied(count, target, param):
    """cam_select_geometry(surfaces): the face count read BACK off the surface-set parameter after
    the assignment, beside WHICH set it landed on - an operation can carry several at once, so the
    role is named rather than guessed."""
    def check(p):
        return _measured(f"{count} face(s) on the '{target}' surface set",
                         {"selections": p.get("selections"),
                          "surface_target": p.get("surface_target"),
                          "surface_param": p.get("surface_param")},
                         p.get("selections") == count and p.get("surface_target") == target
                         and p.get("surface_param") == param)
    return check


def _scoped_body_selected(qualified):
    """cam_select_geometry(silhouette) with a component SCOPE narrowing a shared body name:
    'selected' is the qualified '<occurrence>:<body>' spelling of the entity that reached
    inputGeometry, so it says WHICH of the design's several 'Body1' bodies is being machined -
    which the count of selections cannot. setup_models_selected false is the same read the
    unscoped by-name branch takes: named bodies, not the setup's own models."""
    def check(p):
        return _measured(f"the scoped selection machines '{qualified}'",
                         {"selections": p.get("selections"), "selected": p.get("selected"),
                          "setup_models_selected": p.get("setup_models_selected")},
                         p.get("selected") == f"'{qualified}'"
                         and _num(p.get("selections")) and p["selections"] >= 1
                         and p.get("setup_models_selected") is False)
    return check


def _time_row(p, setup, name):
    """One operation's row in a cam_get(include=['time']) payload, or None. The slice reports per
    setup and then per operation, so both names have to resolve."""
    recs = ((p.get("time") or {}).get("setups") or [])
    rec = next((r for r in recs if r.get("setup") == setup), None)
    if not rec:
        return None
    return next((r for r in (rec.get("operations") or []) if r.get("operation") == name), None)


def _cuts(setup, key):
    """The NON-EMPTY oracle for one operation: its own getMachiningTime figure, above zero.

    hasToolpath cannot answer this - an operation that generated an EMPTY toolpath reads it TRUE
    (measured on a swarf operation warning 'No passes to link. / Generated toolpath is empty.':
    state 0, hasToolpath true, isToolpathValid true, 0.0 s, and 4.19 s once the same operation
    really cut). An empty operation is named on its own row here with no figure at all, so a
    machining_time_seconds above zero is the whole claim."""
    def check(p):
        name = _RECALL.get(key)
        row = _time_row(p, setup, name)
        secs = (row or {}).get("machining_time_seconds")
        return _measured(f"{name!r} cuts (machining time above zero)",
                         {"row": row},
                         name is not None and row is not None and _num(secs) and secs > 0)
    return check


def _first_generation(p):
    """cam_get_status on the swarf setup, read once the act-boundary poll has certified completion:
    the per-state tally and the health counts the lists beside it are drawn from.

    The empty list is read as a NEGATIVE here because otherSide is set to the cutting side before
    this generate, so all three operations are expected to cut: no operation is named empty, and the
    swarf operation in particular is not. That is the reading hasToolpath cannot give - it is TRUE
    on an operation whose toolpath generated empty - and the key has to be PRESENT to say so, or a
    payload that stopped publishing it would pass by absence."""
    named = p.get("empty_toolpaths")
    counts = p.get("counts") or {}
    swarf = _RECALL.get("swarf_op")
    return _measured("the swarf setup's generation completed with nothing cutting air",
                     {"completed": p.get("completed"), "live_states": p.get("live_states"),
                      "counts": p.get("counts"), "empty_toolpaths": named,
                      "swarf_op": swarf, "health_scope": p.get("health_scope")},
                     p.get("completed") is True and isinstance(counts, dict)
                     and isinstance(p.get("live_states"), dict)
                     and isinstance(named, list) and named == []
                     and counts.get("empty_toolpaths") == 0
                     and swarf is not None and swarf not in named)


def _suppressed_census(setup, suppressed):
    """cam_get(include=['operations']): the states tally and active_count PARTITION one row set -
    active_count is that same tally less its 'suppressed' bucket - so a suppression has to show in
    both halves at once, or the two numbers are describing different sets of operations."""
    def check(p):
        recs = ((p.get("operations") or {}).get("setups") or [])
        rec = next((r for r in recs if r.get("setup") == setup), None)
        summary = (rec or {}).get("summary") or {}
        states = summary.get("states") or {}
        active = summary.get("active_count")
        return _measured(f"'{setup}' census (want {suppressed} suppressed)",
                         {"states": states, "active_count": active},
                         states.get("suppressed") == suppressed and _num(active)
                         and sum(states.values()) == active + states["suppressed"])
    return check


def _posted_setups(names):
    """cam_post(setups=[...]): ONE NC program spanning several setups. 'scope_setups' is what was
    asked for; 'program_operation_count' and 'membership_verified' come from the program's own
    stored operations re-read after the post, so they say what the program HOLDS, and
    'posted_operations' how many of those carry a toolpath; every file row is stat'd on disk,
    because the API's success bool can precede an empty file."""
    def check(p):
        files = p.get("files") or []
        return _measured(f"one program over the setups {names}",
                         {"posted": p.get("posted"), "scope": p.get("scope"),
                          "scope_setups": p.get("scope_setups"),
                          "membership_verified": p.get("membership_verified"),
                          "program_operation_count": p.get("program_operation_count"),
                          "posted_operations": p.get("posted_operations"),
                          "file_count": p.get("file_count"), "files": files},
                         p.get("posted") is True and p.get("scope") == "setups"
                         and p.get("scope_setups") == list(names)
                         and p.get("membership_verified") is True
                         and _num(p.get("program_operation_count"))
                         and p["program_operation_count"] >= 2
                         and _num(p.get("posted_operations"))
                         and 1 <= p["posted_operations"] <= p["program_operation_count"]
                         and p.get("file_count") == len(files) and len(files) >= 1
                         and all(_num(f.get("size_bytes")) and f["size_bytes"] > 0 for f in files))
    return check


def _posted_turning(setup, program):
    """cam_post(post_scope='fusion'): a program posted with a post resolved from the library this
    installation ships, whose url names turning - 'post_scope' and 'post_config' are what say so.
    At least one operation posted and every file row is stat'd on disk; the G-code is not read."""
    def check(p):
        files = p.get("files") or []
        return _measured(f"'{setup}' posted with a shipped turning post",
                         {"posted": p.get("posted"), "scope": p.get("scope"),
                          "program_name": p.get("program_name"),
                          "post_scope": p.get("post_scope"), "post_config": p.get("post_config"),
                          "posted_operations": p.get("posted_operations"),
                          "file_count": p.get("file_count"), "files": files},
                         p.get("posted") is True and p.get("scope") == "setup"
                         and p.get("program_name") == program
                         and p.get("post_scope") == "fusion"
                         and "turning" in str(p.get("post_config") or "").lower()
                         and _num(p.get("posted_operations")) and p["posted_operations"] >= 1
                         and p.get("file_count") == len(files) and len(files) >= 1
                         and all(_num(f.get("size_bytes")) and f["size_bytes"] > 0 for f in files))
    return check


def _posted_as_is(name):
    """cam_post carrying nothing but 'program_name': the stored configuration posted untouched. The
    mode and the scope both say so, and files still land - an as-is post that wrote nothing would
    be a re-post in name only."""
    def check(p):
        return _measured(f"as-is re-post of program '{name}'",
                         {"posted": p.get("posted"), "mode": p.get("mode"),
                          "scope": p.get("scope"), "program_name": p.get("program_name"),
                          "file_count": p.get("file_count")},
                         p.get("posted") is True and p.get("mode") == "as_is"
                         and p.get("scope") == "as_is" and p.get("program_name") == name
                         and _num(p.get("file_count")) and p["file_count"] >= 1)
    return check


# ACT 7c: the cameo the extension strategies are machined on, built while Design is still the
# active workspace. The sketch and the component ride the sketch phase; what stays here is the
# drafted extrude and the measurement that says the fixture came out as authored.
_SWARF_RIG = [
    ("model_create_component", {"name": _SW_COMP, "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": _SW_SKETCH}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle",
                                           "x1": _SW_CX - _SW_BASE_X / 2,
                                           "y1": _SW_CY - _SW_BASE_Y / 2,
                                           "x2": _SW_CX + _SW_BASE_X / 2,
                                           "y2": _SW_CY + _SW_BASE_Y / 2}],
                             "sketch_name": _SW_SKETCH}, "ok", None),
    # a NEGATIVE taper leans every wall inward over the rise, so no wall is vertical and each one
    # is a ruled surface between its own bottom and top edge - which is what a rail pair is.
    ("model_extrude", {"sketch_name": _SW_SKETCH, "profile_index": 0, "distance": _SW_H,
                       "taper_deg": -12, "operation": "new"}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_inspect", {"target": _SW_COMP + ":1"}, _frustum_measured, None),
    # THE DIHEDRAL BENCH, built beside it: an L prism is the one shape whose edge census is known by
    # hand - 18 edges, of which exactly the one at the reflex corner is concave - so the edge_filter
    # path has a body to classify. Nothing later reads this body.
    ("model_create_component", {"name": _L_COMP, "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": _L_SKETCH}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "closed_path",
                                           "points": [[_L_X0, _L_Y0],
                                                      [_L_X0 + _L_LONG, _L_Y0],
                                                      [_L_X0 + _L_LONG, _L_Y0 + _L_ARM],
                                                      [_L_X0 + _L_ARM, _L_Y0 + _L_ARM],
                                                      [_L_X0 + _L_ARM, _L_Y0 + _L_SHORT],
                                                      [_L_X0, _L_Y0 + _L_SHORT]]}],
                             "sketch_name": _L_SKETCH}, "ok", None),
    ("model_extrude", {"sketch_name": _L_SKETCH, "profile_index": 0, "distance": _L_H,
                       "operation": "new"}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    # the FILTER path, which no other row drives: every fillet elsewhere hands in its own edge
    # handles. The census partitions the 18, the request is the concave one alone, and the volume
    # GREW - a filter that took the 17 convex edges instead would cut material away.
    ("model_fillet", {"body_name": _L_COMP + ":1:Body1", "edge_filter": "concave", "radius": 2},
     _concave_filtered(17, 1), None),
    # THE SMOOTH BRANCH, on the body that fillet just curved: the round replaced the reflex edge
    # with a cylindrical patch tangent to both walls, so the census now has to read 21 edges as 19
    # convex, no concave one left, and the two tangent joins SMOOTH. A filter matching nothing
    # publishes that census in its own refusal, so the read costs no second feature.
    ("model_fillet", {"body_name": _L_COMP + ":1:Body1", "edge_filter": "concave", "radius": 1},
     _refused("body has 21 edges", "19 convex, 0 concave, 2 smooth"), None),
]


# ACT 10c: the strategies the Machining Extension unblocks, on that cameo - what this installation
# is entitled to generate, the per-setup vocabulary that read is drawn from, and the three
# operations (swarf rails, a deburr chain, a geodesic driven by faces) generated in one launch.
_CAM_EXTENSION = [
    # the cameo, framed: the machining beats below all act on this one drafted block, and the field
    # it sits in is a metre wide.
    _watch(_SW_COMP + ":1"),
    # the document library's own count BEFORE the two tools this movement cuts with, so the indices
    # the operations select by hold whichever route the story's CAM act took.
    ("cam_edit_tools", {"action": "list", "scope": "document"},
     lambda p: _num(p.get("tool_count")) and p["tool_count"] >= 1,
     ("sw_mill", _recall("sw_mill", lambda p: p["tool_count"]))),
    ("cam_edit_tools", {"action": "add", "scope": "document",
                        "add_tools": [{"from_type": "flat end mill", "diameter": "12 mm",
                                       "presets": [{"name": "Alu Rough", "spindle_speed": 12000,
                                                    "feed": 2000}]},
                                      {"from_type": "ball end mill", "diameter": "6 mm"}]},
     lambda p: p.get("added") == 2 and p.get("tool_count") == _RECALL.get("sw_mill") + 2, None),
    # a number each: a sample clone keeps the sample's, and ONE program spans both this setup and
    # the cameo's second one - two tools sharing a number make the post refuse.
    ("cam_edit_tools", lambda c: {"action": "edit", "scope": "document",
                                  "tool": _ctx_get(c, "sw_mill", "the flat mill's index"),
                                  "parameters": {"tool_number": "7"}},
     _tool_param_landed("tool_number", "7"), None),
    ("cam_edit_tools", lambda c: {"action": "edit", "scope": "document",
                                  "tool": _ctx_get(c, "sw_mill", "the flat mill's index") + 1,
                                  "parameters": {"tool_number": "8"}},
     _tool_param_landed("tool_number", "8"), None),
    # the flute has to reach down the whole ruled height the rails span. A cutting-TOOL dimension is
    # edited in the library - the operation refuses that write, which is the beat below.
    ("cam_edit_tools", lambda c: {"action": "edit", "scope": "document",
                                  "tool": _ctx_get(c, "sw_mill", "the flat mill's index"),
                                  "parameters": {"tool_fluteLength": "32"}},
     _tool_param_landed("tool_fluteLength", "32"), None),
    ("cam_create_setup", {"models": [_SW_COMP], "name": _SW_SETUP},
     lambda p: p["created"] is True and p["setup_name"] == _SW_SETUP
     and p["operation_type"] == "milling" and p["model_count"] >= 1
     and p["operation_count"] == 0, None),
    # the library machine carries a simulation model and refuses assignment without the strip.
    ("cam_edit_setup", {"setup": _SW_SETUP, "machine": "Haas VF-2",
                        "machine_strip_simulation": True},
     lambda p: p.get("machine_set") == "Haas VF-2", None),
    ("cam_get", {"include": ["strategies"], "setup": _SW_SETUP},
     _offers(_SW_SETUP, "swarf", "deburr", "geodesic"), None),
    # REFUSED: a strategy this setup OFFERS but the installation will not generate. Creating it
    # would SUCCEED and then never generate, so the refusal names the flag it was read from.
    ("cam_create_operation", {"setup": _SW_SETUP, "strategy": "chamfer",
                              "tool_scope": "document", "tool_index": 0},
     _refused("isGenerationAllowed false in setup"), None),
    ("cam_create_operation", lambda c: {"setup": _SW_SETUP, "strategy": "swarf",
                                        "tool_scope": "document",
                                        "tool_index": _ctx_get(c, "sw_mill",
                                                               "the flat mill's index"),
                                        "generate": False},
     _op_created(_SW_SETUP, "swarf"),
     ("swarf_op", _recall("swarf_op", lambda p: p["operation"]))),
    # the named feeds/speeds recipe, pointed at while there is no toolpath to lose: assigning a
    # preset INVALIDATES a valid operation's toolpath.
    ("cam_edit_operation", lambda c: {"operation": _ctx_get(c, "swarf_op", "the swarf op"),
                                      "preset": "alu rough"},
     _preset_applied("Alu Rough"), None),
    # REFUSED: a preset the operation's own tool does not hold - listed, not resolved to a neighbour.
    ("cam_edit_operation", lambda c: {"operation": _ctx_get(c, "swarf_op", "the swarf op"),
                                      "preset": "NoSuchPresetXyz"},
     _refused("has no preset named", "Presets on this tool"), None),
    # REFUSED: the cutting-TOOL dimension on the OPERATION. The operation carries the parameter and
    # takes no write to it, and the refusal points at the library edit that does reach it.
    ("cam_edit_operation", lambda c: {"operation": _ctx_get(c, "swarf_op", "the swarf op"),
                                      "parameters": {"tool_fluteLength": "40"}},
     _refused("does not accept a write to", "isEditable reads False on each"), None),
    # the rails: the drafted wall's own bottom and top edges, LOWER first. The two are told apart by
    # length, since the draft leans the top one in by _SW_INSET at each end.
    ("find_geometry", {"target": _SW_COMP, "kind": "line_edge",
                       "nearest_to": [_SW_CX, _SW_CY - _SW_BASE_Y / 2, 0], "max_results": 1},
     _line_edge(_SW_BASE_X), _fg("sw_lower")),
    ("find_geometry", {"target": _SW_COMP, "kind": "line_edge",
                       "nearest_to": [_SW_CX, _SW_CY - _SW_BASE_Y / 2 + _SW_INSET, _SW_H],
                       "max_results": 1},
     _line_edge(_SW_TOP_LEN), _fg("sw_upper")),
    # REFUSED: ONE contour where the strategy's drive input is a PAIR. Measured, a single contour
    # is not refused by the platform - it reports 'Invalid contours.' at generate instead.
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "swarf_op", "the swarf op"),
                                       "selection": "chain",
                                       "handles": [_ctx_get(c, "sw_lower", "the lower rail")],
                                       "generate": False},
     _refused("is a RAIL PAIR"), None),
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "swarf_op", "the swarf op"),
                                       "selection": "chain",
                                       "handles": [_ctx_get(c, "sw_lower", "the lower rail"),
                                                   _ctx_get(c, "sw_upper", "the upper rail")],
                                       "generate": False},
     _rails_applied, None),
    ("cam_edit_operation", lambda c: {"operation": _ctx_get(c, "swarf_op", "the swarf op"),
                                      "parameters": {"otherSide": _SW_CUT_SIDE}},
     _param_landed("otherSide", _SW_CUT_SIDE), None),
    # the deburr, cut with the 6 mm BALL: the chamfer-mill sample's 2.1 mm flute fails 'Setting Tool
    # failed' on this strategy.
    ("cam_create_operation", lambda c: {"setup": _SW_SETUP, "strategy": "deburr",
                                        "tool_scope": "document",
                                        "tool_index": _ctx_get(c, "sw_mill",
                                                               "the flat mill's index") + 1,
                                        "generate": False},
     _op_created(_SW_SETUP, "deburr"),
     ("deburr_op", _recall("deburr_op", lambda p: p["operation"]))),
    ("find_geometry", {"target": _SW_COMP, "kind": "line_edge",
                       "nearest_to": [_SW_CX, _SW_CY + _SW_BASE_Y / 2 - _SW_INSET, _SW_H],
                       "max_results": 1},
     _line_edge(_SW_TOP_LEN), _fg("sw_top_edge")),
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "deburr_op", "the deburr op"),
                                       "selection": "chain",
                                       "handles": [_ctx_get(c, "sw_top_edge", "the top edge")],
                                       "generate": False},
     _edges_applied(1), None),
    # the deburr's MULTI-PASS in ONE call: the stepover row reads isEditable False until the flag
    # above it is true, so it is written LAST and its flag re-read once the switch has landed. The
    # gated row is named FIRST here, which is the order a request-order apply would refuse on.
    ("cam_edit_operation", lambda c: {"operation": _ctx_get(c, "deburr_op", "the deburr op"),
                                      "parameters": {"numberOfStepovers": "3",
                                                     "doMultiplePasses": "true"}},
     _unlocked_in_one_call("doMultiplePasses", "numberOfStepovers", 3), None),
    # the faces the geodesic is driven by, sorted from a point above the frustum: the top face and
    # the four drafted walls, with the base face the farthest of the six and so the one left out.
    ("find_geometry", {"target": _SW_COMP, "kind": "planar_face",
                       "nearest_to": [_SW_CX, _SW_CY, 60], "max_results": 5},
     _planar_faces(5), _fgn("sw_faces")),
    ("cam_create_operation", lambda c: {"setup": _SW_SETUP, "strategy": "geodesic",
                                        "tool_scope": "document",
                                        "tool_index": _ctx_get(c, "sw_mill",
                                                               "the flat mill's index") + 1,
                                        "generate": False},
     _op_created(_SW_SETUP, "geodesic"),
     ("geodesic_op", _recall("geodesic_op", lambda p: p["operation"]))),
    # REFUSED: a surface ROLE this operation does not carry - the refusal names the sets it does,
    # rather than dropping the faces onto whichever set happens to exist. Nothing is written.
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "geodesic_op", "the geodesic op"),
                                       "selection": "surfaces",
                                       "handles": _ctx_get(c, "sw_faces", "the frustum faces"),
                                       "surface_target": "floor", "generate": False},
     _refused("It carries drive"), None),
    # REFUSED: the same selection on an operation whose ONE surface set is the deprecated
    # checkSurfaceSelection, measured isEditable False on a deburr op - so the refusal is the
    # not-settable branch, naming the parameter and the read it was left out on.
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "deburr_op", "the deburr op"),
                                       "selection": "surfaces",
                                       "handles": _ctx_get(c, "sw_faces", "the frustum faces"),
                                       "generate": False},
     _refused("carries no SETTABLE surface set", "checkSurfaceSelection", "isEditable"), None),
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "geodesic_op", "the geodesic op"),
                                       "selection": "surfaces",
                                       "handles": _ctx_get(c, "sw_faces", "the frustum faces"),
                                       "surface_target": "drive", "generate": False},
     _surfaces_applied(5, "drive", "driveSurfaces"), None),
    # THE SIMULTANEOUS STRATEGIES, in a setup of their own: the rail program spans the swarf setup
    # and the cameo's plain milling one, and the 3-axis post it is written through refuses a 5-axis
    # toolpath - so these three ride a setup that program never reaches.
    ("cam_create_setup", {"models": [_SW_COMP], "name": _MX_SETUP},
     lambda p: p["created"] is True and p["setup_name"] == _MX_SETUP
     and p["operation_type"] == "milling" and p["model_count"] >= 1
     and p["operation_count"] == 0, None),
    ("cam_edit_setup", {"setup": _MX_SETUP, "machine": "Haas VF-2",
                        "machine_strip_simulation": True},
     lambda p: p.get("machine_set") == "Haas VF-2", None),
    ("cam_get", {"include": ["strategies"], "setup": _MX_SETUP},
     _offers(_MX_SETUP, "multiaxis_finishing", "multiaxis_roughing", "flow2"), None),
    # ONE drafted wall, taken from a point 8 mm off it: the -Y wall's own outward normal is what
    # tells it from the base and the top, whose centroids sit far further from that point.
    ("find_geometry", {"target": _SW_COMP, "kind": "planar_face",
                       "nearest_to": [_SW_CX, _SW_CY - _SW_BASE_Y / 2 - 8, _SW_H / 2 + 2],
                       "max_results": 1},
     _face_facing("leaning out on -Y", lambda n: n[1] < -0.9 and abs(n[2]) > 0.15),
     _fg("sw_wall")),
    # and the block's top, which is the roughing pass's floor - the one planar face reading +Z.
    ("find_geometry", {"target": _SW_COMP, "kind": "planar_face",
                       "nearest_to": [_SW_CX, _SW_CY, _SW_H + 10], "max_results": 1},
     _face_facing("facing +Z", lambda n: n[2] > 0.999), _fg("sw_top_face")),
    # the simultaneous FINISHING pass, driven by that wall as its floor set - the surface role this
    # strategy reads, beside the drive set the geodesic above takes.
    ("cam_create_operation", lambda c: {"setup": _MX_SETUP, "strategy": "multiaxis_finishing",
                                        "tool_scope": "document",
                                        "tool_index": _ctx_get(c, "sw_mill",
                                                               "the flat mill's index") + 1,
                                        "generate": False},
     _op_created(_MX_SETUP, "multiaxis_finishing"),
     ("mafin_op", _recall("mafin_op", lambda p: p["operation"]))),
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "mafin_op",
                                                             "the multi-axis finishing op"),
                                       "selection": "surfaces",
                                       "handles": [_ctx_get(c, "sw_wall", "the drafted wall")],
                                       "surface_target": "floor", "generate": False},
     _surfaces_applied(1, "floor", "floorSurfaces"), None),
    # the ROUGHING pass on the same floor set, floored on the block's top rather than a wall.
    ("cam_create_operation", lambda c: {"setup": _MX_SETUP, "strategy": "multiaxis_roughing",
                                        "tool_scope": "document",
                                        "tool_index": _ctx_get(c, "sw_mill",
                                                               "the flat mill's index"),
                                        "generate": False},
     _op_created(_MX_SETUP, "multiaxis_roughing"),
     ("marough_op", _recall("marough_op", lambda p: p["operation"]))),
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "marough_op",
                                                             "the multi-axis roughing op"),
                                       "selection": "surfaces",
                                       "handles": [_ctx_get(c, "sw_top_face", "the block's top")],
                                       "surface_target": "floor", "generate": False},
     _surfaces_applied(1, "floor", "floorSurfaces"), None),
    # and the FLOW, which drives off the same wall through the drive set instead.
    ("cam_create_operation", lambda c: {"setup": _MX_SETUP, "strategy": "flow2",
                                        "tool_scope": "document",
                                        "tool_index": _ctx_get(c, "sw_mill",
                                                               "the flat mill's index") + 1,
                                        "generate": False},
     _op_created(_MX_SETUP, "flow2"),
     ("flow_op", _recall("flow_op", lambda p: p["operation"]))),
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "flow_op", "the flow op"),
                                       "selection": "surfaces",
                                       "handles": [_ctx_get(c, "sw_wall", "the drafted wall")],
                                       "surface_target": "drive", "generate": False},
     _surfaces_applied(1, "drive", "driveSurfaces"), None),
    # the rails' three at once, so the act boundary's bounded poll certifies one generation rather
    # than three. The poll FAILs on an empty toolpath, which is what says the rails cut.
    ("cam_generate", {"target": _SW_SETUP, "skip_valid": False}, _launched_on(_SW_SETUP), None),
    # then the simultaneous setup, launched straight after: the boundary poll certifies each in
    # turn, and both launches sit behind every write this act makes.
    ("cam_generate", {"target": _MX_SETUP, "skip_valid": False}, _launched_on(_MX_SETUP), None),
]


# ACT 10b2: the cameo's SECOND setup - a plain milling job on the same drafted block, and the
# component SCOPE beats' home. Nothing here is an extension strategy, so it runs on any licence: it
# needs only the cameo ACT 8 builds and the CAM product either lane of ACT 10a leaves behind.
_CAM_SCOPE = [
    _watch(_SW_COMP + ":1"),
    # its own cutter, at the index the library's own count names, with a tool number no other tool
    # in program 1002 carries.
    ("cam_edit_tools", {"action": "list", "scope": "document"},
     lambda p: _num(p.get("tool_count")) and p["tool_count"] >= 1,
     ("scope_mill", _recall("scope_mill", lambda p: p["tool_count"]))),
    ("cam_edit_tools", {"action": "add", "scope": "document",
                        "add_tools": [{"from_type": "flat end mill", "diameter": "12 mm"}]},
     lambda p: p.get("added") == 1 and p.get("tool_count") == _RECALL.get("scope_mill") + 1, None),
    ("cam_edit_tools", lambda c: {"action": "edit", "scope": "document",
                                  "tool": _ctx_get(c, "scope_mill", "the scope mill's index"),
                                  "parameters": {"tool_number": "9"}},
     _tool_param_landed("tool_number", "9"), None),
    ("cam_create_setup", {"models": [_SW_COMP], "name": _SW_SETUP2},
     lambda p: p["created"] is True and p["setup_name"] == _SW_SETUP2
     and p["operation_count"] == 0, None),
    ("cam_edit_setup", {"setup": _SW_SETUP2, "machine": "Haas VF-2",
                        "machine_strip_simulation": True},
     lambda p: p.get("machine_set") == "Haas VF-2", None),
    ("cam_create_operation", lambda c: {"setup": _SW_SETUP2, "strategy": "contour2d",
                                        "tool_scope": "document",
                                        "tool_index": _ctx_get(c, "scope_mill",
                                                               "the scope mill's index"),
                                        "generate": False},
     _op_created(_SW_SETUP2, "contour2d"),
     ("setup2_op", _recall("setup2_op", lambda p: p["operation"]))),
    # A COMPONENT SCOPE on a shared body name, on this cameo's own job. 'Body1' is the first body of
    # every component in the design, so unscoped it is refused with the candidates and with the
    # input that narrows them; scoped, 'selected' names the body that landed in inputGeometry.
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "setup2_op",
                                                            "the second setup's op"),
                                       "selection": "silhouette", "bodies": ["Body1"],
                                       "generate": False},
     _refused("is ambiguous - it names", "pass the owning component as 'component'"), None),
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "setup2_op",
                                                            "the second setup's op"),
                                       "selection": "silhouette", "bodies": ["Body1"],
                                       "component": _SW_COMP, "generate": False},
     _scoped_body_selected(_SW_COMP + ":1:Body1"), None),
    ("cam_select_geometry", lambda c: {"operation": _ctx_get(c, "setup2_op",
                                                            "the second setup's op"),
                                       "selection": "silhouette", "bodies": [_SW_COMP],
                                       "generate": False},
     lambda p: p["setup_models_selected"] is False, None),
    ("cam_generate", {"target": _SW_SETUP2, "skip_valid": False}, _launched_on(_SW_SETUP2), None),
]


# ACT 10d: THE SECOND SETUP ON THE PART - the bracket turned over and machined from underneath,
# with a WCS of its own. It opens with the two reads taken on the finished rail job, which the
# extension act's own capability gates: where the extension is not entitled those two rows do not
# run, and the flip setup below them does.
_CAM_SECOND_SETUP = [
    ("cam_get_status", {"target": _SW_SETUP}, _needs(MACHINING_EXTENSION, _first_generation), None),
    # THE NON-EMPTY ORACLE, per operation: the rails cut only on one side of their own direction,
    # and machining time is the only read that tells a cutting toolpath from an empty one.
    ("cam_get", {"include": ["time"], "setup": _SW_SETUP},
     _needs(MACHINING_EXTENSION, _cuts(_SW_SETUP, "swarf_op")), None),
    # the same oracle over the simultaneous setup: every one of its three operations carries its own
    # machining time above zero, so none of them generated into air.
    ("cam_get", {"include": ["time"], "setup": _MX_SETUP},
     _needs(MACHINING_EXTENSION, _all_cut(_MX_SETUP, 3)), None),
    # back to the real part for the flip: a second setup on the SAME model, cut from the other
    # side, which is how a part with features on two faces is actually run.
    _watch([PART_COMP + ":1"]),
    # its own origin, and a Joint Origin rather than a coordinate: the first setup's WCS is bound
    # to the STOCK's centre, and a second setup that shared it would be the same fixture twice.
    ("joint_create_origin", {"anchor": "bbox_center", "bbox_target": PART_COMP + ":1",
                             "orient_axis": "z", "name": FLIP_WCS},
     _joint_origin_computed(FLIP_WCS), None),
    # EVERY reference to the part from here on is its fullPathName, not its component name: ACT
    # 10b re-imports the exported STEP, so a SECOND component named 'Bracket' stands in the design
    # by now and a bare name reaches both. 'Bracket:1' is the occurrence this story machined.
    ("cam_create_setup", {"models": [PART_COMP + ":1"], "name": FLIP_SETUP},
     lambda p: p["created"] is True and p["setup_name"] == FLIP_SETUP
     and p["operation_type"] == "milling" and p["model_count"] >= 1
     and p["operation_count"] == 0, None),
    ("cam_edit_setup", {"setup": FLIP_SETUP, "stock": [STOCK_COMP],
                        "fixtures": [VISE_BASE, JAW_FIXED, JAW_MOVING]},
     _setup_bodies(stock=1, fixtures=3), None),
    ("cam_edit_setup", {"setup": FLIP_SETUP, "wcs": {"origin": FLIP_WCS}},
     lambda p: bool(p["wcs_set"]["origin"]["bound_entities"]), None),
    ("cam_edit_setup", {"setup": FLIP_SETUP, "machine": "Haas VF-2",
                        "machine_strip_simulation": True},
     lambda p: p.get("machine_set") == "Haas VF-2", None),
    ("cam_get", {"include": ["strategies"], "setup": FLIP_SETUP},
     _offers(FLIP_SETUP, "face", "contour2d"), None),
    # THE UNDERSIDE: the face the flip exists to reach, measured before it is faced. 'nearest_to'
    # answers with the nearest face whether or not it is the one meant, so the row asserts the
    # centroid AND a downward normal - the pair is what separates the bottom from the top, which
    # shares its centroid in x and y.
    ("cam_create_operation", {"setup": FLIP_SETUP, "strategy": "face", "name": _FLIP_FACE_OP,
                              "tool_scope": "document", "tool_index": _FACE_MILL,
                              "generate": False},
     _op_named(FLIP_SETUP, "face", _FLIP_FACE_OP), None),
    ("find_geometry", {"target": PART_COMP + ":1", "kind": "planar_face",
                       "nearest_to": [0, 0, -60], "max_results": 1},
     _face_down_at(0, 0, 0, tol=2.0), _fg("part_bottom")),
    ("cam_select_geometry", lambda c: {"operation": _FLIP_FACE_OP, "selection": "face",
                                       "handles": [_ctx_get(c, "part_bottom", "the part's underside")],
                                       "generate": False}, _selected(1), None),
    # and the contour round what the flip exists to reach - the mounting pattern's backsides, taken
    # off the part's silhouette from this side. Zero handles, so it runs against the bodies set as
    # the SETUP'S models: the one reference the re-imported twin cannot reach, since the setup was
    # told which occurrence it machines when it was created.
    ("cam_create_operation", {"setup": FLIP_SETUP, "strategy": "contour2d", "name": _FLIP_BACK_OP,
                              "tool_scope": "document", "tool_index": _FLAT_MILL,
                              "generate": False},
     _op_named(FLIP_SETUP, "contour2d", _FLIP_BACK_OP), None),
    ("cam_select_geometry", {"operation": _FLIP_BACK_OP, "selection": "silhouette",
                             "generate": False},
     lambda p: p["setup_models_selected"] is True, None),
    ("cam_generate", {"target": FLIP_SETUP, "skip_valid": False}, _launched_on(FLIP_SETUP), None),
]


# ACT 10e: the deliverable ONE program over TWO setups, and the census that partitions the rows a
# suppression takes out of it. The rail half of it rides the capability tier; the part's own two
# setups are posted whatever this installation is entitled to, so the sweep always ends on a post.
_CAM_MULTI_POST = [
    # REFUSED: the rail toolpath posted through a 3-axis machine configuration. The refusal carries
    # the platform's own cause, and it is what makes the suppression below a step with a reason
    # rather than a precaution. Only the cause is asserted - the rollback's own sentence is a
    # separate claim about a delete this beat does not read.
    ("cam_post", {"setups": [_SW_SETUP, _SW_SETUP2], "post": "haas", "post_scope": "local",
                  "output_folder": EXPORT_DIR + "/nc", "program_name": "1002"},
     _needs(MACHINING_EXTENSION,
            _refused("requires a machine configuration for 5-axis simultaneous toolpath")), None),
    # so the rail toolpath is parked, and the program below is written from what is left.
    ("cam_edit_operation", lambda c: {"operation": _ctx_get(c, "swarf_op", "the swarf op"),
                                      "suppressed": True},
     _needs(MACHINING_EXTENSION,
            lambda p: p["is_suppressed"] is True and p["was_suppressed"] is False
            and p["had_toolpath"] is True and p["has_toolpath"] is False), None),
    ("cam_get", {"include": ["operations"], "setup": _SW_SETUP},
     _needs(MACHINING_EXTENSION, _suppressed_census(_SW_SETUP, 1)), None),
    ("cam_post", {"setups": [_SW_SETUP, _SW_SETUP2], "post": "haas", "post_scope": "local",
                  "output_folder": EXPORT_DIR + "/nc", "program_name": "1002"},
     _needs(MACHINING_EXTENSION, _posted_setups([_SW_SETUP, _SW_SETUP2])), None),
    # THE PART'S OWN TWO SETUPS, in ONE program - the last thing this sweep does, and the one that
    # runs on any licence: the job of ACT 10a and the flip of ACT 10d written to a single Haas
    # file. The three cam_post shapes below - the multi-setup post, the reconfigure refusal and the
    # as-is re-post - are the tool's whole contract, so they ride this program rather than the
    # rail one, which the capability tier can hold back.
    ("cam_post", {"setups": [CAM_SETUP, FLIP_SETUP], "post": "haas", "post_scope": "local",
                  "output_folder": EXPORT_DIR + "/nc", "program_name": "2001"},
     _posted_setups([CAM_SETUP, FLIP_SETUP]), None),
    # REFUSED: reconfiguring that program down to ONE of its setups would overwrite operations a
    # machinist may have curated, and the refusal names every input to omit to re-post it as stored.
    ("cam_post", {"setups": [CAM_SETUP], "post": "haas", "post_scope": "local",
                  "output_folder": EXPORT_DIR + "/nc", "program_name": "2001"},
     _refused("already exists", "Omit 'scope', 'setups', 'post', and 'output_folder'"), None),
    ("cam_post", {"program_name": "2001"}, _posted_as_is("2001"), None),
    # THE TREE THE RUN LEAVES BEHIND. Both programs are written, so the rail toolpath comes back off
    # its park and every setup the job's later edits left stale is relaunched. Only setups whose
    # every operation read a selection back are named: a blocked one parks the process on a modal.
    ("cam_edit_operation", lambda c: {"operation": _ctx_get(c, "swarf_op", "the swarf op"),
                                      "suppressed": False},
     _needs(MACHINING_EXTENSION,
            lambda p: p["is_suppressed"] is False and p["was_suppressed"] is True), None),
    ("cam_generate", {"target": CAM_SETUP, "skip_valid": True}, _relaunched(CAM_SETUP), None),
    ("cam_generate", {"target": "Setup2", "skip_valid": True}, _relaunched("Setup2"), None),
    ("cam_generate", {"target": _SW_SETUP, "skip_valid": True},
     _needs(MACHINING_EXTENSION, _relaunched(_SW_SETUP)), None),
    ("cam_generate", {"target": _MX_SETUP, "skip_valid": True},
     _needs(MACHINING_EXTENSION, _relaunched(_MX_SETUP)), None),
]


def _all_current(scope, exactly=None):
    """cam_get_status once the boundary poll certified the relaunch: the CAM browser a watcher is
    left with, asserted rather than described - nothing out of date, errored or parked, and every
    operation valid. An EMPTY scope cannot satisfy that by arithmetic (0 stale, 0 errored, 0 valid
    of 0), so a count is required; 'exactly' pins it where the act knows what the scope must hold."""
    def check(p):
        live = p.get("live_states") or {}
        total = live.get("total")
        return _measured(f"{scope} holds {exactly or 'at least one'} operation(s), every one valid",
                         {"completed": p.get("completed"), "live_states": live,
                          "readiness": p.get("readiness")},
                         p.get("completed") is True and live.get("out_of_date") == 0
                         and live.get("errored") == 0 and live.get("suppressed") == 0
                         and live.get("setups_errored") == 0
                         and _num(total) and total >= 1
                         and (exactly is None or total == exactly)
                         and live.get("valid") == total)
    return check


# ACT 10f: the last CAM step - the state the browser is left in, read off the document itself and
# off the setup whose operations arrived tool-less.
_CAM_GREEN = [
    # Setup2's count is PINNED: the run's own template lands one operation there and the shipped
    # bundle three, so a bundle that landed fewer reddens here rather than passing on an all-valid
    # tally of whatever survived.
    ("cam_get_status", {"target": "Setup2"}, _all_current("setup 'Setup2'", exactly=4), None),
    ("cam_get_status", {}, _all_current("the document"), None),
]
