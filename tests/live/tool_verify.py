# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Live tool verification: every registered tool called at least once against LIVE Fusion.

A deterministic script of direct tools/call requests (no LLM, no SDK) walking a dependency DAG
that builds its own world in a scratch document and tears it down. The gate: a ledger with zero
unexplained rows - every tool is pass / expected-refusal / skipped(reason).

The sweep is the END-TO-END STORY the evals grade agents on, in one document: a parametric
machined BRACKET is cast, turned solid (stepped top, radiused pocket, through bores, a
counterbored mounting pattern, a boss, a broken edge), detailed, resized off its one driving
length, sized into a billet and clamped in a modelled VISE (slider jaw, lead screw on a motion
link, grip proven by measure), photographed, and finally machined - four operations on the REAL
part in the REAL fixture, generated to completion (an empty toolpath fails the run), NC posted.
Cameo fixtures for families with no home on the part ride the same document.

The run is not capped by wall clock - what it costs is reported per act and per tool, and a program
that outgrows one 600 s shell call is walked in CHUNKS that share one receipt (``--run``/``--resume``
below). A run with zero FAIL/blocked steps writes ``tests/live/VERIFIED_TOOLS.md`` - the tracked receipt: the
per-tool ledger stamped with a SHA-256 of the ``commands/mcpServer/`` source tree, binding that
run to the exact tool source it exercised. ``--check`` recomputes the hash offline (no Fusion
needed) and fails on any difference, so a green suite cannot ride on a live run that never saw
the current code. The hash is of the WORKING TREE while Fusion runs its LOADED copy of the
add-in: after editing source, reload the add-in before re-running, or the receipt stamps code
the session never executed.

Run:  py -3 tests/live/tool_verify.py            (requires Fusion running + the add-in enabled)
      py -3 tests/live/tool_verify.py --check    (no Fusion: exit 1 when VERIFIED_TOOLS.md is missing
                                                  or its source hash differs from the tree)
      py -3 tests/live/tool_verify.py --json     (also write tests/live/results/verify-<ts>.json)
      py -3 tests/live/tool_verify.py --keep-open  (leave the story document open for inspection)
      py -3 tests/live/tool_verify.py --acts "ACT 10a..ACT 10e"  (walk only those acts against the
                                                  document a --keep-open run left open; no receipt)
      py -3 tests/live/tool_verify.py --run r1 --acts "ACT 0 - OVERTURE..ACT 9 - THE SHOWCASE"
      py -3 tests/live/tool_verify.py --run r1 --resume   (the same run, chunk by chunk: each chunk
                                                  saves ctx/ledger/acts-done and leaves the document
                                                  open, and the receipt stamps once the whole
                                                  program has run under that id)
      py -3 tests/live/tool_verify.py --run r1 --resume --acts "ACT 10c4 - CAM: THE HUB JOB"
                                                  (a DEVELOPMENT walk: the named acts run again
                                                  against that run's world with its saved ctx, an
                                                  edited source is fine, no receipt, state untouched)

Steps are DATA (see STEPS): each row is (tool, args, expect) where args may be a dict or a
callable(ctx) reading what earlier steps stored, and expect is "ok", "refused" (a deliberate
guard check whose error must name the offense), ``_refused("fragment", ...)`` - the same
deliberate refusal, with each fragment required IN the error text (a guard that starts refusing
for a different reason is a FAIL, not a silent pass) - or a callable(payload) -> bool - a VALUE
PREDICATE run on an ok result (falsy = FAIL); that is how grip contact and machine assignment
are asserted, not just call success. Extend coverage by adding rows, not code.

The expectation's SHAPE is also what buckets the tool in the receipt: a passing value predicate
earns 'covered', a passing bare "ok" earns only 'called' (see ``predicate_kind``), so the count
line separates the tools whose effect was read back from the tools that merely did not fail.

WHERE THE ROWS ARE. This module is the surface every consumer imports; the harness itself is its
siblings, and a step is edited in the one it belongs to:

  verify_core.py      the wire, the step kinds, the value predicates, the scratch fixtures
  verify_layout.py    the sketch hoist, the slot packer, the framing pass
  verify_acts_doc.py / _sketch / _model / _motion / _mesh / _cam / _hub / _cloud   the step rows
  cloud_config.py     the OPT-IN cloud tier's local config (gitignored); absent, the tier is skipped
  verify_program.py   the ordered acts, run through those passes, plus STEPS/STORY/EXCLUDED
  verify_runner.py    run/run_steps/judged_steps and the receipt (source_hash, --check)
