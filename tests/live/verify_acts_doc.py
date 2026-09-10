# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""ACT rows: the overture that opens the document, the showcase, and the finale that discards it.

The orientation reads and the one `doc_new` at the top; the presentation act - beauty shots, the
view verbs, the renames and the export/import round trips - which runs before the machining acts
so the CAM job is what the sweep ends on; and the discard. `reload_smoke` is the post-run beat
run() fires after every act: the add-in reload, which restarts the server and so can be no step.
"""

import json
import time
import urllib.request

from verify_core import (
    BASE, EXPORT_DIR, NOTE_MAX, SERVER_NAME, SVG_PATH, _RECALL, _activated, _ctx_get,
    _document_closed, _document_read, _exported_bytes, _extruded, _fg, _home_address,
    _home_document, _imported_curves, _imported_sketches, _made_component, _measured,
    _new_document, _num, _recall, _refused, _watch, facade)
from verify_layout import _DRIFT_CHUNKS, drift_row


# --- the SECOND document: what puts doc_activate in the always-on receipt -----------------------
# doc_new mints an UNSAVED document with a session handle that addresses it exactly.

def _story_address(p):
    """Return the story document's exact handle while recording the open count."""
    _RECALL["open_before"] = p.get("open_count")
    return _home_address(p)


def _scratch_opened_beside_it(p):
    """doc_get after the scratch doc_new: the session holds one MORE document than it did, and the
    ACTIVE one is the scratch - a new document that replaced the story one would read the same
    count and the same address."""
    rows = [r for r in (p.get("open_documents") or []) if r.get("is_active")]
    here = _home_address(p) if len(rows) == 1 else None
    return _measured("the scratch document opened BESIDE the story document",
                     {"open_count": p.get("open_count"), "open_before": _RECALL["open_before"],
                      "scratch": here, "story": _RECALL["story_doc"]},
                     _num(p.get("open_count"))
                     and p["open_count"] == _RECALL["open_before"] + 1
                     and here is not None and here != _RECALL["story_doc"])


def _scratch_gone_story_active(p):
    """doc_get after the scratch is closed: the session is back to the count it opened with, and
    the story document is active at the address it answered to all along."""
    rows = [r for r in (p.get("open_documents") or []) if r.get("is_active")]
    here = _home_address(p) if len(rows) == 1 else None
    return _measured("the scratch is closed and the session is back on the story document",
                     {"open_count": p.get("open_count"), "open_before": _RECALL["open_before"],
                      "active": here, "story": _RECALL["story_doc"]},
                     p.get("open_count") == _RECALL["open_before"]
                     and here == _RECALL["story_doc"])


# The scratch beat, in the order that leaves nothing behind: read the address, open the second
# document, switch both ways by exact handle, come home, and close the scratch by its own handle -
# never whatever is in front, which is what a bare doc_close would take.
_SCRATCH_DOCUMENT = [
    ("doc_get", {}, _home_document, ("story_doc", _recall("story_doc", _story_address))),
    ("doc_new", {}, _new_document, None),
    ("doc_get", {}, _scratch_opened_beside_it, ("scratch_doc", _home_address)),
    ("doc_activate", lambda c: {"name": _ctx_get(c, "story_doc", "the story document")},
     _activated(), None),
    ("doc_activate", lambda c: {"name": _ctx_get(c, "scratch_doc", "the scratch document")},
     _activated(), None),
    ("doc_activate", lambda c: {"name": _ctx_get(c, "story_doc", "the story document")},
     _activated(), None),
    ("doc_close", lambda c: {"name": _ctx_get(c, "scratch_doc", "the scratch document"),
                             "save_changes": False}, _document_closed, None),
    ("doc_get", {}, _scratch_gone_story_active, None),
]


