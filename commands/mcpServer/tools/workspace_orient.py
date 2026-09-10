# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The COLD-BOOT orientation read: one report of the open document - what it is, what it contains
(counts plus a depth-1 browser digest), its health, whether CAM data exists, and the pointers naming
the targeted tool for each area."""

import re

import adsk.core
import adsk.fusion

from itertools import islice

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives import registry
from ._common import ok, error, safe
from . import _assert
from . import _common
from . import _cam_common
from . import _inputs
from . import _joints
from . import _relations

app = adsk.core.Application.get()

# Budget thresholds: above these, a whole-design dump (design_get(include=['tree']) with no target, find_geometry
# over the whole design) is expensive/large, so the pointers steer to a TARGETED call instead. Tuned so
# small designs (the common case) get the full picture and only genuinely-large ones get steered.
_BIG_OCCURRENCES = 40        # above this, design_get(include=['tree']) is heavy -> suggest a target
_BIG_BODIES = 60             # above this, whole-design find_geometry is heavy -> suggest target=...
_DIGEST_LIMIT = 25           # top-level occurrences listed in the browser digest (not the full tree)

# Every pointer string below reads "<tool_name>(...) - ..." - the tool it names is the first token up
# to the opening paren. Used by _drop_unregistered_pointers to recognize which tool a pointer targets
# without a separate hand-maintained key->tool map that could drift from the strings themselves.
_POINTER_TOOL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(")


def _pointer_tool(text):
    """The tool name a pointer string names, or None if it doesn't follow the '<tool_name>(...)' shape."""
    m = _POINTER_TOOL_RE.match(text or "")
    return m.group(1) if m else None


def _drop_unregistered_pointers(pointers):
    """Drop a pointer naming a tool that ISN'T registered - a gated-off family's pointer 404s."""
    # An EMPTY registry means no live server backs this call (the unit-test context), so filtering
    # is skipped there; in production this tool is itself registered.
    if not registry.get_tools():
        return pointers
    return {k: v for k, v in pointers.items()
            if (_pointer_tool(v) is None or registry.has_tool(_pointer_tool(v)))}


def _design_mode(design):
    """'parametric' / 'direct' / 'unknown' via the shared _inputs.current_design_type - the one source
    ModeGuard and every design-mode read share (a type that is neither reports 'unknown')."""
    return _inputs.current_design_type(design)


def _data_identity(doc):
    """WHERE the active document lives in the data model: lineage URN, version, web URL, and the
    hub/project/folder holding it. An UNSAVED document has no DataFile, so every cloud field stays
    null and saved_to_cloud false."""
    ident = {
        "saved_to_cloud": False,
        "document_id": None,          # lineage URN - the id doc_open / doc_copy / data_delete_file use
        "version_number": None,
        "latest_version_number": None,
        "web_url": None,
        "hub": None,
        "project": None,
        "project_id": None,
        "folder": None,
        "folder_id": None,
    }
    df = safe(lambda: doc.dataFile)
    if not df:
        return ident       # never-saved doc: no data-model identity yet (saved_to_cloud stays false)
    ident["saved_to_cloud"] = True
    ident["document_id"] = safe(lambda: df.id)
    # MEASURED: the ONE DataFile handle an open document holds KEEPS its pre-save values while a
    # fresh findFileById already reports the new tip. This read does not pay that refetch.
    ident["version_number"] = safe(lambda: df.versionNumber)
    ident["latest_version_number"] = safe(lambda: df.latestVersionNumber)
    ident["version_lag_note"] = (
        "version_number / latest_version_number are read off the DataFile handle this open document "
        "HOLDS, and that handle can keep pre-save values after a save. Use a fresh data_get on the "
        "URN for cloud identity; doc_save version_confirmed is true only when fresh comparable "
        "before/after reads observed an advance, while false/pending is unknown.")
    ident["web_url"] = safe(lambda: df.fusionWebURL)

    folder = safe(lambda: df.parentFolder)
    if folder is not None:
        ident["folder"] = safe(lambda: folder.name)
        ident["folder_id"] = safe(lambda: folder.id)
    project = safe(lambda: df.parentProject)
    if project is not None:
        ident["project"] = safe(lambda: project.name)
        ident["project_id"] = safe(lambda: project.id)
        hub = safe(lambda: project.parentHub)
        if hub is not None:
            ident["hub"] = safe(lambda: hub.name)
    return ident


