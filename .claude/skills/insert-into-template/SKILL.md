---
name: insert-into-template
description: >-
  Use when the user asks to insert/place/drop/load a design or part into a template (or
  "windowframe template", "CAM template", "machining template"), to set up a CAM job for a
  part, or to "insert this into the template". Stands up a CAM job for a new part: saves the
  active CAD into the data model, defines a "Center of Model" part-space origin oriented to a
  machining axis the operator picks, places the shop's CAM template beside it (named
  <model>_CAM), inserts the part into the template's model component (the slot the real part
  swaps into), positions it, sizes the stock from the measured part, and regenerates the
  toolpaths so the job leaves current, not stale. A repeatable, team-owned procedure that
  chains fusion-essentials tool calls - it runs without asking the operator for approval; the
  only human step is the machining face, which the skill proposes from measured geometry for a
  yes/no and falls back to a click in Fusion. Edit the CONFIGURATION block below to adapt
  it to your shop. Requires the fusion-essentials MCP server.
allowed-tools: >-
  fusion-essentials:workspace_orient
  fusion-essentials:doc_get
  fusion-essentials:data_get
  fusion-essentials:design_get
  fusion-essentials:cam_get
  fusion-essentials:param_get
  fusion-essentials:param_set
  fusion-essentials:sys_get_selection
  fusion-essentials:sys_request_selection
  fusion-essentials:find_geometry
  fusion-essentials:joint_create_origin
  fusion-essentials:model_inspect
  fusion-essentials:doc_save
  fusion-essentials:doc_save_as
  fusion-essentials:doc_open
  fusion-essentials:doc_insert_occurrence
  fusion-essentials:doc_update_xref
  fusion-essentials:assembly_ground
  fusion-essentials:assembly_get
  fusion-essentials:joint_create
  fusion-essentials:sketch_set_text
  fusion-essentials:cam_set_nc_comment
  fusion-essentials:cam_generate
  fusion-essentials:cam_get_status
  fusion-essentials:view_switch_workspace
  fusion-essentials:view_screenshot
  fusion-essentials:view_screenshot_multi
---

# Insert a new part into a CAM template

A team-owned procedure that stands up a CAM job from a new CAD part as one chain of
fusion-essentials tool calls. Run the phases in order; each numbered step is a tool call whose
output feeds a later step - record the named values and pass them on verbatim. Phase 0 is a HUB
GATE: the shop's template library is hub-scoped, so it is proved to resolve before anything is
saved. The single human input is the machining face: Phase 1 proposes one from measured geometry
and asks for a yes/no, and Phase 2's pick in Fusion is the fallback whenever that proposal is
declined or ambiguous - by `AskUserQuestion` in chat, or by `sys_request_selection` for an agent
that holds no AskUserQuestion. Everything else runs without approval round-trips.
If a step's stated expectation does not hold, STOP and report the failing value - do not
improvise around it, and do not drop to sys_execute_script (a missing capability is a tool-surface
gap to report, not to script around). Template methodology background is in
[reference.md](reference.md).

## CONFIGURATION (edit these for your shop)