# --- ACT 0: OVERTURE - orient, then open the one document the whole story lives in -------------
_OVERTURE = [
    ("doc_new", {}, _new_document, None),
    ("workspace_orient", {}, "ok", ("fusion_version", lambda p: p["fusion_version"])),
    # the family map is a live registry walk: a family whose module failed to register is ABSENT
    # here, not merely uncounted, and every family it does list has to carry an entry tool.
    ("sys_capability_map", {},
     lambda p: ({"cam", "mesh", "model", "sketch", "surface", "view"}
                <= {f["family"] for f in p["families"]}
                and all(f["tool_count"] >= 1 and f["entry_tool"] for f in p["families"])
                and p["tool_count"] >= 150), None),
    # the read stamp is for DOCUMENT reads: a tool that answers off the registry rather than the
    # active design carries no 'active_document' key at all (design_get's own beat in the FINALE is
    # the other half of this pair).
    ("sys_find_tool", {"query": "revolve"},
     lambda p: "active_document" not in p and p.get("tool_count", 0) > 0, None),
    # the introspection FOUND the class in the module it lives in: an adsk submodule that would not
    # import is skipped silently, and the search then answers ok with nothing in it.
    ("sys_get_api_doc", {"searchPattern": "RevolveFeatures", "max_results": 3},
     lambda p: (any(c["name"] == "RevolveFeatures" and c["namespace"] == "adsk.fusion"
                    for c in p["classes"])
                and p["counts"]["classes"] == len(p["classes"])), None),
    # the packaged design guidance, the way a client with tools and no skill loader reads it: the
    # index, then ONE section - its rule records keyed by the ids the canonical document carries,
    # beside the content hash that says which version answered.
    ("sys_get_guidance", {},
     lambda p: (p.get("recipes") and all(r.get("id") and r.get("use_when") for r in p["recipes"])
                and all("steps" not in r for r in p["recipes"])), None),
    ("sys_get_guidance", {"section": "assemble"},
     lambda p: ({"connected-reference-path", "exercise-the-mechanism"}
                <= {r.get("id") for r in (p.get("rules") or [])}
                and len(p.get("sha256") or "") == 64
                and all(c in "0123456789abcdef" for c in p.get("sha256") or "")), None),
    # and the third read: ONE recipe whole - the ordered steps, each with what to read back, and
    # the bar. This is the id cam_get's strategies note tells a caller to ask for.
    ("sys_get_guidance", {"recipe": "manufacture-choose-a-strategy"},
     lambda p: (p["recipe"]["id"] == "manufacture-choose-a-strategy"
                and len(p["recipe"]["steps"]) >= 3
                and all(s.get("tool") and s.get("read_back") for s in p["recipe"]["steps"])
                and p["recipe"]["bar"]["measure"] and p["recipe"]["bar"]["eyes"]), None),
    # each row's is_active is read off the workspace itself and a read that raises publishes null,
    # so exactly one row flagged active - and it is the one 'active_workspace' names - is the read.
    ("view_list_workspaces", {},
     lambda p: (p["workspace_count"] == len(p["workspaces"])
                and [w["name"] for w in p["workspaces"] if w["is_active"]]
                == [p["active_workspace"]]), None),
    ("view_set", {"action": "orient", "orientation": "iso-top-right"}, "ok", None),
    # camera projection: a perspective orient carries the angle through to the camera and reads it
    # back; the follow-up orient returns the projection to orthographic for the rest of the story.
    ("view_set", {"action": "orient", "orientation": "iso-top-right", "projection": "perspective",
                  "perspective_angle_deg": 45},
     lambda p: p.get("applied", {}).get("perspective_angle_deg") == 45.0, None),
    ("view_set", {"action": "orient", "orientation": "iso-top-right", "projection": "orthographic"},
     "ok", None),
    # capture options: the transparent + anti-aliased overload at an explicit size produces an image.
    ("view_screenshot", {"width": 320, "height": 240, "transparent_background": True,
                         "anti_aliased": True}, "ok", None),
    ("sys_get_selection", {}, "refused", None),   # nothing picked yet - the expected empty-selection refusal
    # PREFERENCES: read the application's own configuration, round-trip ONE invisible member, put it
    # back. The sweep leaves the application exactly as it found it, so the restore is an ASSERTION
    # (previous/now inside its own payload), not cleanup - it runs whether or not the bump asserted.
    # recoverSaveScanFrequency is the round-trip member: integer-exact, invisible to a watching
    # operator, and it perturbs no other beat's formatting.
    ("sys_get_preferences", {},
     lambda p: (p["preferences"]["display"]["generalPrecision"]["value"] is not None
                and p["preferences"]["general"]["isAutomaticVersioningEnabled"]["tier"] == "W"
                and p["preferences"]["products"]["Design"]["isFirstComponentGroundToParent"]["value"]
                is not None), None),
    # the enum FAMILY names are string literals inside the member table, so a typo degrades to a bare
    # int that no offline test can see - only a live decode of two known members catches it.
    ("sys_get_preferences", {"include": ["display", "general"]},
     lambda p: (p["preferences"]["display"]["materialDisplayUnit"].get("enum")
                == "MetricStandardDisplayUnits"
                and p["preferences"]["general"]["defaultModelingOrientation"].get("enum")
                == "ZUpModelingOrientation"), None),
    ("sys_get_preferences", {"include": ["compatibility"]},
     lambda p: p["preferences"]["compatibility"]["recoverSaveScanFrequency"]["value"] > 0,
     ("pref_scan",
      lambda p: p["preferences"]["compatibility"]["recoverSaveScanFrequency"]["value"])),
    # the members that RAISE on read are published as null + named in 'unreadable', never dropped -
    # all THREE of the raising members the [F21] census found on this build, and none of them
    # miscategorised as a member the build does not carry ('unknown_members' must be absent).
    ("sys_get_preferences", {"include": ["graphics"]},
     lambda p: ("graphicsPreset" in p["preferences"]["graphics"]
                and set(p.get("unreadable") or []) >= {"graphics.autoThrottleEffects",
                                                       "graphics.degradedSelectionDisplayStyle",
                                                       "graphics.isLimitEffectsDuringNavigation"}
                and "unknown_members" not in p), None),
    ("sys_set_preferences", lambda c: {"member": "compatibility.recoverSaveScanFrequency",
                                       "value": _ctx_get(c, "pref_scan", "the scan frequency") + 1},
     lambda p: p["now"] == p["previous"] + 1, None),
    ("sys_set_preferences", lambda c: {"member": "compatibility.recoverSaveScanFrequency",
                                       "value": _ctx_get(c, "pref_scan", "the scan frequency")},
     lambda p: p["now"] == p["previous"] - 1, None),   # RESTORED - asserted, not cleanup
    # the documented "greater than 0" bound is refused BEFORE the assignment, so nothing is written
    # and there is nothing to restore.
    ("sys_set_preferences", {"member": "compatibility.recoverSaveScanFrequency", "value": 0},
     "refused", None),
    # a tier-R member names the member and the reason, with nothing written.
    ("sys_set_preferences", {"member": "network.proxyHost", "value": "127.0.0.1"}, "refused", None),
] + _SCRATCH_DOCUMENT