def _overall_bbox(root, design):
    """The whole-design world-aligned bounding box - size and center in the design's display units,
    or None when nothing measurable is modelled yet."""
    bb = safe(lambda: root.boundingBox)
    mn = safe(lambda: bb.minPoint) if bb is not None else None
    mx = safe(lambda: bb.maxPoint) if bb is not None else None
    if mn is None or mx is None:
        return None
    units = safe(lambda: design.unitsManager.defaultLengthUnits) or "cm"
    # Internal API length is cm; the units manager converts. A failed conversion returns None,
    # never the raw centimetre number under the requested unit's label.
    def conv(v_cm):
        if v_cm is None:
            return None
        return _common.measured(lambda: design.unitsManager.convert(v_cm, "cm", units), places=4)
    xmn, ymn, zmn = (_common.measured(lambda: mn.x), _common.measured(lambda: mn.y),
                     _common.measured(lambda: mn.z))
    xmx, ymx, zmx = (_common.measured(lambda: mx.x), _common.measured(lambda: mx.y),
                     _common.measured(lambda: mx.z))

    def span(lo, hi):
        return None if (lo is None or hi is None) else conv(hi - lo)

    def mid(lo, hi):
        return None if (lo is None or hi is None) else conv((hi + lo) / 2)

    size = {"x": span(xmn, xmx), "y": span(ymn, ymx), "z": span(zmn, zmx)}
    center = {"x": mid(xmn, xmx), "y": mid(ymn, ymx), "z": mid(zmn, zmx)}
    out = {
        "units": units,
        "size": size,
        "center": center,
        # Component.boundingBox SWEEPS sketch + construction geometry (an orphaned datum inflates
        # it); model_inspect's bbox is solids-only - the two legitimately disagree.
        "scope": "all geometry incl. sketches/construction - solids-only extents: model_inspect",
    }
    if any(v is None for v in list(size.values()) + list(center.values())):
        out["unreadable_values"] = True
        out["note"] = (f"A null size/center component could not be read or converted to '{units}' - "
                       "it is reported as null rather than a fabricated 0 or an unconverted "
                       "centimetre value.")
    return out


def _view_state():
    """What the CAMERA is currently showing - so a screenshot-driven agent knows whether it needs to
    reframe before its first view_screenshot. Returns projection (perspective/orthographic) + the eye
    and target world points (rounded), or None if no viewport."""
    vp = safe(lambda: app.activeViewport)
    cam = safe(lambda: vp.camera) if vp is not None else None
    if cam is None:
        return None
    ct = safe(lambda: cam.cameraType)
    projection = {0: "orthographic", 1: "perspective", 2: "perspective"}.get(ct, ct)

    def pt(p):
        # A component that will not read is null, not 0.0 - and a point carrying one fabricated
        # zero is a WRONG world position, so the whole point reads null instead.
        if p is None:
            return None
        xyz = {axis: _common.measured(getter, places=3) for axis, getter in
               (("x", lambda: p.x), ("y", lambda: p.y), ("z", lambda: p.z))}
        return None if any(v is None for v in xyz.values()) else xyz
    return {
        "projection": projection,
        "eye": pt(safe(lambda: cam.eye)),
        "target": pt(safe(lambda: cam.target)),
    }


def _selection_echo():
    """A COMPACT echo of what the user currently has selected in Fusion - the cheapest bridge from the
    human's intent to an actionable handle. One short record per selection (kind + name/owner); for the
    full geometry detail + direction vectors the agent calls sys_get_selection. Returns (count, list).
    Kept deliberately shallow (no areas/centroids/handles) so the cold-boot read stays cheap."""
    ui = safe(lambda: app.userInterface)
    sels = safe(lambda: ui.activeSelections) if ui is not None else None
    count = safe(lambda: sels.count, 0) if sels is not None else 0
    out = []
    for s in islice(_common.iter_collection(sels), 10):
        ent = safe(lambda s=s: s.entity)
        if ent is None:
            continue
        tname = safe(lambda: type(ent).__name__) or "Unknown"
        rec = {"type": tname}
        # name/owner depending on kind - enough to know WHAT was clicked, not full geometry
        if tname in ("BRepFace", "BRepEdge", "BRepVertex"):
            rec["kind"] = {"BRepFace": "face", "BRepEdge": "edge", "BRepVertex": "vertex"}[tname]
            rec["body"] = safe(lambda: ent.body.name)
            occ = safe(lambda: ent.assemblyContext)
            rec["occurrence"] = safe(lambda: occ.fullPathName) if occ is not None else None
        elif tname == "BRepBody":
            rec["kind"] = "body"
            rec["name"] = safe(lambda: ent.name)
        elif tname == "Occurrence":
            rec["kind"] = "occurrence"
            rec["name"] = safe(lambda: ent.fullPathName)
        else:
            rec["kind"] = "other"
            rec["name"] = safe(lambda: ent.name)
        out.append(rec)
    return (count or 0), out


