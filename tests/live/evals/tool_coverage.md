# Scenario coverage map

Which scenario drives which tool DOMAIN, and the behavior each grades. Coverage is a DIAGNOSTIC
(which tools a run happens to touch), never the target - the scenarios are GOAL-SHAPED and grade
outcome + honesty, not path (see README.md). A cold agent may reach a correct outcome via a
different tool than the ones listed; that is fine, and a wall the agent CANNOT get around is
itself the most valuable finding.

## The pipeline (S1 -> S9): one artifact chain from parametric plan to posted NC

Each scenario builds on the prior artifact, staged/addressed by lineage URN (documents share
names across runs). Immutability: P1-P5 artifacts are never re-versioned by later scenarios; the
two MUTABLE artifacts are P6-Vise (S7 edits it to prove x-ref staleness) and P7-Template (S8
advances it with the CAM layer). S9 forks the template (RING-CAM) and leaves it untouched.

| Scenario | Domain | Tool families it naturally drives | Behavior the postconditions grade |
|---|---|---|---|
| `S1_Foundation` | sketch + parameters + construction | param (add/set/favorite), sketch (create/geometry/constrain/dimension/get incl. the plane world frame + construction LINES), model_create_component (incl. a NESTED sub-component), construction geometry, doc_save_as, screenshots | the open-ended cast (frame+pedestal, carrier, two rings, rotor+shaft, crank) anchored to a SHARED SKELETON with the executor DECLARING its interfaces; 3D-STRUCTURE graded - axis perpendicularity by dot product, ring coplanarity by plane normals, containment by radii chain, declared interfaces ON-axis by distance-to-line, spin axis in the ring plane; expression dims spot-verified per component; propagation incl. an interface element; the UNBODIED handoff (body_count 0 graded) |
| `S2a_Hardware-Structure` | solid modeling (split 1/2 - fits the harness task cap) | model_extrude (multi-profile handles + symmetric extents), model_inspect (volumetric census), model_measure_between, assembly_inspect_interference (overlap + coincident-face contact listing), find_geometry, sketch_get, doc_save_as | the primary bodies owned by the right components under the ENGAGEMENT CONTRACT - flush support contact at declared engagements (carrier-on-pedestal, crank-on-frame) proven by ~0 measures, ZERO overlap everywhere, clearance where parts move; rings as coplanar hollow BANDS by volume arithmetic + midplane reads; predecessor version isolation |
| `S2b_Hardware-Interfaces` | solid modeling (split 2/2) | model_extrude (THROUGH-ALL bores, target_bodies-scoped cuts), model_inspect, find_geometry, sketch_get (pin dimension EXPRESSIONS), assembly_inspect_interference, doc_save_as | pins COLLINEAR with the skeleton axes THREADING both joined parts; through bores + shaft bearing seats with read pin-vs-bore clearance; PARAMETRIC pins (sketch expressions graded - the frozen-pin defect fails at the source); FINAL state = zero overlap, contact only at the inherited support engagements (the exact state S3 assumes); predecessor version isolation |
| `S3_Motion` | joints + kinematics + interference | joint tools (revolutes on pin geometry), joint_motion_link (cross-chain), joint_drive, assembly_get, assembly_inspect_interference, screenshots | a 4-axis mechanism (yaw carrier on the post, pinned ring pivots, rotor spin, frame crank); joint AXIS DIRECTIONS graded against the skeleton (pairwise dots); the CRANK motion-linked to the ROTOR SPIN across independent chains at a declared ratio (Fusion refuses same-chain links); no-teleport joints; interference graded at rest AND posed with expected contacts named |
| `S4_Details` | detail features | model_hole, model_fillet, model_chamfer, model_revolve (a turned boss about a BORE's own face axis), find_geometry, model_inspect, assembly_get, assembly_inspect_interference | features on the LIVING mechanism (swing-clear hole placement graded via the posed interference check); shape-neutral instruction interpretation; volume deltas reconcile; mechanism healthy after |
| `S5_Derive` | scoped derive + local edits + surfacing + datums | doc_insert_derive (source_components scoping; requires the source open), doc_open, doc_get (derive-kind reference rows), model_fillet (ON the derived body), surface_patch, surface_offset (zero + nonzero), sketch_project, joint_create_origin (bbox_center), model_construction (an along-edge datum plane at a FRACTION of an edge), model_inspect, doc_save_as | the machining-prep model: a linked one-way derive of EXACTLY one component, detail features edited locally on the derived body, prep layer + joint origin at the MEASURED bbox center; params-import reported as an honest diagnostic (live-measured platform no-op); source untouched despite the local edits |
| `S6_Vise` | parametric mechanism design | model + sketch + param families (incl. the SLOT kinds and sketch_set_text with font_name - the tee-slot and the named-font vise nameplate), joint_motion_link + joint_drive (ratio -1 on real sliders), assembly_inspect_interference, the volumetric audit habit (per-body volume reads + body census), doc_save_as | a self-centering vise where ONE parameter drives BOTH jaw OCCURRENCES symmetric about center (midpoint math read fresh at two openings) plus the kinematic proof (drive one slider, both jaws mirror); ONE CONNECTED SOLID per part graded by a volumetric digest the grader re-issues |
| `S7_Template-Skeleton` | template architecture + xref lifecycle | model_create_component (components), param + joint_create_origin (offset-EXPRESSION self-centering stock origin), doc_insert_occurrence (fixture x-ref), doc_open/activate (async, by URN), doc_get(xref_tree), doc_update_xref, design_edit_timeline (feature ATTRIBUTE tags, written then QUERIED back incl. the re: pattern form), joint tools, doc_save_as | component architecture; the stock origin FOLLOWS parametric resizes (two sizes read); the FULL xref lifecycle graded live (insert -> stale after source edit -> update -> current, versions read each step); stock gripped by the jaws |
| `S8_CAM-Tooling` | CAM tools + setups + operations | cam_edit_tools (4 tools with holders/presets), cam_create_setup (COMPONENT selection, WCS on the stock origin), cam_create_operation (4 tool types), cam_generate + cam_get_status, cam_activate_setup, cam_save_template, doc_save | the manufacturing layer: tools built through the wire; setups select COMPONENTS (implicit consumption); all operations compute healthy; setup activation round-trip; persisted as document AND template artifact |
| `S10_Drawing-Package` | the 2D deliverable tier | drawing_create (standard/units/strategies + the CREATE-time custom size), drawing_get (the tier's READ: sheets by export_index, sizes incl. custom_size, per-view rows), drawing_edit_sheet (add/rename), drawing_dimension (read-back honesty - values are unreadable on this platform), drawing_insert_image (scale + bounds-check + the graded off-sheet REFUSAL), drawing_export (all sheets; a sheet_range export must run FIRST, never after another export - NEW-12), view_screenshot (the artwork source) | the shop drawing package graded from drawing_get reads, tool READ-BACKS and file evidence; custom sheet 320x200 proven by read-back; off-sheet insert refused naming the extent; the package PDF lands with size evidence; respecting a tool's own hazard warning is itself graded |
| `S11_Gimbal-Ring-Job` | the gyroscope's final component: a mill-turn job sequenced like a machinist's, cold and skill-less | sketch/model_revolve + bosses + holes (the outer gimbal ring), cam_create_setup x2, cam_edit_setup (mill-turn machine; the milling setup's stock from the PREVIOUS setup), cam_edit_tools (cutters by type), cam_create_operation (turning face / bore or profile; drill + boss milling), cam_select_geometry, cam_generate + cam_get_status, cam_get(include=['setups','time']), cam_reorder, cam_show_toolpath one at a time + view_screenshot, cam_post x2 (shipped turning post, numeric program; haas local) | turning FIRST and milling SECOND in the tree; the milling stock reads as rest from the previous setup; every operation cuts (non-zero time, none in the empty census), warnings listed by operation; two NC files landing with exclusions reported; the machinist's product bar in the grader notes; whether sys_get_guidance is called (diagnostic; the A/B is GUIDANCE-AB-1) |
| `S9_Consume` | the insert-into-template dataset | doc_save_as (fork), design_delete_occurrence (placeholder), doc_insert_occurrence (model x-ref into the component), joint to a named JO across references, model_inspect (POST-SEATING), param_set (stock), assembly_inspect_interference (the saved-state validity gate), cam_generate/cam_get_status, cam_post, sim start (bonus) | the finale: template fork isolates P7; the real model seats at the stock center; STOCK SIZED FROM POST-SEATING MEASUREMENT (the reorientation trap: pre-join numbers in the arithmetic = FAIL); workholding feasibility measured, an honest UNCLAMPABLE disclosure passes; THE SAVE IS QUARANTINED (zero overlapping pairs in the saved artifact, whatever the verdict); regeneration healthy; NC posted with file evidence |

Fuzzy variants: deferred; reintroduce as terse-goal controls per territory once the pipeline is
stable.

## Tools whose behavior is guaranteed by the mock suite rather than a live scenario

Some paths are impractical to force in an outcome-graded cold-agent task (they need a specific
object graph, or they are a rare branch). These are pinned by the mock unit suite instead:

- **result-body read-back on the mesh + offset/trim/untrim/reverse-normal surface tools** - the
  shared read-back the surface/mesh tools use; the mesh path needs an imported mesh fixture.
  Pinned by `test_common.py` (the shared reader) + each tool's unit test.
- **joint-health over a broken SUB-COMPONENT joint** - needs a nested assembly with a
  deliberately faulted joint; grading it would use the very tools under test. Pinned by
  `test_joint_motion_link.py` (the full joint walk).
- **the design_export ambiguity REFUSAL and the design_get ambiguous-occurrence REFUSAL** -
  pinned by `test_design_export.py` / `test_design_get.py`.
- **the template-generation-mode and library-location enums** - pinned by `test__cam_templates.py` / `test_cam_apply_template.py`.
- **the CAM setup COMPONENT-selection kind** (ambiguity refusal) - pinned by
  `test_inputs.py::TestTargetRefList`; S8 exercises the happy path live.
- **the design-intent auto-promote** - pinned by
  `test_model_create_component.py::TestDesignIntentPromotion`; every multi-component scenario
  exercises it live.
- **derive staleness + refresh-refusal** - staging it in the pipeline would re-version the
  immutable P4 artifact; live-verified at the tool level (doc_get derive rows, doc_update_xref's
  honest per-reference refusal + delete-and-re-derive fallback) and pinned by the doc_get /
  doc_update_xref unit tests.
- **sys_request_selection** - interactive by design: it holds for a HUMAN pick, and an eval never
  puts a human in the loop (proctor.py hard-denies it; that affordance belongs to skills a human
  invoked). Its guards (nothing-to-select, wait bounds, single-pending) are pinned by
  test_sys_request_selection.py; the pick path is verified owner-present at the tool level.

## Running

Stage the fixture per each scenario's frontmatter, then run the AGENT PROMPT block through
`proctor.py` (blind executor; audited counts; see README.md). The chain runs S1 -> S9 in order,
each consuming the prior artifact by URN; a scenario whose fixture the environment cannot provide
is reported SKIP, not a tool failure.