```
# Destination when the CAD is NOT already saved (a saved part's own folder wins otherwise).
DEFAULT_PROJECT      = "CAM"
DEFAULT_FOLDER       = "{model}"      # "{model}" expands to the CAD's name

# The team's template library: ONE folder holding the published templates. The skill uses ONLY
# documents from this folder. DEFAULT_TEMPLATE is used when the operator does not name one.
TEMPLATE_LIBRARY_PROJECT = "CAM"
TEMPLATE_LIBRARY_FOLDER  = "Workflow Templates"
DEFAULT_TEMPLATE         = "4th Axis Windowframe Template"

# Naming.
TEMPLATE_NAME_SUFFIX = "_CAM"         # the copy is named "<model>_CAM"
NAMEPLATE_SKETCH     = "File_Name"    # sketch whose text gets the model name (missing = skipped)

# Template wiring - the shop's naming convention INSIDE its templates. A pinned name is
# VERIFIED against the document reads (present or the run stops); leave "" to infer from the
# reads instead, with any ambiguity settled by a structured question - never a guess.
SETUP                = ""                            # "" = active milling setup, else first
PLACEHOLDER          = "Placeholder model"           # the slot occurrence the part replaces
ATTACH_JO            = "Attach Center of Workpiece"  # root JO the part joins to

# OPTIONAL stock parameters. If the template defines these user parameters the measured part
# size is written to them; if absent, stock sizing is skipped and the part still inserts.
PART_PARAMS          = ["PartX", "PartY", "PartZ"]

# How the machining face is chosen (Phase 1).
#   "propose" - the skill ranks the faces itself and asks the operator to confirm ONE proposal
#   "auto"    - the same rank, and it proceeds WITHOUT asking when the rank is unambiguous by
#               AUTO_MARGIN below; the proposal is still reported
#   "manual"  - Phase 1 is skipped entirely; the operator clicks the face in Fusion (Phase 2)
AUTO_PICK            = "propose"
AUTO_MIN_AREA_FRAC   = 0.25   # a candidate face's area, as a fraction of the largest bbox face
AUTO_MARGIN          = 2.0    # "auto" needs the top axis to score >= this many x the runner-up
EVIDENCE_DIR         = ""     # "" = inline images only; a directory also writes one PNG per view
```

## Phase 0 - Hub gate (READ, before anything is saved)

Projects and lineage URNs are hub-scoped: the template library resolves in one hub and not in
another. One read proves it before anything is saved.

1. `data_get(project=TEMPLATE_LIBRARY_PROJECT, folder=TEMPLATE_LIBRARY_FOLDER, recursive=false)` -
   the same listing Phase 5 step 1 resolves the template from. A row in `files` whose `name` matches
   the named template (or `DEFAULT_TEMPLATE`), case-insensitively, passes the gate. `Project not
   found: <name>. Available: ...`, a folder miss, or no matching name = STOP. `truncated` /
   `time_truncated` true with no match is an incomplete read, not an absent template - say so.
2. On a STOP: report the active hub (`data_get()` -> `active_hub`) and what did not resolve in it,
   then leave the switch to the operator. A hub switch CLOSES every open document, and URNs recorded
   before it stop resolving after it, so the skill never calls `data_switch_hub` itself. Do not name
   the hub the template is in - no read here sees another hub's contents.

## Phase 1 - Propose the machining face (READ)

`AUTO_PICK = "manual"` -> skip this phase entirely and run Phase 2 as written. Otherwise the skill
proposes ONE face from measured geometry and the operator confirms it in the conversation. Every
exit from this phase that is not a confirmed proposal runs Phase 2 exactly as written - that pick
stays the fallback, unchanged.