def _timeline_rollup(design):
    """(errors, warnings, suppressed, markers, total) timeline COUNTS by healthState (2/1/3). A
    null/other state is a non-computing MARKER - a Snapshot has none - counted distinctly so it is
    never folded into an implied healthy; the total reconciles as errors+warnings+suppressed+
    markers+healthy."""
    errors = warnings = suppressed = markers = total = 0
    tl = safe(lambda: design.timeline)
    if tl is None:
        return errors, warnings, suppressed, markers, total   # direct-mode designs have no timeline
    for o in _common.iter_collection(tl):
        total += 1
        hs = safe(lambda o=o: o.healthState)
        if hs == 2:
            errors += 1
        elif hs == 1:
            warnings += 1
        elif hs == 3:
            suppressed += 1
        elif hs != 0:                                          # null/unknown health -> a Snapshot-like marker
            markers += 1
    return errors, warnings, suppressed, markers, total


def _joint_rollup(design):
    """(joint_count, broken_joints[names], health_unknown count) over the FULL joint walk, each
    state read through _assert.compute_state - an as-built joint answers no healthState of its own
    and is counted off the timeline item that does. An 'unknown' joint is neither broken nor
    healthy."""
    broken, unknown = [], 0
    joints = _joints.all_joints(design)
    for idx, j in enumerate(joints):
        state, _failure = _assert.compute_state(j)
        if state == "broken":
            broken.append(safe(lambda j=j: j.name) or f"#{idx}")
        elif state == "unknown":
            unknown += 1
    return len(joints), broken, unknown


def _relation_rollup(design):
    """(broken_relations[names], health_unknown count) over rigid groups, motion links and assembly
    constraints - the same _assert.compute_state read as the joints, since a RigidGroup answers no
    healthState of its own and is readable only through its timeline item."""
    broken, unknown = [], 0
    for kind in ("rigid_group", "motion_link", "constraint"):
        for rel, _owner in _relations.all_relations(design, kind):
            state, _failure = _assert.compute_state(rel)
            if state == "broken":
                broken.append(safe(lambda rel=rel: rel.name) or f"({kind})")
            elif state == "unknown":
                unknown += 1
    return broken, unknown


def _grounded_count(root):
    grounded = 0
    for o in _common.iter_collection(safe(lambda: root.occurrences)):
        if safe(lambda o=o: o.isGrounded, False):
            grounded += 1
    return grounded


def _unresolved_descendants(walk, name):
    """How many unresolved references sit anywhere under the top-level occurrence `name`, matched
    off the ONE census by each broken row's parent-path prefix."""
    if not name:
        return 0
    return sum(1 for b in walk.broken
               if b["parent_path"] == name or (b["parent_path"] or "").startswith(name + "+"))


def _browser_digest(root, walk):
    """A DEPTH-1 digest of the top-level occurrences - name, component, child and body counts,
    grounded, is_xref (which describes THAT ROW only) - plus unresolved_descendants, the one
    subtree-wide number, read off the shared census rather than a second walk."""
    digest = []
    occs = safe(lambda: root.occurrences)
    count = safe(lambda: occs.count, 0) if occs else 0
    for o in islice(_common.iter_collection(occs), _DIGEST_LIMIT):
        is_broken, detail = _common.broken_reference(o)
        name = safe(lambda o=o: o.name)
        if is_broken:
            # Every other read on such an occurrence raises; a row of swallowed defaults would show
            # it as an ordinary empty component, which is how it stayed invisible.
            digest.append({"name": name if name else "(unreadable name)",
                           "unresolved": True, "detail": detail})
            continue
        digest.append({
        "name": name,
        "component": safe(lambda o=o: o.component.name),
        "children": safe(lambda o=o: o.childOccurrences.count, 0),
        "unresolved_descendants": _unresolved_descendants(walk, name),
        "bodies": safe(lambda o=o: o.bRepBodies.count, 0),
        "grounded": bool(safe(lambda o=o: o.isGrounded, False)),
        "is_xref": bool(safe(lambda o=o: o.isReferencedComponent, False)),
        })
    return digest, (count or 0)