# --- THE SHOWCASE: the finished fixture photographed, renamed, exported and read back -----------
# It runs BEFORE the machining acts so the sweep ends on the CAM job and its post, which is the
# deliverable. Nothing here touches the part's name or its geometry, so the CAM acts that follow
# address exactly what the modelling acts built.
_SHOWCASE = [
    ("model_extrude", {"sketch_name": "NoSuchSketch", "distance": 5}, "refused", None),   # guard probe
    ("param_set", {"name": "", "expression": "1"}, "refused", None),                      # guard probe
    # THE SCRATCH FIELD OFF THE PICTURES: every act above this one left its sketches on screen, and
    # the shots below are of the fixture. The FOLDER bulb, so no entity's own visibility is
    # disturbed; the finale puts it back after the machining acts have finished with it too.
    ("view_set", {"action": "display", "categories": ["sketches"], "visible": False},
     lambda p: p.get("visible") is False, None),
    # the machined part in its fixture - the end state of the modelling movement is the vise
    # holding the billet the bracket is cut from.
    _watch(["ViseBase:1", "STOCK:1"]),
    # the summary counts the views actually CAPTURED - one that failed to orient or capture is
    # skipped, not failed - and names the camera it could not put back after a read.
    ("view_screenshot_multi", {"views": ["iso-top-right", "front"], "width": 500, "height": 400},
     lambda p: ("Captured 2 view(s): iso-top-right, front" in str(p)
                and "could NOT be put back" not in str(p)), None),
    # THE VIEW VERBS, all on the finished fixture. ONE framed orient sets the subject; every preset
    # after it carries fit=false and no focus, so the camera ROTATES about what is already framed
    # instead of re-fitting per preset. That is the difference between a turntable and ten separate
    # zoom-outs - the vise stays the same size in the same place and only the angle changes. It also
    # keeps the tour silent: the runner shoots a frame for a camera row that names a focus, so ten
    # focused orients would write ten near-identical screenshots.
    # The tour opens with a snapshot and closes on 'restore', which is what makes it checkable -
    # camera, style and every visibility bulb come back to the state the tour started from.
    ("view_set", {"action": "snapshot"}, "ok", None),
    ("view_set", {"action": "orient", "orientation": "front", "focus": ["ViseBase:1", "STOCK:1"]},
     "ok", None),
    ("view_set", {"action": "orient", "orientation": "back", "fit": False}, "ok", None),
    ("view_set", {"action": "orient", "orientation": "left", "fit": False}, "ok", None),
    ("view_set", {"action": "orient", "orientation": "right", "fit": False}, "ok", None),
    ("view_set", {"action": "orient", "orientation": "top", "fit": False}, "ok", None),
    ("view_set", {"action": "orient", "orientation": "bottom", "fit": False}, "ok", None),
    ("view_set", {"action": "orient", "orientation": "iso-top-left", "fit": False}, "ok", None),
    ("view_set", {"action": "orient", "orientation": "iso-bottom-right", "fit": False}, "ok", None),
    ("view_set", {"action": "orient", "orientation": "iso-bottom-left", "fit": False}, "ok", None),
    ("view_set", {"action": "orient", "orientation": "iso-top-right", "fit": False}, "ok", None),
    # every visual style the tool offers, held on the one hero angle. 'current' on view_screenshot is
    # the no-move capture - the only way to shoot what the camera already frames, since a NAMED view
    # refits the whole model.
    ("view_set", {"action": "style", "style": "wireframe"}, "ok", None),
    ("view_set", {"action": "style", "style": "wireframe-edges"}, "ok", None),
    ("view_set", {"action": "style", "style": "wireframe-hidden-edges"}, "ok", None),
    ("view_set", {"action": "style", "style": "shaded-hidden-edges"}, "ok", None),
    ("view_set", {"action": "style", "style": "shaded"}, "ok", None),
    ("view_screenshot", {"view": "current", "width": 500, "height": 400}, "ok", None),
    ("view_set", {"action": "style", "style": "shaded-edges"}, "ok", None),
    # visibility, in the order that leaves nothing hidden behind: isolate the stock, hide one jaw,
    # show it again, then drop the isolation. Each verb reports what it reached.
    ("view_set", {"action": "isolate", "target": "STOCK:1"}, "ok", None),
    ("view_set", {"action": "clear_isolation"}, "ok", None),
    ("view_set", {"action": "hide", "target": "JawMoving:1"}, "ok", None),
    ("view_set", {"action": "show", "target": "JawMoving:1"}, "ok", None),
    # a persistent Named View: parked, listed among the document's own, and re-applied.
    ("view_set", {"action": "save_view", "view_name": "SweepHero"}, "ok", None),
    # the camera has to LEAVE the saved view for re-applying it to prove anything - in place, so the
    # proof does not cost a fit-to-whole-model on the way out and another on the way back.
    ("view_set", {"action": "orient", "orientation": "bottom", "fit": False}, "ok", None),
    ("view_set", {"action": "apply_view", "view_name": "SweepHero"}, "ok", None),
    ("view_set", {"action": "list_views"},
     lambda p: "SweepHero" in [v.get("name") for v in (p.get("named_views") or [])], None),
    ("view_set", {"action": "restore"}, "ok", None),
    # one contact sheet, four presets. This tool walks the camera per view and fits each one, so its
    # cost on screen is one zoom-out per view in the list - a seven-view sheet and an 'all' sheet
    # behind it read as the camera coming loose right at the end of the run. Four is enough to show
    # the sheet is a sheet; the orientation vocabulary is already covered by the turntable above,
    # which pays nothing to do it.
    ("view_screenshot_multi", {"views": ["back", "bottom", "left", "iso-bottom-left"],
                               "width": 300, "height": 240}, "ok", None),
    # THE RENAMES, last: a rename invalidates every row that names its target, so they run once the
    # build is done. A two-body cameo carries the dedupe beat - it needs a SIBLING pair, and the
    # machined part is a component of one body.
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "TwinCameo", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "TwinA"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 1400, "y1": 200,
                                           "x2": 1430, "y2": 230}],
                             "sketch_name": "TwinA"}, "ok", None),
    ("model_extrude", {"sketch_name": "TwinA", "profile_index": 0, "distance": 10}, _extruded, None),
    ("sketch_create", {"plane": "xy", "name": "TwinB"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 1450, "y1": 200,
                                           "x2": 1480, "y2": 230}],
                             "sketch_name": "TwinB"}, "ok", None),
    ("model_extrude", {"sketch_name": "TwinB", "profile_index": 0, "distance": 10}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("find_geometry", {"target": "TwinCameo", "kind": "planar_face", "nearest_to": [1415, 215, 10],
                       "max_results": 1}, "ok", _fg("twin_a")),
    ("find_geometry", {"target": "TwinCameo", "kind": "planar_face", "nearest_to": [1465, 215, 10],
                       "max_results": 1}, "ok", _fg("twin_b")),
    ("design_set_name", lambda c: {"target": _ctx_get(c, "twin_a", "the first twin body"),
                                   "new_name": "TwinPlate"},
     lambda p: p.get("name") == "TwinPlate" and p.get("kind") == "body"
     and p.get("deduped") is False, None),
    # the name its sibling already holds: Fusion dedupes it to 'TwinPlate (1)' and THAT is the name
    # the payload has to publish - a payload echoing the request would read 'TwinPlate' here.
    ("design_set_name", lambda c: {"target": _ctx_get(c, "twin_b", "the second twin body"),
                                   "new_name": "TwinPlate"},
     lambda p: p.get("deduped") is True and p.get("name") == "TwinPlate (1)", None),
    # an OCCURRENCE target renames the COMPONENT behind it, and the instance name follows.
    ("design_set_name", {"target": "TwinCameo:1", "new_name": "TwinAssy"},
     lambda p: p.get("kind") == "component" and p.get("occurrence_name") == "TwinAssy:1", None),
    # THE OCCURRENCE FAN-OUT, in the two-step shape that discriminates ([F64]): colour ONE body
    # directly (colour A), then write the OCCURRENCE in a different colour (colour B). The
    # occurrence's own read-back agrees with the write whether or not a body took it, so the bodies
    # are re-read: the body holding its own override kept colour A and must come back under
    # 'bodies_not_reached' - NOT under applied_to. Both colours are minted from one base asset, so
    # they share an Appearance.id and differ only by NAME - an id-only comparison lists the
    # overridden body as reached, which is exactly the defect this beat stands on.
    ("appearance_set", {"target": "TwinPlate", "color": "#C2185B"},
     lambda p: p.get("kind") == "body" and p.get("applied_to") == ["TwinPlate"], None),
    ("appearance_set", {"target": "TwinAssy:1", "color": "#00897B"},
     lambda p: any(o.get("body") == "TwinPlate" for o in (p.get("bodies_not_reached") or []))
     and "TwinPlate" not in (p.get("applied_to") or [])
     and "TwinPlate (1)" in (p.get("applied_to") or []), None),
    # the same shape where the overridden body is the occurrence's ONLY one: nothing was reached, so
    # the call is a refusal naming the body to colour directly - never an ok on the occurrence's own
    # agreeable read-back.
    ("model_create_component", {"name": "SoloColor", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "SoloS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 1500, "y1": 200,
                                           "x2": 1530, "y2": 230}],
                             "sketch_name": "SoloS"}, "ok", None),
    ("model_extrude", {"sketch_name": "SoloS", "profile_index": 0, "distance": 10}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("find_geometry", {"target": "SoloColor", "kind": "planar_face", "nearest_to": [1515, 215, 10],
                       "max_results": 1}, "ok", _fg("solo_face")),
    ("design_set_name", lambda c: {"target": _ctx_get(c, "solo_face", "the solo body"),
                                   "new_name": "SoloBody"},
     lambda p: p.get("kind") == "body" and p.get("name") == "SoloBody", None),
    ("appearance_set", {"target": "SoloBody", "color": "#C2185B"},
     lambda p: p.get("kind") == "body", None),
    ("appearance_set", {"target": "SoloColor:1", "color": "#00897B"},
     _refused("reached NONE", "SoloBody"), None),
    # A COMPONENT rename, then the component re-found through the name that landed - the rename
    # reaches the browser name every other tool addresses it by. It is taken on a cameo rather than
    # on the machined part, because the CAM acts after this one address the part by the name the
    # modelling acts gave it.
    ("design_set_name", {"target": "SoloColor:1", "new_name": "SoloRenamed"},
     lambda p: p.get("name") == "SoloRenamed" and p.get("previous_name") == "SoloColor"
     and p.get("kind") == "component", None),
    ("find_geometry", {"target": "SoloRenamed", "kind": "planar_face", "max_results": 1}, "ok", None),
    # re-asking for the name it already holds mutates nothing and says so.
    ("design_set_name", {"target": "SoloRenamed", "new_name": "SoloRenamed"},
     lambda p: p.get("changed") is False, None),
    ("design_set_name", {"target": "", "new_name": "X"}, "refused", None),
    # the ROOT component is refused UP FRONT: its name is the document's, and the platform's own
    # raise would abort the transaction around it. The root name is read off the tree, never guessed.
    ("design_get", {"include": ["tree"]}, "ok", ("root_name", lambda p: p["tree"]["root"])),
    ("design_set_name", lambda c: {"target": _ctx_get(c, "root_name", "the root component name"),
                                   "new_name": "RootRename"}, "refused", None),
    # every DOCUMENT read is stamped with the document it read from, so two tallies taken in two
    # documents are distinguishable. (sys_find_tool, the registry read in the overture, carries no
    # such key - it never touched the design.)
    ("design_get", {},
     lambda p: bool((p.get("active_document") or {}).get("name")), None),
    # the assignable catalog, at both zoom levels: the document's own entries plus a count-only
    # census of every loaded library, then ONE library paged by name_filter/max_results. A library
    # name that is not loaded is refused with the loaded names listed.
    ("design_get", {"include": ["materials"]}, "ok", None),
    ("design_get", {"include": ["appearances"], "library": "Fusion Appearance Library",
                    "name_filter": "paint", "max_results": 5}, "ok", None),
    ("design_get", {"include": ["appearances"], "library": "NoSuchLibrary"}, "refused", None),
    # EVERY neutral-CAD format the exporter declares, one file per factory, each measured ON DISK -
    # a build missing a factory, or one that reports success and writes nothing, fails here rather
    # than at whoever opens the file. The formats ImportManager can read then come straight back in
    # with the format named EXPLICITLY: doc_insert_import refuses a format that contradicts the
    # file's extension, so naming it checks that the extension the exporter chose is the one the
    # importer expects.
    ("design_export", {"format": "iges", "file_path": EXPORT_DIR + "/fmt_bracket",
                       "target": "Bracket"}, _exported_bytes, None),
    # SAT is deliberately NOT here. Measured on this build: the FIRST createSATExportOptions export
    # in a Fusion session writes its file, and every one after it returns false having written
    # nothing - on any target, in a fresh document holding one box, and into a directory no .sat has
    # ever been written to, while IGES and SMT through the same call shape keep working in that same
    # session. The tool reports the failure honestly, which is the behaviour that matters; what the
    # sweep cannot do is assert an outcome that depends on whether anything exported SAT earlier.
    ("design_export", {"format": "smt", "file_path": EXPORT_DIR + "/fmt_bracket",
                       "target": "Bracket"}, _exported_bytes, None),
    # F3D of a COMPONENT is the row where execute()'s bool and the disk disagree: the archive lands
    # while execute() answers false, so the tool verifies the file and discloses the bool under
    # 'execute_returned_false' - and this row stands on the size on disk, as its siblings do.
    ("design_export", {"format": "f3d", "file_path": EXPORT_DIR + "/fmt_bracket",
                       "target": "Bracket"}, _exported_bytes, None),
    ("design_export", {"format": "obj", "file_path": EXPORT_DIR + "/fmt_bracket",
                       "target": "Bracket"}, _exported_bytes, None),
    ("design_export", {"format": "3mf", "file_path": EXPORT_DIR + "/fmt_bracket",
                       "target": "Bracket"}, _exported_bytes, None),
    # USD lands as .usdz whatever extension the path carries - Fusion appends its own - so the tool
    # publishes the path it actually wrote.
    ("design_export", {"format": "usd", "file_path": EXPORT_DIR + "/fmt_bracket",
                       "target": "Bracket"},
     lambda p: _exported_bytes(p) is True and str(p.get("file_path", "")).endswith(".usdz"), None),
    # STL with the units baked in: the one format carrying its own unit, so the knob is set and read
    # back off the options object that LANDED. The single-file path publishes 'options_applied' and
    # 'options_requested'; this predicate reads only the applied value - what the options object
    # that wrote THIS file read back. Reading 'options_requested' here would only echo this step's
    # own two arguments back at it.
    ("design_export", {"format": "stl", "file_path": EXPORT_DIR + "/fmt_bracket_in",
                       "target": "Bracket", "stl_units": "in", "stl_binary": False},
     lambda p: _exported_bytes(p) is True
     and (p.get("options_applied") or {}).get("stl_units") == "in"
     and (p.get("options_applied") or {}).get("stl_binary") is False, None),
    # the 2D branch: a sketch written as DXF, then read back onto a named plane as sketches.
    ("design_export", {"format": "dxf", "file_path": EXPORT_DIR + "/fmt_twin",
                       "dxf_sketch": "TwinA"}, _exported_bytes, None),
    ("doc_insert_import", {"file_path": EXPORT_DIR + "/fmt_twin.dxf", "format": "dxf",
                           "plane": "xy"}, _imported_sketches, None),
    # SVG lands in an EXISTING sketch (there is no component-level SVG import), so one is made for it.
    ("sketch_create", {"plane": "xy", "name": "SvgImport"}, "ok", None),
    ("doc_insert_import", {"file_path": SVG_PATH, "format": "svg", "sketch": "SvgImport"},
     _imported_curves, None),
    # NO further solid re-imports. An import lands its geometry at the coordinates the FILE carries,
    # so re-importing a part into the design it came from drops a second copy exactly on top of the
    # original - measured: one per format left FIVE coincident copies on the machined part, which is
    # the one thing the CAM shot is of. Every format is proven by its own measured bytes on disk,
    # which costs the scene nothing; the STEP round trip the deliverables act runs is the visible
    # proof that a written file reads back.
    # the contradiction the explicit format exists to catch, on a file that is certainly there.
    ("doc_insert_import", {"file_path": EXPORT_DIR + "/fmt_bracket.smt", "format": "step"},
     "refused", None),
    # DRAWING GUARDS: every one of these is settled before the tool looks for a cloud source, so
    # they run on the story document exactly as they would on a saved one, and each refuses for the
    # reason it names with no drawing created. The creation path itself is cloud-tier (it needs a
    # saved source design) and stays out of the default sweep.
    # adsk.drawing carries no plain shaded member - shading pairs with hidden or with visible edges.
    ("drawing_create", {"view_style": "shaded"}, "refused", None),
    # center_line / center_mark have NO enum family to reach on this build (the namespace carries
    # CenterLineOptions / CenterMarkOptions classes instead), so a non-default request is refused
    # rather than dropped by a best-effort setter.
    ("drawing_create", {"center_line": "holes"}, "refused", None),
    ("drawing_create", {"center_mark": "fillets"}, "refused", None),
    ("drawing_create", {"tangent_edges": "partial"}, "refused", None),
    # Fusion gates manual creation on a template carrying view-placeholder information, and the
    # failure escapes an enclosing try/except - so the mode is refused up front instead of called
    # into, and the session is still healthy afterwards.
    ("drawing_create", {"creation_mode": "manual"}, "refused", None),
    ("workspace_orient", {}, "ok", None),
    # the API silently IGNORES a sheet size from the other standard, so the pairing is guarded here.
    ("drawing_create", {"standard": "asme", "sheet_size": "a2"}, "refused", None),
]

