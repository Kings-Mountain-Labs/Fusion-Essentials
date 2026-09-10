# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Owner-present live verification of the drawing user-present tier.

The blind sweep (tool_verify.py) excludes the tools that need an open drawing document:
drawing_update, drawing_export, drawing_add_sketch, drawing_dimension, drawing_edit_sheet,
drawing_insert_image - plus drawing_create's two real create paths (its sweep beats are all
refusals). This script drives them, in two phases; the pause between them keeps the owner's
eyes on the staged drawings (never-reviewed drawings open headless fine on current builds -
measured on 2705.0.87 - so the pause is a review checkpoint, not a technical requirement):

  py -3 tests/live/drawing_verify.py --stage
      Builds a parametric plate, saves it into the project cloud_config.local.json names, and
      creates TWO drawings from it (default ISO, full-option ASME) - the create beats.
  <the owner opens the ISO drawing in the Fusion UI and leaves it the active document>
  py -3 tests/live/drawing_verify.py --run
      Drives every user-present drawing beat against the open drawing, round-trips a source-design
      edit through drawing_update, and exports the PDFs the owner eyeballs.

Results go to tests/live/results/drawing-verify-<ts>.json plus a printed ledger. This script never
touches VERIFIED_TOOLS.md - the blind sweep's receipt stays its own.
"""

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# the one HTTP driver, the one step engine and the one raster fixture, reused - the status
# vocabulary cannot fork, and neither can the PNG two harnesses place on a sheet
from tool_verify import NOTE_MAX, call, health_gate, run_steps, write_png, _refused  # noqa: E402
# the destination comes from the operator's own gitignored config - no hub or project name is
# written in the repo, and the cloud tier's probe reads the same file
import cloud_config  # noqa: E402

RESULTS_DIR = os.path.join(_HERE, "results")
OUT_DIR = os.path.join(RESULTS_DIR, "drawing_verify")
STAGE_FILE = os.path.join(RESULTS_DIR, "drawing_verify_stage.json")
URN_PREFIX = "urn:adsk.wipprod:dm.lineage:"


def _project():
    """The project this script stages into, or exit with the sentence the operator acts on."""
    config, problem = cloud_config.load_config()
    if problem:
        sys.exit(problem)
    return config["project"]


def _run_steps(steps, ctx):
    """tool_verify.run_steps with a per-step printed line - same engine, same vocabulary."""
    return run_steps(steps, ctx,
                     on_result=lambda tool, status, note:
                     print(f"  {status:18} {tool:26} {note}", flush=True))


def _report(rows, phase):
    # pass* blocks here exactly as it blocks the sweep receipt: a payload-shape mismatch.
    fails = [r for r in rows if r[1] in ("FAIL", "blocked", "pass*")]
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"drawing-verify-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"phase": phase, "steps": rows}, fh, indent=2)
    print(f"\n== {phase}: {len(rows)} steps, {len(fails)} FAIL/blocked -> {path}")
    return 1 if fails else 0


def _created_urn(p):
    return bool(p.get("created")) and str(p.get("file_id", "")).startswith(URN_PREFIX)


def _create_drawing_with_retry(args, tries=6, wait_s=15):
    """drawing_create, retried on the processing lag: a design saved seconds earlier fails with
    '3 : Failed to create drawing document' until its DataFile finishes cloud processing
    (about a minute after doc_save_as)."""
    for attempt in range(tries):
        is_error, payload = call("drawing_create", args)
        if not (is_error and "Failed to create drawing document" in str(payload)):
            return is_error, payload
        if attempt < tries - 1:
            print(f"    (source still processing cloud-side - retry {attempt + 1}/{tries - 1} "
                  f"in {wait_s}s)", flush=True)
            time.sleep(wait_s)
    return is_error, payload


def stage():
    project = _project()
    health_gate()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    design_name = f"SweepDrawingSource {stamp}"
    ctx = {}
    steps = [
        ("doc_new", {}, "ok", None),
        ("param_add", {"name": "PlateH", "expression": "10 mm"}, "ok", None),
        ("sketch_create", {"plane": "xy", "name": "PlateSketch"}, "ok", None),
        ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 0, "y1": 0,
                                               "x2": 80, "y2": 50}],
                                 "sketch_name": "PlateSketch"}, "ok", None),
        ("model_extrude", {"sketch_name": "PlateSketch", "profile_index": 0,
                           "distance": "PlateH"}, "ok", None),
        ("doc_save_as", {"name": design_name, "project": project}, "ok",
         ("design_urn", lambda p: p.get("document_id"))),
    ]
    print("-- STAGE: source design + the two drawing_create beats --")
    rows = _run_steps(steps, ctx)

    # The real create beats the blind sweep cannot carry (they mint cloud files). Fusion
    # auto-names every drawing from the source design, so identity is the lineage URN.
    # Each row is (label, args, extra_predicate_or_None) - the URN check runs on all of them.
    def _custom_500x333(p):
        """The custom sheet the create ASKED for, read back off the input at create time: all four
        numbers land, and the zone counts sit at or above the minimum the API takes."""
        cs = ((p.get("settings_requested") or {}).get("custom_size") or {})
        return (abs((cs.get("width_applied") or 0) - 500.0) < 1e-6
                and abs((cs.get("height_applied") or 0) - 333.0) < 1e-6
                and cs.get("horizontal_zones_applied") == 2
                and cs.get("vertical_zones_applied") == 2)

    for label, args, extra in (
            ("iso", {}, None),
            ("asme", {"standard": "asme", "units": "inch", "sheet_size": "b",
                      "view_style": "shaded_hidden", "tangent_edges": "shortened",
                      "hole_annotations": "thread", "parts_list": True,
                      "parts_list_location": "bottom_right", "auto_dimension": "baseline"}, None),
            # a CUSTOM sheet, live-gated: the setter is assign-back checked and the size is written
            # in the DOCUMENT unit the standard fixes, so 500 x 333 mm has to read back as 500/333
            # with 2x2 zones. This beat pins that path for every future sweep.
            ("iso_custom", {"standard": "iso", "sheet_size": "custom",
                            "custom_width_mm": 500, "custom_height_mm": 333}, _custom_500x333),
            # ordinate is a platform-legal auto-dimension strategy and is offered as one.
            ("iso_ordinate", {"auto_dimension": "ordinate"}, None)):
        is_error, payload = _create_drawing_with_retry(args)
        good = (not is_error) and isinstance(payload, dict) and _created_urn(payload)
        if good and extra is not None:
            try:
                good = bool(extra(payload))
            except Exception as e:
                good, payload = False, f"predicate raised: {e}"
        status = "pass" if good else "FAIL"
        # the create beats are judged here rather than by run_steps, so they truncate the same way
        rows.append(("drawing_create", status, "" if good else str(payload)[:NOTE_MAX]))
        print(f"  {status:18} {'drawing_create':26} [{label}]", flush=True)
        if good:
            ctx[f"drawing_{label}"] = (payload.get("drawing_name"), payload.get("file_id"))

    rc = _report(rows, "stage")
    if rc == 0:
        state = {"design_name": design_name, "design_urn": ctx.get("design_urn"),
                 "drawing_iso": ctx.get("drawing_iso"), "drawing_asme": ctx.get("drawing_asme"),
                 "drawing_iso_custom": ctx.get("drawing_iso_custom"),
                 "drawing_iso_ordinate": ctx.get("drawing_iso_ordinate")}
        with open(STAGE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        iso_name = state["drawing_iso"][0] if state["drawing_iso"] else "?"
        print(f"\nstage state -> {STAGE_FILE}")
        print(f"\nNEXT (one human step): every staged drawing carries the name '{iso_name}'. In the")
        print(f"Fusion UI ({project}) open EACH of them once (review + close is fine), then run:")
        print("  py -3 tests/live/drawing_verify.py --run")
    return rc


def run():
    project = _project()
    health_gate()
    if not os.path.isfile(STAGE_FILE):
        sys.exit(f"No stage state at {STAGE_FILE} - run --stage first.")
    with open(STAGE_FILE, encoding="utf-8") as fh:
        state = json.load(fh)
    design_name = state["design_name"]
    iso_name, iso_urn = state["drawing_iso"]

    # Both staged drawings share a name, so the ISO one is reached by URN: doc_activate if it is
    # open, else doc_open (a never-reviewed drawing opens headless fine on current builds).
    is_error, payload = call("doc_activate", {"name": iso_urn})
    if is_error:
        is_error, payload = call("doc_open", {"file_id": iso_urn, "force_api_open": True})
        if is_error:
            sys.exit(f"Could not reach the staged ISO drawing by URN ({iso_urn}): {payload}\n"
                     f"Open '{iso_name}' in the Fusion UI ({project}) once, then rerun.")
        time.sleep(3)
    is_error, orient = call("workspace_orient", {})
    active = "" if is_error else ((orient.get("document") or {}).get("name") or "")
    if iso_name not in active:
        sys.exit(f"The active document is '{active or '?'}', not the staged drawing '{iso_name}'.\n"
                 f"Open '{iso_name}' in the Fusion UI ({project}) and make it active, then rerun.")

    os.makedirs(OUT_DIR, exist_ok=True)
    png = write_png(os.path.join(OUT_DIR, "sweep_marker.png"))
    stamp = time.strftime("%H%M%S")
    pdf1 = os.path.join(OUT_DIR, f"sweep_drawing_{stamp}.pdf")
    pdf2 = os.path.join(OUT_DIR, f"sweep_drawing_updated_{stamp}.pdf")
    dxf = os.path.join(OUT_DIR, f"sweep_drawing_{stamp}.dxf")
    dwg = os.path.join(OUT_DIR, f"sweep_drawing_{stamp}.dwg")

    ctx = {}
    steps = [
        # Up-to-date no-op: the drawing was just generated from the saved design.
        ("drawing_update", {}, lambda p: p.get("updated") is False and p.get("is_up_to_date") is True,
         None),

        # drawing_dimension - active sheet still holds the generated views.
        ("drawing_dimension", {}, "refused", None),
        ("drawing_dimension", {"view": 999}, "refused", None),
        ("drawing_dimension", {"view": 0, "strategy": "bogus"}, "refused", None),
        ("drawing_dimension", {"view": 0, "strategy": "baseline"},
         lambda p: p.get("dimensioned") is True and p.get("document_modified") is True, None),
        ("drawing_dimension", {"view": 0, "strategy": "ordinate", "datum": "top_right"},
         lambda p: p.get("dimensioned") is True, None),

        # drawing_insert_image - the locally written PNG. An image POSITION is standard-keyed
        # (millimetres under ISO), and the anchor is bounds-checked against the sheet before
        # anything is placed: 'position_bounds_checked' says the check RAN, which is what separates
        # a passed bound from one that was silently skipped.
        ("drawing_insert_image", {"image_path": png, "x": 150, "y": 100},
         lambda p: p.get("inserted") is True and p.get("position_bounds_checked") is True, None),
        ("drawing_insert_image", {"image_path": png, "x": 40, "y": 40, "scale": 2},
         lambda p: p.get("inserted") is True and p.get("scale") == 2.0
         and p.get("position_bounds_checked") is True, None),
        # off the sheet in the drawing's own unit: an off-sheet insert returns success and renders
        # NOTHING, and an image cannot be read back or moved afterwards - so the refusal names the
        # anchor and the extent it fell outside, and nothing is placed.
        ("drawing_insert_image", {"image_path": png, "x": 9999, "y": 10},
         _refused("off sheet", "which spans 0 to"), None),
        ("drawing_insert_image", {"image_path": png + ".missing", "x": 0, "y": 0}, "refused", None),
        ("drawing_insert_image", {"image_path": png, "x": 0, "y": 0, "scale": 0}, "refused", None),
        ("drawing_insert_image", {"image_path": png}, "refused", None),

        # drawing_add_sketch - one call, five kinds, six curves.
        ("drawing_add_sketch", {"name": "SweepDwgSketch", "geometry": [
            {"kind": "line", "points": [[0, 0], [30, 0], [30, 20]]},
            {"kind": "rectangle", "points": [[40, 5], [70, 25]]},
            {"kind": "circle", "points": [[15, 35]], "radius": 5},
            {"kind": "arc", "points": [[0, 45], [10, 50], [20, 45]]},
            {"kind": "ellipse", "points": [[55, 40], [65, 40], [55, 44]]},
        ]}, lambda p: p.get("curves_landed") == 6, None),
        ("drawing_add_sketch", {"geometry": [{"kind": "hexagon", "points": [[0, 0]]}]},
         "refused", None),
        ("drawing_add_sketch", {"geometry": [{"kind": "circle", "points": [[0, 0]]}]},
         "refused", None),
        ("drawing_add_sketch", {"sheet_name": "NoSuchSheet",
                                "geometry": [{"kind": "circle", "points": [[0, 0]], "radius": 2}]},
         "refused", None),

        # drawing_edit_sheet - adds/copies change the ACTIVE sheet, so these come after the
        # active-sheet beats above.
        ("drawing_edit_sheet", {"action": "add"},
         lambda p: p.get("added") is True and p.get("sheet_count") == p.get("sheet_count_before") + 1,
         None),
        # the second add runs on a drawing that ALREADY holds two sheets, so this is where the
        # listing has to be read: 'sheets' comes back 1-based and contiguous - the numbering
        # drawing_export's sheet_range takes, obtainable nowhere else - with the new sheet directly
        # after the one that was active (the previous add left that one last, so here the position
        # after the active is also the last index).
        ("drawing_edit_sheet", {"action": "add", "new_name": "SweepSheetA"},
         lambda p: p.get("sheet") == "SweepSheetA" and p.get("sheet_count_before", 0) >= 2
         and [s["export_index"] for s in p["sheets"]] == list(range(1, len(p["sheets"]) + 1))
         and next(s["export_index"] for s in p["sheets"] if s["name"] == "SweepSheetA")
         == p["sheet_count_before"] + 1, None),
        ("drawing_edit_sheet", {"action": "add", "new_name": "SweepDup"},
         lambda p: p.get("sheet") == "SweepDup", None),
        ("drawing_edit_sheet", {"action": "add", "new_name": "SweepDup"}, "refused", None),
        ("drawing_edit_sheet", {"action": "rename", "sheet": "SweepSheetA",
                                "new_name": "SweepSheetB"},
         lambda p: p.get("changed") is True and p.get("previous_name") == "SweepSheetA", None),
        # A case variant of another sheet's name: the measured silent no-op, surfaced as an error.
        ("drawing_edit_sheet", {"action": "rename", "sheet": "SweepSheetB", "new_name": "sweepdup"},
         "refused", None),
        ("drawing_edit_sheet", {"action": "rename", "sheet": "SweepSheetB", "new_name": ""},
         "refused", None),
        ("drawing_edit_sheet", {"action": "copy", "sheet": "SweepSheetB", "new_name": "SweepCopy"},
         lambda p: p.get("copied") is True and p.get("copied_from") == "SweepSheetB", None),
        ("drawing_edit_sheet", {"action": "set_size", "sheet": "SweepCopy", "sheet_size": "a3"},
         lambda p: p.get("sheet_size") == "a3", None),
        ("drawing_edit_sheet", {"action": "set_size", "sheet": "SweepCopy", "sheet_size": "b"},
         "refused", None),
        ("drawing_edit_sheet", {"action": "set_orientation", "sheet": "SweepCopy",
                                "orientation": "portrait"},
         lambda p: p.get("orientation") == "portrait", None),
        ("drawing_edit_sheet", {"action": "set_orientation", "sheet": "SweepCopy",
                                "orientation": "landscape"},
         lambda p: p.get("orientation") == "landscape", None),
        ("drawing_edit_sheet", {"action": "set_size", "sheet": "SweepCopy", "sheet_size": "a0"},
         lambda p: p.get("sheet_size") == "a0", None),
        ("drawing_edit_sheet", {"action": "set_orientation", "sheet": "SweepCopy",
                                "orientation": "portrait"}, "refused", None),
        ("drawing_edit_sheet", {"action": "tidy_up"}, lambda p: p.get("tidied") is True, None),
        ("drawing_edit_sheet", {"action": "delete", "sheet": "SweepCopy"},
         lambda p: p.get("deleted") is True, None),
        ("drawing_edit_sheet", {"action": "delete", "sheet": "NoSuchSheet"}, "refused", None),

        # drawing_export - all three formats land non-empty files; a cross-format option refused.
        ("drawing_export", {"format": "pdf", "file_path": pdf1},
         lambda p: (p.get("size_bytes") or 0) > 0, None),
        ("drawing_export", {"format": "dxf", "file_path": dxf},
         lambda p: (p.get("size_bytes") or 0) > 0, None),
        ("drawing_export", {"format": "dwg", "file_path": dwg, "dwg_variant": "autocad"},
         lambda p: (p.get("size_bytes") or 0) > 0, None),
        ("drawing_export", {"format": "dxf", "file_path": dxf, "sheet_range": "1-2"},
         "refused", None),

        # The round-trip: edit the source design, save it, refresh the stale drawing.
        # By URN: opening each drawing loads its source design as an extra same-name loaded doc
        # (measured: two open docs shared the design's exact name), so the display name is refused
        # as ambiguous - correctly - by doc_activate.
        ("doc_activate", {"name": state.get("design_urn") or design_name}, "ok", None),
        ("param_set", {"name": "PlateH", "expression": "16 mm"}, "ok", None),
        ("doc_save", {"description": "PlateH 10 -> 16 for the drawing_update beat"}, "ok", None),
        # By URN: the two staged drawings share a display name.
        ("doc_activate", {"name": iso_urn}, "ok", None),
        ("drawing_update", {},
         lambda p: p.get("updated") is True and p.get("stale_references_before", 0) > 0, None),
        ("drawing_export", {"format": "pdf", "file_path": pdf2},
         lambda p: (p.get("size_bytes") or 0) > 0, None),
    ]
    print(f"-- RUN: user-present drawing beats on '{iso_name}' --")
    rows = _run_steps(steps, ctx)

    # THE STANDARD-KEYED POSITION UNIT, on the staged ASME drawing: an image anchor is INCHES under
    # ASME and millimetres under ISO (both measured by placing an image and reading where it
    # rendered), while Sheet.width/height stay millimetres whatever the standard - so the bound has
    # to convert before it compares. (5,3) in is on a B sheet; (100,50) in is 2540 x 1270 mm, which
    # would render nothing at all.
    asme = state.get("drawing_asme")
    if asme:
        asme_name, asme_urn = asme
        is_error, payload = call("doc_activate", {"name": asme_urn})
        if is_error:
            is_error, payload = call("doc_open", {"file_id": asme_urn, "force_api_open": True})
            if not is_error:
                time.sleep(3)
        if is_error:
            rows.append(("drawing_insert_image", "skipped",
                         f"the staged ASME drawing ({asme_urn}) could not be reached: {payload}"))
            print(f"  {'skipped':18} {'drawing_insert_image':26} ASME drawing unreachable")
        else:
            print(f"-- RUN: the ASME position-unit beats on '{asme_name}' --")
            rows += _run_steps([
                ("drawing_insert_image", {"image_path": png, "x": 100, "y": 50},
                 _refused("off sheet", "which spans 0 to"), None),
                ("drawing_insert_image", {"image_path": png, "x": 5, "y": 3},
                 lambda p: p.get("inserted") is True
                 and p.get("position_bounds_checked") is True, None),
            ], ctx)
    rc = _report(rows, "run")
    if rc == 0:
        print("\nEYEBALL (the human half of this verification):")
        print(f"  {pdf1}   - dimensions on view 0, two orange markers, the sketch shapes, the sheets")
        print(f"  {pdf2}   - the plate thickness now 16 mm after drawing_update")
    return rc


def get_only():
    """READ-ONLY drawing_get beats against whatever drawing is ACTIVE - no staging, nothing
    written. The read tool's live verification: sheets listed by export_index, per-view rows,
    the one-sheet scope, and the miss refusal naming the available sheets."""
    health_gate()
    ctx = {}
    steps = [
        ("drawing_get", {}, "ok",
         ("sheet_names", lambda p: [s.get("name") for s in (p.get("sheets") or []) if s])),
        # the payload's own shape claims: contiguous 1-based export indices, exactly one active
        ("drawing_get", {}, lambda p: isinstance(p.get("sheet_count"), int), None),
        ("drawing_get", {}, lambda p: (
            [s.get("export_index") for s in p.get("sheets") or []]
            == list(range(1, len(p.get("sheets") or []) + 1))), None),
        ("drawing_get", {}, lambda p: (
            sum(1 for s in p.get("sheets") or [] if s.get("is_active")) == 1), None),
        # view_rows agree with each sheet's own view count (uncapped sheets only)
        ("drawing_get", {"include": ["views"]}, lambda p: all(
            isinstance(s.get("view_rows"), list) and len(s["view_rows"]) == s.get("views")
            for s in p.get("sheets") or [] if (s.get("views") or 0) <= 50), None),
        ("drawing_get", lambda c: {"sheet": (c.get("sheet_names") or [""])[0]},
         lambda p: len(p.get("sheets") or []) == 1, None),
        ("drawing_get", {"sheet": "NoSuchSheet_XYZ"}, "refused", None),
        ("drawing_get", {"include": ["dimensions"]}, "refused", None),
    ]
    print("-- GET-ONLY: drawing_get against the active drawing --")
    rows = _run_steps(steps, ctx)
    return _report(rows, "get-only")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--stage", action="store_true")
    g.add_argument("--run", action="store_true")
    g.add_argument("--get-only", action="store_true", dest="get_only",
                   help="read-only drawing_get beats against the ACTIVE drawing (no staging)")
    args = ap.parse_args()
    sys.exit(stage() if args.stage else (get_only() if args.get_only else run()))