def _xref_health(doc):
    """(xref_count, out_of_date[names]) for ANY document's external references, off
    DocumentReference.isOutOfDate - the flag doc_update_xref acts on. Read-only: getLatestVersion is
    never called."""
    refs = safe(lambda: doc.documentReferences)
    n = safe(lambda: refs.count, 0) if refs is not None else 0
    ood = []
    for i, ref in enumerate(_common.iter_collection(refs)):
        if bool(safe(lambda ref=ref: ref.isOutOfDate, False)):
            nm = safe(lambda ref=ref: ref.dataFile.name) or safe(lambda ref=ref: ref.name) or f"#{i}"
            ood.append(nm)
    return (n or 0), ood


_EMPTY_NAME_CAP = 8


# The walk builds its parent chain top-down, so it is acyclic; the cap only stops a hand-built node
# whose chain loops from spinning the climb below.
_MAX_BREADCRUMB_HOPS = 64


def _op_breadcrumb(node):
    """The walk's 'Setup / Folder / Operation' path for ONE operation node, or '' where any level
    of it answered nothing or an EMPTY name - half an address addresses nothing."""
    # The chain is checked through the walk's own PARENT LINKS, each level's raw `name` read, never
    # by reading the joined string back - so a container literally named 'None' passes unharmed.
    hop, hops = node, 0
    while hop is not None:
        if not hop.name or hops > _MAX_BREADCRUMB_HOPS:
            return ""
        hop, hops = hop.parent, hops + 1
    return node.path


def _empty_labels(rows):
    """The discriminator each empty operation is told apart by, one for one over `rows`
    ([(name, path, position)]) - the breadcrumb, plus this read's own walk POSITION where two rows
    share even that. A row whose path did not read gets '', and told_apart keeps its plain name."""
    per_path = {}
    for _name, path, _position in rows:
        per_path[path] = per_path.get(path, 0) + 1
    out = []
    for name, path, position in rows:
        if not path:
            out.append((name, ""))
        elif per_path[path] > 1:
            out.append((name, f"{path} (operation {position})"))
        else:
            out.append((name, path))
    return out


def _cam_summary(doc):
    """(has_cam, {setups, total_operations, ungenerated_operations, errored_operations,
    suppressed_operations, empty_toolpath_operations, unread_state_operations?, empty_toolpaths?,
    operations_unread?}) WITHOUT switching to Manufacture, each op bucketed once through
    op_primary_state - including 'unread', the op whose operationState did not answer."""
    # MEASURED: a missing hasToolpath is NOT "needs generating" - a suppressed op has none by
    # design, and a generated op can finish with an EMPTY toolpath (state IsValid, isToolpathValid
    # true, hasToolpath false), which is why is_empty_toolpath is also handed the CAM product.
    cam, _ = _cam_common.get_cam()
    if not cam:
        return False, None
    setups = safe(lambda: cam.setups)
    n_setups = safe(lambda: setups.count, 0) if setups else 0
    total_ops = ungenerated = errored = suppressed = empty = unread_state = 0
    unread = 0
    unread_unknown = 0
    empty_rows = []
    position = 0
    for s in _common.iter_collection(setups):
        walked = 0
        # tree_nodes, not setup.allOperations: allOperations flattens folder-nested operations and
        # DROPS the folder objects, so an empty row's breadcrumb exists only in this walk.
        nodes = safe(lambda s=s: [n for n in _cam_common.tree_nodes(s) if n.kind == "operation"])
        for node in (nodes or []):
            facts = _cam_common.op_state_facts(node.obj, cam)
            walked += 1
            position += 1
            state = _cam_common.op_primary_state(facts)
            if state == "suppressed":
                suppressed += 1
            elif state == "error":
                errored += 1
            elif state in ("out_of_date", "no_toolpath"):
                ungenerated += 1
            elif state == "unread":
                # operationState did not answer for this op - it is in no lifecycle bucket, and
                # counting it as generated would read as a clean job.
                unread_state += 1
            elif _cam_common.is_empty_toolpath(facts):
                empty += 1
                empty_rows.append((facts["name"], _op_breadcrumb(node), position))
        total_ops += walked
        # The census check: the walk SKIPS an item(i) that raises, so a short walk reads as a small
        # setup unless it is compared against the setup's own flat count.
        declared = _common.counted(lambda s=s: s.allOperations.count)
        if declared is None:
            unread_unknown += 1
        elif walked < declared:
            unread += declared - walked
    out = {"setups": n_setups or 0, "total_operations": total_ops,
           "ungenerated_operations": ungenerated, "errored_operations": errored,
           "suppressed_operations": suppressed, "empty_toolpath_operations": empty}
    if unread_state:
        # Present only where one was found: a zero here would read as a checked-and-clean claim
        # on every job that never met the state.
        out["unread_state_operations"] = unread_state
    if empty_rows:
        # told_apart judges over EVERY empty operation and the cap is applied after, so a listed
        # name that repeats only outside the cap is still replaced by its own breadcrumb.
        out["empty_toolpaths"] = _common.told_apart(_empty_labels(empty_rows))[:_EMPTY_NAME_CAP]
    # Present only when the census is INCOMPLETE, so the counts above are never read as a full
    # tally of a job whose operations did not all answer.
    if unread:
        out["operations_unread"] = unread
    if unread_unknown:
        out["setups_with_unreadable_operation_count"] = unread_unknown
    return True, out