# THE LAYOUT DRIFT GATE, on the field ACT 9 has finished dressing. verify_layout._MEASURED_BOX
# records where the chunks really are and the framing pass widens every frame from it, so a layout
# move that outdates the table fails HERE rather than ageing it silently.
_SHOWCASE += [drift_row(chunk) for chunk in _DRIFT_CHUNKS]


# --- FINALE: put the workspace and the browser back, then DISCARD the document on camera --------
# Everything that must run LAST and nothing else. The machining acts leave Manufacture active and
# the sketch folders hidden, so the two restores are the sweep leaving the application as it found
# it; the document identity is read while it still answers, and then it goes.
_FINALE = [
    # the CAM acts left Manufacture active, so this is a real switch: 'activation_verified' is true
    # only where isActive or the UI's own active workspace read the change back.
    ("view_switch_workspace", {"workspace": "design"},
     lambda p: p.get("switched") is True and p.get("activation_verified") is True, None),
    # CAM hid the sketch folders for the machining movement; this is where they come back.
    ("view_set", {"action": "display", "categories": ["sketches"], "visible": True},
     lambda p: p.get("visible") is True, None),
    ("doc_get", {}, _document_read, None),
    ("doc_close", {"save_changes": False}, _document_closed, None),
]


# --- the reload beat: the one tool no act can hold, driven after every act has run ---------------
# sys_reload_addin restarts the server the sweep is talking to, so it can be no step: the call
# after it would reach a socket that is coming down. It is a post-run beat instead, and what it
# has to establish is that the restart HAPPENED. The reload is DEFERRED - the handler starts a
# timer and returns while the server is still answering - so a /health read taken when the call
# comes back describes the state before the teardown, and would pass identically against a server
# that never left. Watching /health go DOWN and then answer again is what tells those apart.
_RELOAD_PROBE_GAP_S = 0.25
_RELOAD_PROBE_TIMEOUT_S = 1.0
# Attempt budgets, not deadlines, so the beat's cost is bounded the way poll_generation's is: 40
# probes to catch the teardown and 60 to see the re-import answer, a quarter-second apart, each
# probe itself capped by the timeout above. A budget that runs out ends the beat, never the wait.
_RELOAD_DOWN_POLLS = 40
_RELOAD_UP_POLLS = 60
# The smoke read: a registry search. sys_find_tool is registered run_on_main_thread=False, so it
# answers off the registry rather than queuing behind Fusion's main thread, and entry.start()
# collects and registers every tool BEFORE it starts the HTTP server - so a /health that answers
# is a registry already populated, and this read is of the restarted add-in, not a race with it.
_RELOAD_SMOKE_QUERY = "reload addin"