1. `workspace_orient` + `design_get(include=['tree'], tree_bodies=true)` - the part's bodies. From
   `root_bodies` plus each tree node's `bodies`, keep the rows with `is_solid` true and `visible`
   true. EXPECT exactly ONE - it is the part, and `body_name` is its `name`. Zero, several, or any
   `bodies_truncated` / `root_bodies_truncated` flag set: this phase cannot name the part without
   guessing - report the body names and run Phase 2 (the operator's click names the body too).
2. `model_inspect(target=<body_name>, units="mm")` - the world-axis bounding box. `BBOX_FACE_MM2` =
   the product of the two LARGEST of `x` / `y` / `z`. Any of the three null -> run Phase 2.
3. `find_geometry(target=<body_name>, kind="planar_face", units="mm", max_results=100)` and
   `find_geometry(target=<body_name>, kind="cylinder_face", units="mm", max_results=100)`. In
   EITHER response `match_count` > `returned` means the rows are a SUBSET of the part: record
   `truncated=true` and treat `AUTO_PICK="auto"` as `"propose"` for this run - a rank counted over
   part of the part is not a rank to act on unasked.
4. Candidates, from the planar_face rows. A row carrying no `normal` key is dropped: find_geometry
   omits that key when the face normal did not read, which is not a normal of zero. Keep a row when
   BOTH hold:
   - principal axis: exactly one component of `normal` has absolute value >= 0.999 and the other
     two are <= 0.001 in absolute value. That component's letter and sign is the row's AXIS
     (+X/-X/+Y/-Y/+Z/-Z). A part modelled off the world axes yields no candidate at all - that is a
     fallback to Phase 2, not a failure.
   - size: `area` >= `AUTO_MIN_AREA_FRAC` * `BBOX_FACE_MM2` (inclusive - a face exactly at the
     fraction is kept).
   No candidate survives -> run Phase 2.
5. Score the six AXES (not the faces) from the same two reads. For an axis `d`:
   - `co_normal_faces(d)` = planar_face rows whose `normal` dotted with `d` is >= 0.999 - the faces
     pointing the same way as `d`, the candidate itself included.
   - `coaxial_holes(d)` = cylinder_face rows whose `axis` dotted with `d` has ABSOLUTE value
     >= 0.999. A cylindrical face's `axis` carries no reliable end-to-end sign, so each such face
     counts for `+d` AND for `-d`.
   - `score(d)` = `co_normal_faces(d)` + `coaxial_holes(d)`. `exposure(d)` = `score(d)` divided by
     the sum of `score` over all six axes - null when that sum is 0.
   This counts faces that FACE the axis and round faces ALIGNED to it, and nothing more. Neither
   read says a counted round face is open to the candidate face rather than blind, buried, or
   entered from the far end. The proposal says so in those words.
6. Rank: the winning AXIS has the highest `score`; the PROPOSED FACE is the largest-`area`
   candidate carrying that axis. Force `"propose"` (ask, never auto-proceed) when any of these
   holds - each is a tie the numbers do not settle:
   - the winning `score` is less than `AUTO_MARGIN` times the second-highest axis's `score`;
   - the two largest candidate areas on the winning axis differ by less than 1% of the larger;
   - every `score` is 0, i.e. the rank is AREA ONLY - say exactly that in the proposal.
7. Evidence: `view_screenshot_multi(views=["top", "front", "right", <the candidate's own view>],
   width=800, height=600)`. The candidate's view is the one LOOKING AT its axis: +X `right`,
   -X `left`, +Y `back`, -Y `front`, +Z `top`, -Z `bottom`; a duplicate name collapses (the tool
   returns one image per distinct view) and its response names the views it captured. The images
   come back inline, each labelled `View: <name>` - view_screenshot_multi writes no file and
   returns no path, so the proposal names the VIEWS, never a path. With `EVIDENCE_DIR` set, also
   `view_screenshot(view=<name>, file_path=<EVIDENCE_DIR>/<model>_<name>.png)` per view and quote
   the `file_path=` and `size_bytes=` that response reports back - those are the only screenshot
   paths this skill can state.
8. The proposal, shown to the operator as ONE block, every number quoted from the reads above:
   - the face `handle`, verbatim;
   - the AXIS plus the raw `normal`;
   - `area` in mm2, and that as a percentage of `BBOX_FACE_MM2`;
   - `position` - the face centroid, world mm;
   - the rank reason: `co_normal_faces` + `coaxial_holes` = `score` for this axis, its `exposure`,
     the runner-up axis and its score, whether the `AUTO_MARGIN` margin held, and `truncated` when
     step 3 set it;
   - the evidence views (plus the written paths when `EVIDENCE_DIR` is set);
   - the limits, stated plainly: this rank is geometry only. It does not read FIXTURE ACCESS or
     CLAMPING - which faces the vise can grip, what the jaws will cover, how the part is held - nor
     whether a counted round face is open to this face, nor whether the part needs a second setup.
     A yes settles the machining Z direction and nothing else.
9. The question. `AUTO_PICK="auto"` and step 6 forced nothing -> skip the question, proceed, and
   report the proposal as made. Otherwise ONE `AskUserQuestion`, header `"Machining face"`,
   question `"Machine from this face? <AXIS>, <area> mm2, centroid <position>."`, options exactly:
   `"Yes - machine from this face"` (proceed) / `"No - I will pick in Fusion"` (run Phase 2) /
   `"Cancel"` (stop the skill). Any answer outside these three = stop and report it.
10. On a yes, RE-ACQUIRE the handle before anything consumes it - find_geometry handles are
    short-lived and the operator's answer took as long as it took:
    `find_geometry(target=<body_name>, kind="planar_face", nearest_to=<the proposal's position>,
    units="mm", max_results=5)`, then take the row whose `position` is within 0.01 mm on each axis
    of the proposed one AND whose `normal` matches it within 0.001 per component. No such row =
    STOP and report both records; a handle that did not re-read is never passed on as the axis
    source.

-> Record: `zdir` (= the re-acquired row's `normal`), `body_name`, face `handle` (= the re-acquired
row's `handle`). These are the same three values Phase 2 records and they are used identically, so
continue at Phase 2 step 4 and skip Phase 2 steps 1-3.

## Phase 2 - Machining face + orientation (READ)

TWO human-step paths, one outcome. Which runs depends on the tool the executing agent holds, not on
the operator's taste; both end at step 3's three recorded values, and step 4 onward is identical.

- PATH A - `AskUserQuestion` (the default; steps 1-3 below). One deterministic structured question -
  no selection hold, no timeout, no polling. The operator clicks in Fusion at their own pace; the
  question is the sync point, and answering it hands control back.
- PATH B - `sys_request_selection`, for an agent that holds no AskUserQuestion. A Fusion-side pick
  with NO confirm/cancel: the click IS the control, so there is nothing to answer and no "read my
  selection again" option. ANNOUNCE in chat what to click FIRST - the hold shows no prompt inside
  Fusion, and it clears the operator's current selection (`clear_current` defaults true). Then ONE
  `sys_request_selection(what="face", wait_seconds=<keep it under your MCP client's per-call
  timeout>)`. `status="picked"` -> take `selections[0]` and go straight to step 3: the record carries
  the same `direction`, `direction_kind`, `body_name` and `handle` a `sys_get_selection` record does,
  so step 3 reads it identically. `status="timeout"` -> nothing was picked (this is ok, not an
  error); tell the operator what to click, get their go-ahead, and request ONCE more - never re-fire
  in a loop. A null `direction` (a sphere, or a normal that did not read) is not a machining face:
  ask for a different face, as step 2's second branch does.

1. `sys_get_selection(require="face")` - read whatever is selected right now.
2. `AskUserQuestion`, fixed shape (every run presents the identical control), header
   `"Machining face"`:
   - A face with a non-null `direction` is in hand -> question `"Selected: <face summary> on
     <body>, normal <direction>. Use this as the Z-normal machining face for <model>?"`,
     options exactly: `"Yes - use this face"` (proceed) / `"Read my selection again"` (the
     operator has clicked a different face in Fusion; re-run step 1 and re-ask) / `"Cancel"`
     (stop the skill).
   - Nothing usable selected (or a null `direction`, e.g. a sphere) -> question `"No usable
     face is selected. In Fusion, click the machining face - the face whose normal is the
     machining Z - then choose Continue."`, options exactly: `"Continue - read my selection"`
     (re-run step 1 and re-ask) / `"Cancel"` (stop).
   Any free-text answer outside these options = stop and report it.
3. From the confirmed record: `zdir` (= `direction`), `body_name`, and the face `handle`
   (selection reads mint find_geometry-style handles).
4. `workspace_orient` + `doc_get` - record model name, units, and identity:
   - unsaved (`has_data_file` false): derive the model name (operator's name for the part,
     else the dominant body's name, else ask once - never "Untitled"); destination =
     `DEFAULT_PROJECT` / `DEFAULT_FOLDER`.
   - saved: destination = the part's own folder (`data_get` on its `document_id` ->
     `folder_path`); record the existing URN.

-> Record: `zdir`, `body_name`, face `handle`, model name, units, URN or null, destination.

## Phase 3 - "Center of Model" part-space origin (WRITE)

1. `joint_create_origin(anchor="bbox_center", bbox_target=<body_name>, orient_axis=<face
   handle>, name="Center of Model")` - builds the frame at the part's bbox center with Z along
   the picked face's normal, and verifies its own placement (it rolls back and errors if the
   origin lands off its computed center). EXPECT: the response's `frame_axes.primary_axis_Z`
   is parallel to `zdir` - a negation means the selection went stale; redo Phase 2 step 2.
2. `model_inspect(target=<body_name>, frame="Center of Model", units="mm")` - the part-space
   extents (Z = machining axis). EXPECT: non-zero x/y/z; `center` matches step 1's center.

-> Record: `extents_mm` (x/y/z; z feeds the Phase 7 stock-top offset).

## Phase 4 - Save the part with the JO (WRITE, async)

1. Unsaved: `doc_save_as(name=<model>, project=<destination>, folder=<destination>,
   create_path=true)`. Saved: `doc_save()`. Either way the new version captures the JO.
2. `doc_get` - EXPECT the active document is the part, saved, with a URN. Record URN + version.
   (The insert in Phase 7 references this SAVED version - the JO must be in it.)

## Phase 5 - Resolve and copy the template (WRITE, async)

1. `data_get(project=TEMPLATE_LIBRARY_PROJECT, folder=TEMPLATE_LIBRARY_FOLDER,
   recursive=false)` - the eligible templates are exactly this listing. Match the operator's
   named template (exact, case-insensitive) or use `DEFAULT_TEMPLATE`; a miss = STOP and
   report the available names. Record `TEMPLATE_URN` from the listing - never a URN from
   memory or another folder.
2. `doc_open(<TEMPLATE_URN>, force_api_open=true)`, then `doc_get` until the template is the
   active document. (Copy = open then save-as: `doc_copy` cold-reconciles a closed template's
   reference graph and destabilizes the session - see reference.md.)
3. `doc_save_as(name=<model> + TEMPLATE_NAME_SUFFIX, project=<destination>,
   folder=<destination>, create_path=true)` - the copy becomes the active document.
4. `doc_get` - EXPECT `active_document` = `<model>_CAM`; record its URN. The library original
   is never modified.

## Phase 6 - Verify the template's wiring (READ)

All reads against the now-active `<model>_CAM`. Each wiring fact comes from CONFIGURATION and
is VERIFIED against the read - a pinned name missing from the document = STOP, reporting the
names that were found. Only a blank ("") config entry is inferred from the read, and an
ambiguous inference is settled with an `AskUserQuestion` listing the read names as options -
never by picking one silently.

1. `cam_get` - EXPECT `SETUP` among the setups (blank: the active milling setup, else the
   first milling setup). Record the setup, its model component occurrence
   (`selected_models[0]` - the slot component the part swaps into), and its stock/fixture
   names. `selected_models` null (the setup names it in `model_lists_unreadable`) means the
   list was NOT READ, which is not the same as empty; `[]` means the setup selects no model.
   Either way there is no slot component to record = STOP, reporting which of the two it was
   - never index a null or empty list.
2. `design_get(include=['tree'], component=<model component occurrence>)` - EXPECT
   `PLACEHOLDER` among its child occurrences (blank: the one child occurrence with bodies
   whose name is not WCS/zero-like - a lone cube named like "WCS"/"zero" is the setup's WCS
   cube, never delete it; several candidates = AskUserQuestion, one option per child plus "No
   placeholder - insert alongside" and "Cancel"). `children_truncated` true on the model
   component's node means the children listed are a SUBSET (the level was cut by
   `max_results`), so re-read it with `max_results` above that node's `child_count` before
   naming a placeholder - picking one out of a partial level guesses. Bodies sitting directly
   in the model component itself (no occurrence to remove) = STOP and report the listing.
3. `assembly_get(include=['joint_origins'])` - EXPECT `ATTACH_JO` among the joint origins
   whose `component` is the root component (blank: the root JO matching Attach / Center of
   Model / Workpiece; several = AskUserQuestion with the read names; none = record none and
   Phase 7 seats on the stock top instead). Also record the placeholder's own JO
   `world_position` if it carries one - it marks the seat the part must land on, and it is
   gone once the placeholder is deleted.
4. `param_get()` - record which of `PART_PARAMS` exist as user parameters.

-> Record: setup, model component occurrence, stock name, placeholder (or none), attach JO
(or none), the seat position, which PART_PARAMS exist.

## Phase 7 - Stand up the part (WRITE)

1. `doc_insert_occurrence(document_id=<part URN>, into_component=<model component occurrence>
   [, remove_existing=<placeholder occurrence>])` - inserts the part as an x-ref at identity
   and clears the placeholder in the same call. Record `new_occurrence_name`. (If the tree
   later shows the reference stale, `doc_update_xref(name=<model>)`.)
2. `assembly_ground(occurrence=<new_occurrence_name>, ground_to_parent=false)` - an inserted
   occurrence is locked to its parent by default; free it so the joint can position it.
3. Join the part's JO to the template, one of two ways:
   - Root JO recorded: `joint_create(occurrence_one="Center of Model",
     occurrence_two=<root JO>, joint_type="rigid")` - the template JO's offsets position the
     part; nothing to measure.
   - No root JO: `joint_create(occurrence_one="Center of Model",
     occurrence_two="<stock occurrence>:top", joint_type="rigid",
     offset=-(0.5 * <extents_mm.z> + 1), units="mm")` - seats the part's top 1 mm below the
     stock top (the skim allowance).
   Each side is a Joint Origin name (bare, or `<occurrence>:<JO name>`) or a snap; on a
   resolve error the tool lists the design's JOs - correct the name and retry once.
4. Verify from numbers: `assembly_get` - EXPECT the new joint is not in `broken_joints`
   (pre-existing template warnings are not yours to fix). `model_inspect(target=<inserted
   occurrence full path>, units="mm")` - a world-frame read (`frame=` takes a BODY target
   only, not an occurrence) - EXPECT `center` at the placeholder's recorded JO position from
   Phase 6 (the seat), or on the stock-top path at the stock top minus the skim allowance.
5. Stock (only if Phase 6 found PART_PARAMS): `model_inspect(target=<inserted occurrence full
   path>, units="mm")` - measure POST-join in world axes (the join reorients the part, so
   Phase 3's pre-join extents land on the wrong axes) - then `param_set` each parameter from
   this reading. No PART_PARAMS = skip, and say the stock was left as the template defines.
6. Naming (best-effort): `sketch_set_text(text=<model>, sketch_name=NAMEPLATE_SKETCH)`
   (`changed_count` 0 = template has no nameplate; fine) and `cam_set_nc_comment(
   comment=<model>)`.
7. `doc_save()` - the insert, joint, and parameters are session-only until saved.

## Phase 8 - Generate the toolpaths (WRITE, async)

The insert and stock resize invalidate the template's operations; the job is not stood up
until they regenerate.

1. `view_switch_workspace` to Manufacture - out-of-date state is only re-evaluated against
   the new geometry once Manufacture is active; generating from Design can wrongly skip
   stale operations.
2. `cam_generate()` (whole document) - returns a handle immediately; generation runs in the
   background at its own pace (often minutes).
3. `cam_get_status(handle=<handle>)` occasionally - a plain progress read; check every minute
   or so until `completed` is true, doing nothing in between. An ERRORED operation will never
   finish: stop waiting and report it from `cam_get`'s error text.
4. `cam_get` - EXPECT no out_of_date or errored operations among the unsuppressed ones.
   Failures = report each by name; do not silently accept a partial job.
5. `doc_save()` - capture the generated job.

## Phase 9 - Machine-limit check (READ)

A template's tool presets and a setup's machine are set by different people in different places, and
nothing in the insert chain compares them. This phase compares the two NUMBERS - the rpm each
operation asks for against the rpm its setup's machine can turn - and hands the operator a table.
It changes nothing: which spindle speed a preset should carry is the shop's decision, not the
skill's.

1. `cam_get(include=['machine'])` - one call, every setup. Each `setups` row carries `setup`,
   `machine` (the label), `kinematics_readable`, `spindle` (`max_rpm` / `min_rpm`, or null when no
   spindle answered a speed - a 0 is never published as a limit) and `axes` (per axis: `kind`,
   `travel` + `range` + `units` for a linear axis, `travel_deg` / `range_deg` for a rotary one,
   `is_infinite: true` with no number for an unlimited one). `kinematics_readable: false` with
   `blocked_by: ["no_machine_selected"]` means the setup has no machine - the comparison below is
   impossible for that setup; say so, never treat it as "within limits".
2. `cam_get(include=['default', 'operations'])` - one call, every setup (a deep include omits the
   default slice unless it is named, and step 3 reads that slice). The setup row carries
   `machine_spindle_max_rpm` (the same number as step 1's `spindle.max_rpm`). Each operation row
   carries `name`, `state`, `preset` (the tool preset it uses, null when none), `folder` (the
   CAM folder it sits in, beside `is_suppressed`) and the comparison:
   - `spindle_over_machine_max: true` plus `spindle_rpm` and `machine_max_rpm` - the op asks for
     MORE than the machine can turn;
   - `spindle_over_machine_max: null` plus `spindle_check` naming the unreadable side
     (`machine_max_unavailable` / `op_spindle_speed_unreadable`) - NOT checked;
   - no `spindle_over_machine_max` key at all - checked, at or under the maximum.
   A missing key and a null are different answers; only the first is "fine".
3. Completeness before counting: a setup's `operations_truncated`, or the payload's own `truncated`,
   means the rows are a SUBSET - label the table PARTIAL and quote rows-read against each setup's
   `operation_count` from the default slice. A partial census is never reported as "within limits".
4. Fusion's own view of the same disagreement rides on the row as `has_warning: true` + `warning`
   (absent = no warning raised; a read, not a gap). Quote any warning text VERBATIM beside the
   numbers - it names what Fusion compared, and the two channels should agree; if they do not,
   report both rather than picking one.
5. The table, for the operator to read before hand-over: one row per operation that is over
   (name, preset, `spindle_rpm` vs `machine_max_rpm`, the ratio), then the NOT-checked operations
   with their `spindle_check` reason, then a one-line count of the operations that checked clean.
   STOP only if step 2 errors - with no census there is nothing to hand over, so report the error
   rather than a clean bill.

-> Record: the machine's `spindle.max_rpm` per setup, the over-max operations with both numbers,
the operations that could not be checked and why, and any warning text quoted.

## Phase 10 - Verify and report (READ)

1. `data_get(project=<destination project>, folder=<destination folder>)` - EXPECT the part
   and `<model>_CAM` both listed.
2. `view_screenshot` - the part seated in the fixture.

Report: destination folder, part URN, `<model>_CAM` URN, part-space extents, the join used
(attach JO or stock top), PART_PARAMS written or skipped, the generation outcome per setup,
and the screenshot. Also which face-pick path ran - a Phase 1 proposal the operator confirmed, a
Phase 1 proposal taken under `AUTO_PICK="auto"`, or the Phase 2 pick (naming whether that pick came
through `AskUserQuestion` or `sys_request_selection`) - and for either Phase 1 path the rank reason
it was proposed on. Carry Phase 9's limit table verbatim: the hand-over states which operations
exceed the machine and which could not be checked, not just that the job generated.