def _cam_pointer(cam):
    """The cam pointer's state clause - what to do next about the toolpaths, ERRORED ops first,
    then ungenerated, then generated-but-empty, then parked. An incomplete census never reads as a
    clean bill."""
    if not cam:
        return "toolpaths look generated."
    unread = (cam.get("operations_unread") or cam.get("setups_with_unreadable_operation_count")
              or cam.get("unread_state_operations"))
    incomplete = (" Some operations did not read - the counts are incomplete." if unread else "")
    if cam.get("errored_operations"):
        return (f"{cam['errored_operations']} operation(s) have ERRORS - cam_get("
                "include=['operations']) for the text; regenerating will not clear them."
                + incomplete)
    if cam.get("ungenerated_operations"):
        return f"{cam['ungenerated_operations']} operation(s) need generating." + incomplete
    parked = (f" {cam['suppressed_operations']} suppressed."
              if cam.get("suppressed_operations") else "")
    if cam.get("empty_toolpath_operations"):
        listed = [n for n in (cam.get("empty_toolpaths") or []) if n]
        names = ", ".join(listed)
        # the names are capped; say so with a clause rather than a trailing ellipsis, which ran
        # into the sentence's own full stop as '....'
        if names and len(listed) < cam["empty_toolpath_operations"]:
            names += f" (first {len(listed)} of {cam['empty_toolpath_operations']})"
        return (f"toolpaths are generated; {cam['empty_toolpath_operations']} produced an EMPTY "
                f"toolpath (nothing to cut)" + (f": {names}." if names else ".")
                + parked + incomplete)
    if unread:
        return ("the operation census is incomplete - some operations did not read." + parked)
    return "toolpaths look generated." + parked


# MEASURED: a base license reads 33 of 54 milling strategies allowed and the Machining Extension
# 50, and these four sit in the 17 the extension adds. The probe needs no document, CAM product or
# setup (64 probes measured at 0.7ms), so it rides every orient.
_CAPABILITY_SENTINELS = (
    "steep_and_shallow",      # advanced 3D steep-and-shallow finishing
    "multiaxis_finishing",    # simultaneous 4/5-axis finishing (multi-axis)
    "swarf",                  # swarf / flank multi-axis machining (multi-axis)
    "probe_geometry",         # on-machine probing
)

_CAPABILITY_NOTE = (
    "observed_generation[strategy] is that strategy's isGenerationAllowed flag, probed with no "
    "document or setup - it describes the INSTALLATION, not the open document. false = the "
    "entitlement reads absent (measured: creation succeeds, then generation silently declines); "
    "null = the probe could not read it. entitled: their one verdict. No license tier or SKU is "
    "asserted (Fusion exposes no license API).")

# Appended only where cam_get is registered: cam is a gateable family, and a tool named mid-note
# bypasses _drop_unregistered_pointers, which only sees the 'pointers' dict.
_CAPABILITY_POINTER = (
    " cam_get(include=['strategies']) lists the full per-setup map (needs a CAM setup).")


def _capability_note():
    """The note, with the cam_get pointer kept only where that tool is registered (an EMPTY
    registry is the unit-test context and keeps every pointer)."""
    if registry.get_tools() and not registry.has_tool("cam_get"):
        return _CAPABILITY_NOTE
    return _CAPABILITY_NOTE + _CAPABILITY_POINTER


def _sentinel_flags():
    """{sentinel strategy: its isGenerationAllowed}, read through the shared _cam_common seam - the
    same one cam_generate's launch pre-flight excludes on, so the two cannot disagree about one
    strategy."""
    return {name: _cam_common.strategy_generation_allowed(name) for name in _CAPABILITY_SENTINELS}