def _server_answers(timeout=_RELOAD_PROBE_TIMEOUT_S):
    """True when GET /health answers right now AS THIS SERVER. Every other outcome is False -
    refused, reset, timed out, or a different server holding the port - because none of them is
    this add-in answering, and the caller reads the two states apart, never the reason."""
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    return data.get("server") == SERVER_NAME


def _poll_health(up, polls):
    """Poll /health until it reads `up` (True = answering as this server, False = not), bounded by
    `polls` attempts. True when that state was OBSERVED, False when the budget ran out - a budget
    that runs out is never read as the state it was waiting for."""
    answers = facade("_server_answers")
    for i in range(polls):
        if i:
            time.sleep(_RELOAD_PROBE_GAP_S)
        if bool(answers()) is up:
            return True
    return False


def reload_smoke(rows, notes, valued=None, down_polls=_RELOAD_DOWN_POLLS,
                 up_polls=_RELOAD_UP_POLLS, expected_attestation=None):
    """Reload the add-in, watch the server go down and come back, and read the fresh registry.

    Appends ONE row for sys_reload_addin and, only on a restart it OBSERVED end to end, registers
    the tool in 'valued' - the receipt's covered bucket - the same way a STEPS value predicate
    does. Its pass reads values: the scheduling sentence off the call, the two /health states, and
    the tool's own name out of the restarted registry.

    A beat that cannot confirm the restart appends NO row and prints why. The tool then falls to
    its EXCLUDED entry and the receipt keeps a skipped row, which is the honest reading of what
    happened: nothing was observed to claim, and the evidence every other tool's rows carry was
    gathered before this beat ran and is untouched by it."""
    # the wire read and the shot-list note as the facade holds them - see verify_core.facade
    call, STORY = facade("call"), facade("STORY")
    try:
        is_error, payload = call("sys_reload_addin", {})
    except Exception as e:
        # the teardown can cut the response short; nothing was observed either way
        is_error, payload = True, f"the reload call did not come back: {e}"
    if is_error or "Reload scheduled" not in str(payload):
        print(f"  reload beat: no reload was scheduled - {str(payload)[:NOTE_MAX]}")
        return
    if not _poll_health(False, down_polls):
        print(f"  reload beat: /health kept answering across {down_polls} probes - the restart was "
              "not observed, so the run claims nothing for it")
        return
    if not _poll_health(True, up_polls):
        print(f"  reload beat: /health did not answer again within {up_polls} probes - the add-in "
              "is down; start it from Fusion's Scripts and Add-Ins dialog (Shift+S)")
        return
    current_attestation = None
    if expected_attestation is not None:
        current_health = facade("health_gate")()
        current_attestation = facade("attestation_identity")(current_health)
        same_build = all(current_attestation and current_attestation.get(field)
                         == expected_attestation.get(field)
                         for field in ("implementation_fingerprint", "schema_fingerprint"))
        new_generation = all(current_attestation and current_attestation.get(field)
                             != expected_attestation.get(field)
                             for field in ("load_id", "session_id"))
        if not same_build or not new_generation:
            print("  reload beat: the new server did not prove the expected build and new session")
            return
        if "sys_reload_addin" not in facade("registered_tools")(current_health):
            print("  reload beat: the restarted tools/list did not return sys_reload_addin")
            return
    try:
        found = call("sys_find_tool", {"query": _RELOAD_SMOKE_QUERY})[1]
    except Exception as e:
        found = f"the registry read did not come back: {e}"
    # a refusal answers with the error TEXT rather than a payload, so the match list reads empty
    # off it and needs no separate error flag
    matches = found.get("tools") or [] if isinstance(found, dict) else []
    names = [m.get("tool") for m in matches if isinstance(m, dict)]
    if "sys_reload_addin" not in names:
        print("  reload beat: the restarted server answered, but its registry did not return "
              f"sys_reload_addin - {str(found)[:NOTE_MAX]}")
        return
    rows.append(("sys_reload_addin", "pass",
                 f"/health stopped answering and answered again as {SERVER_NAME}; the restarted "
                 f"registry returned {len(names)} match(es) for '{_RELOAD_SMOKE_QUERY}', "
                 "sys_reload_addin among them"))
    notes["sys_reload_addin"] = STORY.get("sys_reload_addin", "")
    if valued is not None:
        valued.add("sys_reload_addin")
    return current_attestation