"""

import argparse
import os
import sys
import time   # noqa: F401 - surface: a consumer stubs tool_verify.time.sleep

# This module is spec-loaded BY PATH (the completeness lint) as
# often as it is imported by name, and a spec load leaves its directory off sys.path - so
# the modules below would not import. Every consumer reaches them through this one.
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_core  # noqa: E402

from verify_core import (  # noqa: F401
    BASE, MCP, SERVER_NAME, DOC_PREFIX, MACHINE_NAME, TEMPLATE_NAME, NOTE_MAX, REFUSAL_NOTE_MAX,
    STEP_SLEEP_S, _SHELL_TIMEOUT_S, _DWELL, _HERE, REPO_ROOT, SRC_ROOT,
    VERIFIED, _post, call, health_gate, registered_tools, _ctx_get, _Refusal, _refused, Parked,
    Needs, _needs, step_capability, parked_reason, CAPABILITY_PROBES, probe_capabilities,
    capability_met, capability_skip_reason, _machining_extension_probe,
    CLOUD_TIER, OPT_IN_TIERS, CAPABILITY_DETAIL, _cloud_tier_probe,
    _unparked, _PUSH_OPS, _INSPECT_OPS, _is_inspect, _ARG_LOAD_OPS, _arg_load_sites,
    _TRUTHY_ONLY_CALLS, _callee_name, _inspects_argument, _inspects_payload, predicate_kind,
    EXPORT_DIR, SVG_PATH, SVG96_PATH, write_png, MARKER_PNG, _fg, _fgn, _prof, _matched, _face_up_at,
    _PATH_LABEL, _path_count, _measured,
    _RECALL, _recall, _SVG96_MM, _SVG96_TOL, _svg96_extent, _repair_no_op, _made_component,
    _made_component_inactive, _PLANE_NORMAL_AXIS, _datum_plane, _dim_measures, _datum,
    _result_bodies, _extruded, _revolved, _swept, _lofted, _material_assigned, _gap_measured,
    _relation_measured, _rebuilt, _relation_read, _relation_passes, _extent_measured, _drafted,
    _drilled, _mirrored, _patterned, _joined, _edge_feature, _filleted, _chamfered, _shelled,
    _offset_faces, _moved, _split_bodies, _unstitched, _stitched, _base_feature_open,
    _base_feature_closed, _arranged, _holder_computed, _piped, _num, _near, _mod360, _jointed,
    _mesh_round_trip, _joint_origin_landed, _joint_origin_at, _joint_origin_computed,
    _joint_limits, _motion_linked, _driven_angle, _driven_slide, _as_built, _jointed_at_geometry,
    _grounded, _moved_occurrence, _rigid_grouped, _constrained, _captured, _joint_is,
    _joints_listed, _joint_origins_listed, _interference_measured, _PARAM_TOL, _param_added,
    _param_set_to, _param_deleted, _params_listed, _param_read, _param_favorited, _new_document,
    _document_read, _document_closed, _imported, _imported_sketches, _imported_curves,
    _exported_bytes, _box, _JOINT_STATIONS, _STN_X0, _STN_PITCH, _STN_Y, _STN_Z, _JOINT_BEAT,
    _pose_took, _joint_bench, _group_of, _watch, _dwell, _leaves_no_row, _watch_all,
    _SKETCH_PLANE, _PLANE_VIEW)

from verify_layout import (  # noqa: F401
    _SKETCH_MAKERS, _BODY_MAKERS, _FRAME_MARGIN, _FRAME_CONTEXT, _FRAME_MIN_SPAN, _FRAME_STRETCH,
    _FRAME_FALLBACK_NEIGHBOURS, _FRAME_MAX_SUBJECTS, _FRAME_SKETCH_GROUP, _FRAME_SKETCH_SPAN,
    _FRAME_RELATION_WIDEN, _ORIGIN_PLANES, _SKETCH_ORDER_BOUND, _sketches_first,
    _sketch_reading_order, _RELATION_TOOLS, _PLACED_BOX, _MEASURED_BOX, _CHUNK_OF, _COMPONENTS,
    _PATTERNED, measured_boxes, _chunk_box, _union,
    _DRIFT_CHUNKS, _DRIFT_TOL_MM, layout_drift_mm, layout_placed_as_measured, drift_row,
    _FRAME_PATTERN_WIDEN, _framed, _frame_box, _frame_cluster, _expand, _inside,
    _frame_neighbourhood, _PLACE_PAIRS, _PLACE_DELTA_TOOLS, _PLACE_XYZ, _PLACE_POINTS,
    _PLACE_NAMES, _PLACE_FRAMES, _PLACE_WITH, _JOINT_GROUPS, _JOINT_FAMILY, _PLACE_ANCHORED,
    _ORIGIN_RELATIVE_TOOLS, _PIN_HALF, _FIELD_X0, _FIELD_Y0, _FIELD_WIDTH, _FIELD_GUTTER,
    _place_owner, _place_num, _place_points, _place_walk, _place_shift, _place_slots, _placed,
    _px, _py, _SLOTS)

from verify_acts_doc import (  # noqa: F401
    _OVERTURE, _SHOWCASE, _FINALE, _RELOAD_PROBE_GAP_S, _RELOAD_PROBE_TIMEOUT_S,
    _RELOAD_DOWN_POLLS, _RELOAD_UP_POLLS, _RELOAD_SMOKE_QUERY, _server_answers, _poll_health,
    reload_smoke)

from verify_acts_sketch import _SKELETON, _SKETCHWORK  # noqa: F401

from verify_acts_model import (  # noqa: F401
    _SOLIDS, _DETAILS, _RESIZE, _SOLIDS_FB, _DETAILS_FB, _RESIZE_FB)

from verify_acts_motion import (  # noqa: F401
    _MOTION, _plate, _fixture_rest_clean, _VISE)

from verify_acts_mesh import _MACHINING, _NESTING, _MESH  # noqa: F401

from verify_acts_cam import (  # noqa: F401
    PART_COMP, PART_DRIVER, STOCK_COMP, VISE_BASE, JAW_FIXED, JAW_MOVING, CAM_SETUP,
    _op_created, _toolpath_shown, _CAM_STORY, _tmpl_names, _CAM_DELIVER,
    poll_generation, _CAM, _CAM_FB_DELIVER)

from verify_acts_cloud import (  # noqa: F401
    RUN_FOLDER, MOVED_FOLDER, SOURCE_DOC, COPY_DOC, HOST_DOC, RUN_PATH, MOVED_PATH,
    RESTORE_VERSION, _CLOUD_DATA, _CLOUD_DOC, _CLOUD_DRAWING, _lit, _files_gone, _xrefs)

from verify_acts_hub import (  # noqa: F401
    HUB_COMP, HUB_MILL_SETUP, HUB_TURN_SETUP, _HUB, _HUB_JOB, _fully_constrained, _hub_box,
    _hub_setups, _hub_tools_landed, _rim_edge, _threaded, _tilt_frame, _tilt_v)

from verify_program import (  # noqa: F401
    _ACT_PROGRAM, _SKETCH_PHASE, _placed_boxes, ACTS, ACT_NEEDS, POLL_AFTER, STEPS, STORY,
    EXCLUDED, PENDING)

from verify_runner import (  # noqa: F401
    source_hash, _STAMP_RE, write_verified, check, _shoot, run_steps, judged_steps,
    _precondition_holds, _one_act, select_acts, run, RESULTS_DIR, run_state_path,
    save_run_state, load_run_state, resume_refusal, develop_refusal, _document_now)
from verify_core import attestation_identity  # noqa: F401

# The patchable surface is patched ON THIS MODULE (a consumer stubs tool_verify.call,
# tool_verify.ACTS, tool_verify.source_hash), so the runner and the post-act hook read
# those names off this namespace when a run starts instead of binding their own copies.
verify_core.bind_facade(globals())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--keep-open", action="store_true",
                    help="leave the story document open at the end instead of discarding it")
    ap.add_argument("--shots", metavar="DIR", default=None,
                    help="write a PNG of the framed view after every framing row, so the framing "
                         "can be judged by looking instead of by trusting its ratio")
    ap.add_argument("--trace", action="store_true",
                    help="print each step (flushed) before it runs, so a Fusion crash names its killer")
    ap.add_argument("--acts", metavar="SPEC", default=None,
                    help="walk only these acts, in ACTS order, against the document a prior "
                         "--keep-open run left open: a comma list of act names as printed in the "
                         "log (\"ACT 10a\", \"FINALE\"), each one act or an \"A..B\" range. Writes "
                         "no receipt")
    ap.add_argument("--run", metavar="ID", default=None, dest="run_id",
                    help="walk the program under this RUN ID, saving the ctx, the ledger so far and "
                         "the acts done to tests/live/results/run-<ID>.json after every act. A "
                         "chunk that does not finish the program leaves the document open and "
                         "writes no receipt")
    ap.add_argument("--resume", action="store_true",
                    help="carry on the --run ID from where its last chunk stopped, against the "
                         "document that chunk left open. The receipt is stamped once the whole "
                         "program has run under that id, from the union of its chunks. With "
                         "--acts it is a development walk: the named acts run again against that "
                         "run's world with its saved ctx, no receipt, state untouched")
    args = ap.parse_args()
    shots = args.shots
    if shots:
        os.makedirs(shots, exist_ok=True)
    if args.resume and not args.run_id:
        sys.exit("--resume needs the run it continues: --run <id> --resume")
    sys.exit(check() if args.check else run(args.json, keep_open=args.keep_open, trace=args.trace,
                                            shots_dir=shots, acts_spec=args.acts,
                                            run_id=args.run_id, resume=args.resume))