def _entitled_over(flags):
    """True / False / None over the sentinel flags: true where every one reads true, false where
    one reads false, None where any did not read - an unread flag is no entitlement, and a bare
    all() would fold it into a confident false."""
    if any(flag is None for flag in flags):
        return None
    return all(flags)


def _capability_block():
    """One observed_generation entry per sentinel (see _CAPABILITY_SENTINELS), the single entitled
    verdict over them, plus the note - the flags probed ONCE for both."""
    flags = _sentinel_flags()
    return {"observed_generation": flags,
            "entitled": _entitled_over(flags.values()),
            "note": _capability_note()}


def handler() -> dict:
    """See TOOL_DESCRIPTION."""
    out = {
    "fusion_version": safe(lambda: app.version),
    "document": None,
    "workspace": safe(lambda: app.userInterface.activeWorkspace.name),
    "product": safe(lambda: app.activeProduct.productType),
    }

    doc = safe(lambda: app.activeDocument)
    if doc is None:
        return error("No active document. Open or create one first (see doc_new / doc_open).")
    out["document"] = {
    "name": safe(lambda: doc.name),
    "saved": bool(safe(lambda: doc.isSaved, False)),
    "modified": bool(safe(lambda: doc.isModified, False)),
    # WHERE it lives in the data model: URN + version + web URL + hub/project/folder. Lets the agent
    # orient within the data model (and get the URN doc_copy/doc_open/data_* need) in this one read.
    "data_model": _data_identity(doc),
    }

    # Camera state and the user's selection are document-independent - reported on EVERY path,
    # a non-Design document included.
    out["view"] = _view_state()
    sel_count, sel = _selection_echo()
    out["selection"] = {"count": sel_count, "selected": sel}
    # Keyed machining_capabilities, not bare 'capabilities': that word already means the SERVER's
    # tool families here (sys_capability_map).
    out["machining_capabilities"] = _capability_block()

    design = _common.design()
    pointers = {}

    xref_count, out_of_date = _xref_health(doc)   # doc-level: works even without an active Design

    if design is None:
        # A document is open but it's not a Design (e.g. a drawing). Report what we can + a pointer.
        has_cam, cam = _cam_summary(doc)
        out["has_design"] = False
        out["has_cam"] = has_cam
        if cam:
            out["cam"] = cam
        out["references"] = {"referenced_documents": xref_count, "out_of_date": out_of_date}
        note = ("A document is open but no Design product is active. "
    "Switch to the Design workspace, or use the CAM tools if has_cam is true.")
        if out_of_date:
            note = (f"WARNING: {len(out_of_date)} out-of-date reference(s) - run doc_update_xref. " + note)
        out["note"] = note
        return ok(out)

    root = safe(lambda: design.rootComponent)
    mode = _design_mode(design)
    # The ONE design-wide census: root.allOccurrences.count RAISES on a design holding an
    # unresolved external reference, so the walk falls back to component.occurrences and reports
    # WHICH walk answered; total is null when neither did.
    occ_walk = _common.occurrence_walk(design)
    occ_total = occ_walk.total
    unresolved = [{"name": b["name"], "parent_path": b["parent_path"], "detail": b["detail"]}
                  for b in occ_walk.broken]
    unresolved_names = sorted({u["name"] for u in unresolved})
    # Design-wide, not root-only: a doc whose only sketches live in sub-components reads
    # sketches:0 from a root-only count.
    body_total, sketch_total = _common.design_wide_counts(design)
    param_total = safe(lambda: design.userParameters.count, 0) or 0

    errors, warnings, suppressed, markers, tl_total = _timeline_rollup(design)
    marker_pos, marker_count = _common.timeline_marker(design)
    rolled_back = bool(marker_pos is not None and marker_count and marker_pos < marker_count)
    joint_count, broken_joints, joints_unknown = _joint_rollup(design)
    # MEASURED: a failed assembly constraint left this read healthy while only a deeper
    # include=['relations'] slice named it, so relation health rides the first call.
    broken_relations, relations_unknown = _relation_rollup(design)
    grounded = _grounded_count(root)
    digest, top_level = _browser_digest(root, occ_walk)
    has_cam, cam = _cam_summary(doc)

    out["has_design"] = True
    out["design"] = {
    "root_component": safe(lambda: root.name),
    "units": safe(lambda: design.unitsManager.defaultLengthUnits),
    "mode": mode,
    "top_level_occurrences": top_level,
    # null, never 0, when neither walk enumerated: an unreadable census is UNKNOWN.
    "total_occurrences": occ_total,
    "occurrences_walk": occ_walk.method,
    "bodies": body_total,
    "sketches": sketch_total,
    "parameters": param_total,
    "overall_bbox": _overall_bbox(root, design),
    }
    out["health"] = {
    "timeline_features": tl_total,
    "timeline_errors": errors,
    "timeline_warnings": warnings,
    "timeline_suppressed": suppressed,
    # Snapshot-like markers read null health - counted distinctly so the total reconciles and they
    # are not folded into an implied 'healthy'.
    "timeline_markers": markers,
        "joint_count": joint_count,
        "broken_joints": broken_joints,
        "broken_relations": broken_relations,
        "grounded_occurrences": grounded,
        # Features after a rolled-back marker are NOT in the current model - they revert to home.
        "timeline_rolled_back": rolled_back,
        "out_of_date_references": out_of_date,
        # Disjoint from out_of_date_references: a broken reference has no DocumentReference at all,
        # so no isOutOfDate read can carry it (measured: a template holding one read is_healthy
        # true under a note declaring the document clean).
        "unresolved_references": unresolved,
        "is_healthy": (errors == 0 and not broken_joints and not broken_relations
                       and not out_of_date and not rolled_back and not unresolved),
    }
    # Present only when a compute state did NOT read, so the two lists above are never taken for a
    # complete census - is_healthy is a verdict over the entities that HAVE a state.
    if joints_unknown:
        out["health"]["joints_health_unknown"] = joints_unknown
    if relations_unknown:
        out["health"]["relations_health_unknown"] = relations_unknown
    # The noun is IN the key: referenced DOCUMENTS (Document.documentReferences), while
    # doc_get(include=['xref_tree']).reference_link_count counts reference LINKS.
    out["references"] = {"referenced_documents": xref_count, "out_of_date": out_of_date}
    out["has_cam"] = has_cam
    if cam:
        out["cam"] = cam
    out["browser_digest"] = digest

    # ── POINTERS: the targeted tool per area, steering away from whole-design dumps on a large
    # design. An UNREADABLE census cannot be called large - it steers by the counts that DID read.
    occ_known = occ_total if occ_total is not None else 0
    large = occ_known > _BIG_OCCURRENCES or body_total > _BIG_BODIES
    pointers["assembly_structure"] = (
        f"design_get(include=['tree'], component='<name from browser_digest>') - {occ_total} occurrences is large; "
        "scope to a component rather than dumping the whole tree."
        if occ_known > _BIG_OCCURRENCES else
        "design_get(include=['tree']) - small enough to walk the whole assembly in one call.")
    pointers["geometry"] = (
        f"find_geometry(target='<occurrence/body>') - {body_total} bodies; always scope by target "
        "(filter by kind/radius/nearest_to) rather than scanning the whole design."
        if body_total > _BIG_BODIES else
        "find_geometry(target='<part>', kind=...) to get stable handles for jointing/filleting.")
    if joint_count or grounded:
        pointers["kinematics"] = "assembly_get() for full per-occurrence position/ground/joint state."
    if param_total:
        pointers["parameters"] = (
            f"param_get() to read the {param_total} user parameter(s); param_set / param_add to change them.")
    if sel_count:
        pointers["selection"] = (
            f"sys_get_selection() for full detail (geometry + direction vectors + handles) on the "
            f"{sel_count} entity(ies) the user has selected - likely what they mean by 'this'.")
    if broken_joints or errors or rolled_back:
        parts = []
        if errors:
            parts.append(f"{errors} timeline error(s)")
        if broken_joints:
            parts.append(f"{len(broken_joints)} broken joint(s)")
        if rolled_back:
            parts.append(f"a rolled-back marker ({marker_pos}/{marker_count} - features after it are reverted)")
        pointers["fix_health"] = "design_recompute() then re-orient - " + ", ".join(parts) + "."
    if broken_relations:
        # design_recompute (the fix_health remedy) does not mend a relation.
        pointers["fix_relations"] = (
            f"assembly_get() - {len(broken_relations)} assembly relation(s) failed to compute "
            f"({', '.join(broken_relations[:5])}); assembly_edit_relations repairs or removes one.")
    if unresolved:
        # No repair TOOL to point at: component and documentReference both raise on such a row, and
        # it is absent from Document.documentReferences, so nothing here can fetch or relink it.
        pointers["unresolved_references"] = (
            f"design_get(include=['tree']) - {len(unresolved)} occurrence(s) "
            f"({', '.join(unresolved_names[:5])}) reference a component that could not be loaded; "
            "the tree marks each row unresolved:true. Their source file is not readable through the "
            "API - open the browser tree in Fusion and hover the flagged node for the reason.")
    if out_of_date:
        pointers["fix_references"] = (
            f"doc_update_xref() - {len(out_of_date)} external reference(s) are OUT OF DATE "
            f"({', '.join(out_of_date[:5])}). Stale references show the wrong geometry (and miss newer "
            "features); refresh before relying on, machining, or inserting this part.")
    if has_cam:
        pointers["cam"] = "cam_get() for the machining job; " + _cam_pointer(cam)
    pointers["guidance"] = "sys_get_guidance() - the index of recipes (one per call)."
    out["pointers"] = _drop_unregistered_pointers(pointers)

    # The gate matches is_healthy exactly, broken_relations included - a design whose only fault is
    # a failed assembly constraint must not read "No compute errors ..." beside is_healthy false.
    if errors or broken_joints or broken_relations or out_of_date or rolled_back or unresolved:
        bits = []
        if errors:
            bits.append(f"{errors} timeline error(s)")
        if broken_joints:
            bits.append(f"{len(broken_joints)} joint(s) failed to compute")
        if broken_relations:
            bits.append(f"{len(broken_relations)} assembly relation(s) failed to compute")
        if unresolved:
            bits.append(f"{len(unresolved)} UNRESOLVED reference(s): {', '.join(unresolved_names)}")
        if out_of_date:
            bits.append(f"{len(out_of_date)} out-of-date reference(s)")
        if rolled_back:
            bits.append(f"timeline rolled back to {marker_pos}/{marker_count} (features after the marker are reverted)")
        verdict = (f"Attention ({', '.join(bits)}) - see health + the fix_* pointer(s). These CAN be "
                   "intentional on a fixture/CAM template (parked alternates, pinned refs) or a "
                   "deliberate mid-history roll; confirm before treating as broken. ")
    else:
        verdict = "No compute errors, failed joints, or stale references. "
    # A timeline warning is stated distinctly, never folded into a clean bill and never counted as
    # unhealthy.
    if warnings:
        verdict += (f"{warnings} timeline WARNING(s) present (not errors) - "
                    "design_get(include=['timeline']) lists which. ")
    # A withheld state is stated out loud - is_healthy is silent about it.
    if joints_unknown or relations_unknown:
        unknown_bits = []
        if joints_unknown:
            unknown_bits.append(f"{joints_unknown} joint(s) (joints_health_unknown)")
        if relations_unknown:
            unknown_bits.append(
                f"{relations_unknown} assembly relation(s) (relations_health_unknown)")
        verdict += (" and ".join(unknown_bits) + " published NO compute state - neither the entity "
                    "nor its timeline item answered one - so they are counted neither broken nor "
                    "healthy and is_healthy makes no claim about them. ")
    if unresolved:
        verdict += (
            "An UNRESOLVED reference means reading that occurrence's component RAISES, so the source "
            "component is not loaded. Its source file, project and hub are NOT readable through the "
            "API (component and documentReference both raise, and the reference is absent from the "
            "document's documentReferences) - open the browser tree in Fusion and hover the flagged "
            "node for the reason. Such an occurrence also has no readable placement, bodies or "
            "children, so it is excluded from geometry searches and interference checks. ")
    if occ_total is None:
        verdict += ("total_occurrences is null: NEITHER occurrence walk enumerated, so the design's "
                    "occurrence count is unknown rather than zero. ")
    elif occ_walk.method == _common.WALK_RECURSED:
        verdict += ("occurrences_walk='recursed': root.allOccurrences raised, so total_occurrences "
                    "was rebuilt from component.occurrences. ")
    out["note"] = (
        verdict +
        "browser_digest is DEPTH-1: is_xref describes each row ITSELF, so a reference nested below "
        "one leaves every flag false - doc_get(include=['xref_tree']) walks every depth. "
        "references.referenced_documents counts DOCUMENTS; xref_tree's reference_link_count counts "
        "LINKS. Use 'pointers' to drill down with scoped calls."
        + (" Design is LARGE - prefer scoped calls." if large else "")
        + ("" if out["document"]["data_model"]["saved_to_cloud"] else
           " Document is UNSAVED - no URN/project yet; save before addressing it by id."))
    return ok(out)


TOOL_DESCRIPTION = (
    "Getting started on an open document - the first read: what it is, where it lives, its contents "
    "and health, CAM state, and pointers to the tool that drills each area."
)

tool = Tool.create_simple(name="workspace_orient", description=TOOL_DESCRIPTION).strict_schema()
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    registry.register(item)
