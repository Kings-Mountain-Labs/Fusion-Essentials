# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""CAM READ cores: the slice handlers behind cam_get(include=[...])."""

import re

import adsk.core
import adsk.cam
import adsk.fusion

from ._common import (CM_TO_UNIT, counted, measured, ok, error, iter_collection, safe, told_apart)
from ._cam_common import (_SETUP_BLOCKER_REMEDY, _segment, _setup_node, _walk_children,
                          blocked_setup_records, clamp_rows, counts_as_warning, first_line, get_cam,
                          is_empty_toolpath, machine_label, machine_limits, machine_spindle_max,
                          op_primary_state, op_state_facts, operations_under, ready_verdict,
                          resolve_cam_node, setup_blockers, setups, spindle_check,
                          stock_mode_name, toolpath_present_tally, validity_basis)

MAP_BLURB = (
    "the per-slice READ cores behind cam_get(include=[...]) - get_cam_setups_handler, "
    "get_cam_operations_handler, get_setup_references_handler, get_tool_list_handler, "
    "get_machining_time_handler, get_nc_programs_handler, get_inspection_results_handler, "
    "get_machine_limits_handler; reach for one only from that router, every other CAM tool "
    "shares _cam_common")

_MAX_ITEMS = 1000

# safe() cannot tell "read None" from "the read raised", and those are different answers wherever a
# CAM property RAISES instead of reading empty.
_MISSING = object()

# Fusion logs why an op went out of date in op.messageLog, not in op.warning/op.error (both read
# empty for a plain invalidation): "Invalidated: <category>" lines plus per-parameter delta lines.
_INVAL_CATEGORICAL = ("Design changed", "Dependency changed", "Holder changed", "Tool", "Stock", "Suppress")
_INVAL_REASON_CAP = 12
_INVAL_RE = re.compile(r"Invalidated:\s*(.+?)\s*$")
_INVAL_PARAM_RE = re.compile(r"different value for parameter '")
# A changed machine definition is logged as "External changed: machine.<field>", not as an
# "Invalidated:" line.
_INVAL_MACHINE_RE = re.compile(r"External changed:\s*machine\.")

def _invalidation_reasons(op):
    """(categorical_reasons, parameter_change_count, machine_changed) parsed from op.messageLog."""
    ml = safe(lambda: op.messageLog) or ""
    reasons = []
    param_changes = 0
    machine_changed = False
    for line in ml.replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if _INVAL_MACHINE_RE.search(line):
            machine_changed = True
            continue
        if _INVAL_PARAM_RE.search(line):
            param_changes += 1
            continue
        m = _INVAL_RE.search(line)
        if not m:
            continue
        reason = m.group(1).strip()
        if any(reason.startswith(c) for c in _INVAL_CATEGORICAL) and reason not in reasons:
            reasons.append(reason)
    return reasons[:_INVAL_REASON_CAP], param_changes, machine_changed

def _operation_type_name(op_type) -> str:
    """Map an OperationTypes enum value to a readable name, defensively."""
    mapping = {
        getattr(adsk.cam.OperationTypes, n, object()): n
        for n in ("MillingOperation", "TurningOperation", "JetOperation",
        "AdditiveOperation")
    }
    return mapping.get(op_type, str(op_type))


def _model_names(getter) -> tuple:
    """(names, truncated) - names in one of a setup's model/fixture/stock collections, capped at
    _MAX_ITEMS. Takes the GETTER because the property itself raises on some setups: names is None
    where it raised, and truncated marks an INCOMPLETE list (raise, cap hit, or a dying walk)."""
    collection = safe(getter, _MISSING)
    if collection is _MISSING:
        return None, True
    names = []
    truncated = False
    try:
        for i, m in enumerate(collection):
            if i >= _MAX_ITEMS:
                truncated = True
                break
            names.append(safe(lambda: m.name, "(unnamed)"))
    except Exception:
        truncated = True
    return names, truncated

# setup.parameters.itemByName reads the WCS back: a mode parameter's .value.value is the mode
# string, a geometry binding's .value.value is an ITERABLE of the bound entities.
_WCS_MODE_PARAMS = (("origin_mode", "wcs_origin_mode"),
                    ("orientation_mode", "wcs_orientation_mode"))
_WCS_ENTITY_PARAMS = (("origin_entities", "wcs_origin_point"),
                      ("orientation_z_entities", "wcs_orientation_axisZ"))


def _wcs_bound_entities(param) -> list:
    """[{type, name?}] for ONE CadObjectParameterValue's bound entities - type is the leaf of
    objectType, and name is present only where the entity reads one back."""
    rows = []
    for ent in safe(lambda: list(param.value.value), []) or []:
        kind = safe(lambda ent=ent: ent.objectType) or ""
        row = {"type": str(kind).split("::")[-1] or None}
        name = safe(lambda ent=ent: ent.name)
        if name:
            row["name"] = name
        rows.append(row)
    return rows


def _wcs_z_world(setup):
    """The setup's own +Z as a world unit vector, off Setup.workCoordinateSystem - which way the
    tool comes at the part. A turning setup and a top milling job on one part read opposite Zs."""
    matrix = safe(lambda: setup.workCoordinateSystem)
    if matrix is None:
        return None
    axes = safe(lambda: matrix.getAsCoordinateSystem())
    if not axes or len(axes) != 4:
        return None
    z = axes[3]
    return safe(lambda: [round(z.x, 6), round(z.y, 6), round(z.z, 6)])


def setup_wcs(setup):
    """ONE setup's bound WCS {origin_mode, orientation_mode, z_world, origin_entities,
    orientation_z_entities}, terse; None where the setup exposes no readable parameters."""
    wcs = {}
    z_world = _wcs_z_world(setup)
    if z_world is not None:
        wcs["z_world"] = z_world
    params = safe(lambda: setup.parameters)
    if params is None:
        return wcs or None
    for key, pname in _WCS_MODE_PARAMS:
        mode = safe(lambda pname=pname: params.itemByName(pname).value.value)
        if mode is not None:
            wcs[key] = mode
    for key, pname in _WCS_ENTITY_PARAMS:
        p = safe(lambda pname=pname: params.itemByName(pname))
        rows = _wcs_bound_entities(p) if p is not None else []
        if rows:
            wcs[key] = rows
    return wcs or None


def get_cam_setups_handler() -> dict:
    cam, err = get_cam()
    if err:
        return error(err)

    setups = []
    setups_truncated = False
    try:
        setups_total = safe(lambda: cam.setups.count, 0) or 0
        for i in range(setups_total):
            if i >= _MAX_ITEMS:
                setups_truncated = True
                break
            s = cam.setups.item(i)
            models, models_trunc = _model_names(lambda: s.models)
            fixtures, fixtures_trunc = _model_names(lambda: s.fixtures)
            stock, stock_trunc = _model_names(lambda: s.stockSolids)
            setups.append({
        "name": safe(lambda: s.name),
        "operation_type": _operation_type_name(safe(lambda: s.operationType)),
        "is_active": safe(lambda: s.isActive),
        "machine": machine_label(safe(lambda: s.machine)),
            "stock_mode": stock_mode_name(safe(lambda: s.stockMode)),
            "wcs": setup_wcs(s),
            # null (not []) for a list whose collection property RAISED - see _model_names.
            "selected_models": models,
            "fixtures": fixtures,
            "stock_solids": stock,
            "model_lists_truncated": bool(models_trunc or fixtures_trunc or stock_trunc),
            # allOperations sees the ops nested in folders, so a folder-organized setup does not
            # read as empty.
            "operation_count": safe(lambda: s.allOperations.count, 0),
            "folder_count": safe(lambda: s.folders.count, 0),
            })
            _attach_setup_invalidation(setups[-1], s)
            # Names the list that is null because its property raised, so the null does not read as
            # "none selected".
            unreadable = [key for key, names in (("selected_models", models), ("fixtures", fixtures),
                                                 ("stock_solids", stock)) if names is None]
            if unreadable:
                setups[-1]["model_lists_unreadable"] = unreadable
            setups[-1]["blocked_by"] = setup_blockers(s)
            if setups[-1]["stock_mode"] == _PREVIOUS_SETUP_MODE:
                setups[-1]["stock_extents_describe"] = _REST_STOCK_UNREADABLE
    except Exception as e:
        return error(f"Could not read setups: {e}")

    out = {"setup_count": len(setups), "setups": setups, "truncated": setups_truncated}
    if any(r.get("stock_mode") == _PREVIOUS_SETUP_MODE for r in setups):
        out["note"] = _REST_STOCK_NOTE
    return ok(out)


# MEASURED on a setup switched to this mode: stockSolids reads 0, no previousSetup parameter exists,
# and stockXLow..stockZHigh keep the RELATIVE-BOX numbers a plain setup reads - so nothing published
# here describes what the preceding setup actually left.
_PREVIOUS_SETUP_MODE = "previous_setup"

_REST_STOCK_UNREADABLE = "the relative box, NOT the rest stock this mode cuts from"

_REST_STOCK_NOTE = (
    "A setup at stock_mode 'previous_setup' cuts what the SETUP BEFORE it left, and nothing readable "
    "describes that: its stockSolids read empty and its stock extents keep the relative-box numbers "
    "(stock_extents_describe says so on the row). Size a clearing strategy from the preceding "
    "setup's own operations, not from those extents.")

_GENERATE_REQUIRES = {"tool": "cam_generate", "workspace": "Manufacture"}
# A state that never answered is not stale work: the remedy is another READ, in the workspace op
# validity is trustworthy in, not a generation run.
_UNREAD_REQUIRES = {"tool": "cam_get", "workspace": "Manufacture"}


def _op_blocked_by(summary):
    """(blocked_by, requires) for one op - reason codes (present-and-empty when nothing blocks),
    read off the row's own state bucket, and the tool/workspace that unblocks them."""
    if summary.get("state") == "suppressed":
        return [], None                    # suppressed = excluded from posting; blocks nothing
    blocked = []
    requires = None
    if summary.get("state") == "unread":
        # No lifecycle state read, so 'ready to post' cannot be claimed for this row either.
        blocked.append("state_unread")
        requires = dict(_UNREAD_REQUIRES)
    if summary.get("tool") is None:
        blocked.append("tool_unselected")  # real refusal: "Toolpath requires tool to be selected"
    if summary.get("is_out_of_date"):
        blocked.append("toolpath_out_of_date")
        requires = dict(_GENERATE_REQUIRES)
    return blocked, requires


def _attach_setup_invalidation(rec, setup):
    """Add op_states (the terse per-state tally), invalidation_reasons and machine_out_of_date to
    one setup record, in ONE walk of its operations."""
    tally = {}
    warnings = 0
    reasons = []
    machine_changed = False
    try:
        for o in setup.allOperations:
            op = adsk.cam.Operation.cast(o)
            if op is None:
                continue
            facts = op_state_facts(op)
            st = op_primary_state(facts)
            tally[st] = tally.get(st, 0) + 1
            if facts["has_warning"]:
                warnings += 1
            if st == "out_of_date":
                op_reasons, _, op_machine = _invalidation_reasons(op)
                if op_machine:
                    machine_changed = True
                for r in op_reasons:
                    if r not in reasons:
                        reasons.append(r)
    except Exception:
        pass
    if warnings:
        tally["warning"] = warnings        # overlay: ops with a warning (may also be in another bucket)
    if tally:
        rec["op_states"] = tally           # terse: only non-zero buckets present (incl. out_of_date)
    # invalidation_reasons = WHY the out-of-date ops are stale (the count is op_states['out_of_date']).
    if tally.get("out_of_date", 0) and reasons:
        rec["invalidation_reasons"] = reasons[:_INVAL_REASON_CAP]
    if machine_changed:
        rec["machine_out_of_date"] = True

# The marker a suppressed row carries in place of the spindle comparison, which was not made.
_SUPPRESSED_NOT_COMPARED = "suppressed_not_compared"

_OPERATIONS_NOTE = (
    "'empty_toolpath' marks a row that cuts nothing; state 'unread' means operationState did not "
    "answer - re-read in Manufacture, not cam_generate. "
    "'spindle_over_machine_max' compares tool_spindleSpeed against machine_spindle_max_rpm on "
    "ACTIVE rows (a suppressed one reads spindle_check 'suppressed_not_compared'); null means a "
    "side did not read; summary.spindle_over_machine_max_count counts those over.")


def get_cam_operations_handler(setup: str = "") -> dict:
    """Operations across all setups, or just the named setup (`setup`)."""
    cam, err = get_cam()
    if err:
        return error(err)

    want = (setup or "").strip()
    if want:
        node, rerr = resolve_cam_node(cam, want, kinds=("setup",), label="setup")
        if rerr:
            return error(rerr)
        target_setups = [node.obj]
    else:
        target_setups = setups(cam)

    result_setups = []
    try:
        for s in target_setups:
            machine_max = machine_spindle_max(safe(lambda s=s: s.machine))
            ops, ops_truncated = _operations_in(s, machine_max, cam)
            # The setup's own blockers ride into the readiness verdict below.
            blocked = blocked_setup_records([s])
            rec = {
            "setup": safe(lambda s=s: s.name),
            "summary": _operations_summary(ops, blocked),   # exception-first rollup BEFORE the list
            "operations": ops,
            "operations_truncated": ops_truncated,
            }
            if machine_max is not None:
                rec["machine_spindle_max_rpm"] = machine_max
            result_setups.append(rec)
    except Exception as e:
        return error(f"Could not read operations: {e}")

    # Also summarize the distinct tools used across the returned operations.
    tools_used = {}
    for rs in result_setups:
        for op in rs["operations"]:
            t = op.get("tool")
            if t:
                tools_used[t] = tools_used.get(t, 0) + 1

    return ok({
    "setup_count": len(result_setups),
    "setups": result_setups,
    "tools_used": [{"tool": k, "operation_count": v} for k, v in tools_used.items()],
    "note": _OPERATIONS_NOTE,
    })


# The remedy each OPERATION-level exception code earns, beside the setup-level vocabulary's own map
# in _cam_common. A code with no entry here contributes no remedy.
_EXCEPTION_REMEDY = {
    "toolpath_out_of_date": "run cam_generate",
    "state_unread": "re-read with cam_get in the Manufacture workspace",
    "tool_unselected": "cam_edit_operation assigns a tool",
    "operation_error": "cam_get(include=['operations']) has the error text",
}


def _exception_remedies(exceptions, setup_blocked) -> str:
    """The readiness parenthetical, worded from the codes PRESENT and deduped in first-seen order
    (the shape _cam_common._blocked_remedies words the setup clause from) - so a scope whose only
    exception is an unread state is not sent to cam_generate."""
    codes = [c for row in (exceptions or []) for c in (row.get("blocked_by") or [])]
    codes += [c for row in (setup_blocked or []) for c in (row.get("blocked_by") or [])]
    table = dict(_EXCEPTION_REMEDY, **_SETUP_BLOCKER_REMEDY)
    remedies = list(dict.fromkeys(table[c] for c in codes if c in table))
    return f" ({'; '.join(remedies)})" if remedies else ""


def _operations_summary(op_records, setup_blocked=None) -> dict:
    """Exception-first rollup of an operations list: the per-state tally, the active census, the
    ACTIVE ops that block, and a readiness verdict gated by validity_basis and by the setup's own
    blocked_by (`setup_blocked`, from blocked_setup_records)."""
    states = {}
    exceptions = []
    active_total = 0
    valid_active = 0
    warned = 0
    over_spindle = 0
    empty_toolpaths = 0
    warning_sample = None
    for r in op_records:
        st = r.get("state")
        states[st] = states.get(st, 0) + 1
        # Keyed on the same bucket states counts, so the tally and the active census partition ONE
        # row set whichever half answered the suppression.
        if st == "suppressed":
            continue                              # suppressed = excluded from posting; not active, not blocking
        active_total += 1
        # `is True` only: a null is a comparison that could not be made.
        if r.get("spindle_over_machine_max") is True:
            over_spindle += 1
        if r.get("empty_toolpath") is True:
            empty_toolpaths += 1
        if counts_as_warning(r):
            warned += 1
            if warning_sample is None:
                warning_sample = {"name": r.get("name"), "warning": first_line(r.get("warning"))}
        has_err = bool(r.get("has_error"))
        # A toolpath reads valid while its op carries an error, so good-to-post needs BOTH flags.
        if r.get("toolpath_valid") and not has_err:
            valid_active += 1
        blocked = list(r.get("blocked_by") or [])
        if has_err and "operation_error" not in blocked:
            blocked.append("operation_error")    # an errored op blocks the post even if nothing else flags it
        if blocked:
            exceptions.append({"name": r.get("name"), "blocked_by": blocked})

    basis = validity_basis()
    summary = {"states": states, "active_count": active_total, "exceptions": exceptions,
               "validity_basis": basis}
    if over_spindle:
        summary["spindle_over_machine_max_count"] = over_spindle   # active rows only; absent = none
    if empty_toolpaths:
        summary["empty_toolpath_count"] = empty_toolpaths          # active rows only; absent = none
    if basis == "manufacture_verified":
        if active_total and valid_active == active_total and not exceptions:
            summary["readiness"] = ready_verdict(
                f"{active_total} of {active_total} active ops have valid toolpaths",
                warned, warning_sample, setup_blocked)
        else:
            summary["readiness"] = (f"{valid_active} of {active_total} active ops have valid "
                                    "toolpaths - resolve the exceptions"
                                    + _exception_remedies(exceptions, setup_blocked)
                                    + " before posting.")
    else:
        summary["readiness"] = ("op validity is only trustworthy after entering the Manufacture "
                                "workspace - enter it (and run cam_generate) to assess post-readiness.")
    return summary



def _operations_in(setup_obj, machine_max=None, cam=None) -> tuple:
    """(ops, truncated) - the operations under a setup with their folder breadcrumb, capped at
    _MAX_ITEMS; truncated means INCOMPLETE (cap hit or the walk raised). Walks _walk_children,
    which keeps the folder objects setup.allOperations drops."""
    ops = []
    truncated = False
    root = _setup_node(setup_obj)
    nodes = [root]
    try:
        # _walk_children appends AS it walks, so a walk that dies leaves the nodes it reached.
        _walk_children(setup_obj, root.name, root.path, nodes, root)
    except Exception:
        truncated = True
    # A CAM collection that stops answering item(i) yields its survivors silently, so the walk is
    # counted against the setup's own flat total.
    expected = counted(lambda: setup_obj.allOperations.count)
    walked = sum(1 for n in nodes if n.kind == "operation")
    if expected is not None and walked < expected:
        truncated = True
    try:
        for i, node in enumerate(n for n in nodes if n.kind == "operation"):
            if i >= _MAX_ITEMS:
                truncated = True
                break
            operation = adsk.cam.Operation.cast(node.obj)
            if not operation:
                continue
            ops.append(_operation_summary(operation, machine_max, node, cam))
    except Exception:
        # A row read that dies left an INCOMPLETE list - flagged, never passed off as the full read.
        truncated = True
    return ops, truncated


def _folder_of(node):
    """The folder/pattern an operation sits IN, off the walk's parent node; None directly under the
    setup."""
    if node is None or node.parent is None:
        return None
    return node.parent.name if node.parent.kind in ("folder", "pattern") else None


def _operation_summary(op, machine_max=None, node=None, cam=None) -> dict:
    tool_desc = None
    try:
        t = op.tool
        if t:
            tool_desc = t.description
    except Exception:
        tool_desc = None

    facts = op_state_facts(op, cam)
    state = facts["operation_state"]
    has_warn = facts["has_warning"]
    has_err = facts["has_error"]
    summary = {
        "name": safe(lambda: op.name),
        "tool": tool_desc,
        "strategy": safe(lambda: op.strategy),
        # operationState reads 0 ('valid') on an op carrying hasError, so the bucket is derived
        # from the facts, never from that enum alone.
        "state": op_primary_state(facts),
        "has_toolpath": safe(lambda: op.hasToolpath),
        "toolpath_valid": safe(lambda: op.isToolpathValid),
        "is_generating": safe(lambda: op.isGenerating),
        "is_suppressed": safe(lambda: op.isSuppressed),
    "is_optional": safe(lambda: op.isOptional),
    "has_warning": has_warn,
    "has_error": has_err,
    }
    # An op that generated EMPTY still reads state 'valid' beside has_toolpath true.
    if is_empty_toolpath(facts):
        summary["empty_toolpath"] = True
    if has_warn:
        summary["warning"] = (safe(lambda: op.warning) or "").strip()
    # States 1 and 3 are the toolpath-invalid ones cam_generate(skip_valid=true) redoes.
    summary["is_out_of_date"] = bool(
        state in (1, 3) and not summary["is_suppressed"]
    )
    if has_err:
        summary["error"] = safe(lambda: op.error)
    if summary["is_out_of_date"]:
        reasons, param_changes, machine_changed = _invalidation_reasons(op)
        if reasons:
            summary["invalidation_reasons"] = reasons
        if param_changes:
            summary["invalidation_param_changes"] = param_changes
        if machine_changed:
            summary["machine_changed"] = True
    blocked, requires = _op_blocked_by(summary)
    summary["blocked_by"] = blocked
    if requires:
        summary["requires"] = requires
    if node is not None:
        summary["path"] = node.path
        folder = _folder_of(node)
        if folder:
            summary["folder"] = folder
    # Two ops sharing one tool can run different presets, which the tool description cannot show.
    preset = safe(lambda: op.toolPreset)
    summary["preset"] = safe(lambda preset=preset: preset.name) if preset is not None else None
    # A suppressed op does not post, so its spindle comparison is withheld rather than answered.
    if summary["state"] == "suppressed":
        summary["spindle_check"] = _SUPPRESSED_NOT_COMPARED
        return summary
    over, requested, marker = spindle_check(op, machine_max)
    summary["spindle_over_machine_max"] = over
    if over is not False:
        if requested is not None:
            summary["spindle_rpm"] = requested
        if machine_max is not None:
            summary["machine_max_rpm"] = machine_max
    if marker:
        summary["spindle_check"] = marker
    return summary

def get_setup_references_handler(setup: str = "") -> dict:
    """Each setup's externally-referenced components resolved to their source document id, name,
    version and web URL."""
    cam, err = get_cam()
    if err:
        return error(err)

    want = (setup or "").strip()
    if want:
        node, rerr = resolve_cam_node(cam, want, kinds=("setup",), label="setup")
        if rerr:
            return error(rerr)
        target_setups = [node.obj]
    else:
        target_setups = setups(cam)

    out_setups = []
    try:
        for s in target_setups:
            refs = []
            seen_ids = set()
            refs_truncated = False
            for role, getter in (("model", lambda: s.models),
                                 ("fixture", lambda: s.fixtures),
                                 ("stock", lambda: s.stockSolids)):
                found, role_truncated = _references_in(getter, role)
                refs_truncated = refs_truncated or role_truncated
                for ref in found:
                    key = ref.get("source_id")
                    # De-dupe identical references that appear in multiple roles.
                    if key and key in seen_ids:
                        continue
                    if key:
                        seen_ids.add(key)
                    refs.append(ref)

            out_setups.append({"setup": safe(lambda s=s: s.name), "reference_count": len(refs),
        "references": refs, "references_truncated": refs_truncated})
    except Exception as e:
        return error(f"Could not read setup references: {e}")

    return ok({"setup_count": len(out_setups), "setups": out_setups})


def _references_in(getter, role: str) -> tuple:
    """(found, truncated) - external-reference rows for one of a setup's model/fixture/stock
    collections, capped at _MAX_ITEMS; truncated means INCOMPLETE, as in _model_names."""
    found = []
    truncated = False
    collection = safe(getter, _MISSING)
    if collection is _MISSING:
        return found, True
    try:
        for i, item in enumerate(collection):
            if i >= _MAX_ITEMS:
                truncated = True
                break
            occ = adsk.fusion.Occurrence.cast(item)
            if not occ:
                # Not an occurrence (could be a BRepBody/MeshBody) -> no external ref.
                continue
            if not safe(lambda: occ.isReferencedComponent, False):
                continue
            info = {"role": role, "occurrence_name": safe(lambda: occ.name),
    "source_id": None, "source_name": None, "version": None,
    "fusion_web_url": None, "is_out_of_date": None}
            try:
                docref = occ.documentReference
                if docref:
                    df = safe(lambda: docref.dataFile)
                    info["version"] = safe(lambda: docref.version)
                    info["is_out_of_date"] = safe(lambda: docref.isOutOfDate)
                    if df:
                        info["source_id"] = safe(lambda: df.id)
                        info["source_name"] = safe(lambda: df.name)
                        info["fusion_web_url"] = safe(lambda: df.fusionWebURL)
            except Exception:
                pass
            found.append(info)
    except Exception:
        truncated = True
    return found, truncated

def get_tool_list_handler() -> dict:
    """Distinct cutting tools used across the document, with the ops that use each."""
    cam, err = get_cam()
    if err:
        return error(err)

    tools = {}  # description -> {"operations": [...], "setups": set()}
    try:
        for i in range(cam.setups.count):
            s = cam.setups.item(i)
            s_name = safe(lambda: s.name)
            for op in safe(lambda: s.allOperations, []):
                operation = adsk.cam.Operation.cast(op)
                if not operation:
                    continue
                desc = None
                try:
                    t = operation.tool
                    if t:
                        desc = t.description
                except Exception:
                    desc = None
                if not desc:
                    continue
                entry = tools.setdefault(desc, {"operations": [], "setups": set()})
                # One op NAME can exist in two setups, so each row is qualified "setup / op", both
                # halves through _segment so a name that did not read is marked, not joined as None.
                op_name = safe(lambda: operation.name)
                entry["operations"].append(f"{_segment(s_name)} / {_segment(op_name)}")
                if s_name:
                    entry["setups"].add(s_name)
    except Exception as e:
        return error(f"Could not read tools: {e}")

    tool_list = [{
    "tool": desc,
    "operation_count": len(info["operations"]),
    "operations": info["operations"],
    "setups": sorted(info["setups"]),
    } for desc, info in tools.items()]
    # Most-used first.
    tool_list.sort(key=lambda t: t["operation_count"], reverse=True)

    return ok({"distinct_tool_count": len(tool_list), "tools": tool_list})

def _timeable_ops(setup_obj) -> tuple:
    """(ops, suppressed_count) - the setup's operations with the SUPPRESSED ones held back."""
    ops, suppressed = [], 0
    for raw in operations_under(setup_obj):
        op = adsk.cam.Operation.cast(raw)
        if op is None:
            continue
        if safe(lambda op=op: op.isSuppressed, False):
            suppressed += 1
            continue
        ops.append(op)
    return ops, suppressed


def _any_valid_toolpath(ops) -> bool:
    """True if any op in the list has a valid generated toolpath - what getMachiningTime needs."""
    return any(safe(lambda o=o: o.isToolpathValid, False) for o in ops)


def _op_collection(ops):
    """(collection, added) - an ObjectCollection carrying `ops` for getMachiningTime, with how many
    the collection took; (None, 0) when it could not be created."""
    coll = safe(lambda: adsk.core.ObjectCollection.create())
    if coll is None:
        return None, 0
    added = 0
    for op in ops:
        if safe(lambda op=op: coll.add(op), False):
            added += 1
    return coll, added


_TIME_OP_CAP = 200    # one getMachiningTime call per op; bound the per-turn cost on a large job

_TIME_NOTE = (
    "Estimate at 100% feed, 10.58 cm/s rapid, 1.5 s tool changes. Suppressed operations are left "
    "out and counted in excluded_suppressed. Per-operation figures do not sum to their setup "
    "total: machining_time_seconds is ONE call over the whole collection, and "
    "operations_time_sum_seconds sums the per-operation calls over operations_time_summed rows. "
    "A row marked empty_toolpath carries no time figure.")


def _op_time_rows(cam, ops, args, factor) -> tuple:
    """(rows, truncated) - one getMachiningTime call per operation carrying a valid toolpath,
    distances scaled out of CM; an EMPTY-toolpath op is named rather than timed."""
    rows = []
    for op in ops:
        facts = op_state_facts(op)
        if not (is_empty_toolpath(facts) or safe(lambda op=op: op.isToolpathValid, False)):
            continue
        if len(rows) >= _TIME_OP_CAP:      # bounds EVERY row, timed or named
            return rows, True
        if is_empty_toolpath(facts):
            rows.append({"operation": facts["name"], "empty_toolpath": True})
            continue
        try:
            mt = cam.getMachiningTime(op, *args)
        except Exception as e:
            rows.append({"operation": safe(lambda op=op: op.name), "error": str(e)})
            continue
        timed = dict(facts, machining_time=measured(lambda: mt.machiningTime))
        if is_empty_toolpath(timed):
            rows.append({"operation": facts["name"], "empty_toolpath": True})
            continue
        rows.append({"operation": safe(lambda op=op: op.name),
                     "machining_time_seconds": measured(lambda: mt.machiningTime, 1.0, 1),
                     "feed_distance": measured(lambda: mt.feedDistance, factor, 1),
                     "rapid_distance": measured(lambda: mt.rapidDistance, factor, 1)})
    return rows, False


# Said only where a setup row actually carries the key - a job whose every setup totalled would
# otherwise be handed a sentence about a state nothing in the payload is in.
_TOTAL_UNAVAILABLE_NOTE = (
    " A setup carrying setup_total_unavailable is one whose whole-collection getMachiningTime "
    "RAISED - that key is the platform's message, its per-operation rows were read one at a time, "
    "and operations_with_errors names its operations reading hasError true. It adds nothing to "
    "total_machining_time_seconds, which is a PARTIAL: total_excludes_setups names every setup "
    "left out of it.")


def _op_time_block(cam, ops, args, factor) -> dict:
    """The per-operation half of a setup row: the rows, their sum and how many were summed. This
    sum is NOT the setup total - that is one call over the whole collection."""
    rows, truncated = _op_time_rows(cam, ops, args, factor)
    timed = [r["machining_time_seconds"] for r in rows
             if isinstance(r.get("machining_time_seconds"), (int, float))]
    block = {"operations": rows, "operations_time_sum_seconds": round(sum(timed), 1),
             "operations_time_summed": len(timed)}
    if truncated:
        block["operations_truncated"] = True
    return block


def _errored_op_names(ops) -> list:
    """The operations of a setup reading hasError true - present-and-empty when none do."""
    return [safe(lambda o=o: o.name) for o in ops if safe(lambda o=o: o.hasError, False)]


def _per_operation_only(cam, label, ops, suppressed, args, factor, exc) -> dict:
    """The setup row for a whole-collection getMachiningTime that RAISED: every per-operation
    reading it could still take, the platform's message as setup_total_unavailable, and the
    operations reading hasError true beside them."""
    rec = {"setup": label, "excluded_suppressed": suppressed,
           "setup_total_unavailable": str(exc),
           "operations_with_errors": _errored_op_names(ops)}
    rec.update(_op_time_block(cam, ops, args, factor))
    return rec


def get_machining_time_handler(setup: str = "", units: str = "mm") -> dict:
    """Estimated machining time for the whole doc, or one setup (`setup`), per setup and per op."""
    cam, err = get_cam()
    if err:
        return error(err)
    unit = (units or "mm").strip().lower()
    factor = CM_TO_UNIT.get(unit)
    if factor is None:
        return error(f"Unknown units '{units}'. Valid: mm, cm, in.")

    # getMachiningTime takes feedScale/rapidFeed/toolChangeTime in percent, cm/s and s, and moving
    # them changes no figure it returns.
    feed_scale = 100.0          # 100% of programmed feed
    rapid_feed = 10.58          # cm/s
    tool_change = 1.5           # seconds
    args = (feed_scale, rapid_feed, tool_change)

    targets = []  # (label, object)
    if (setup or "").strip():
        node, rerr = resolve_cam_node(cam, setup, kinds=("setup",), label="setup")
        if rerr:
            return error(rerr)
        targets.append((node.name, node.obj))
    else:
        for s in setups(cam):
            targets.append((safe(lambda s=s: s.name), s))

    results = []
    grand = 0.0
    for label, obj in targets:
        ops, suppressed = _timeable_ops(obj)
        # getMachiningTime needs at least one VALID toolpath in the target. The failure raises
        # catchably here, but through sys_execute_script it takes the whole invocation down, so
        # the precondition is checked before the call.
        if not _any_valid_toolpath(ops):
            results.append({"setup": label, "excluded_suppressed": suppressed,
                "error": "No generated toolpath to time - every unsuppressed operation is "
                         "out-of-date or ungenerated. Run cam_generate (in the Manufacture "
                         "workspace), then retry."})
            continue
        collection, added = _op_collection(ops)
        if collection is None or added < len(ops):
            results.append({"setup": label, "excluded_suppressed": suppressed,
                "error": f"Could not build the operation collection to time: {added} of "
                         f"{len(ops)} unsuppressed operations went in."})
            continue
        try:
            mt = cam.getMachiningTime(collection, *args)
            secs = safe(lambda: mt.machiningTime, 0.0) or 0.0
            grand += secs
            rec = {
        "setup": label,
            "machining_time_seconds": round(secs, 1),
            "machining_time_hms": _hms(secs),
            "feed_time_seconds": round(safe(lambda: mt.totalFeedTime, 0.0) or 0.0, 1),
            "rapid_time_seconds": round(safe(lambda: mt.totalRapidTime, 0.0) or 0.0, 1),
            "tool_changes": safe(lambda: mt.toolChangeCount, 0),
            "feed_distance": measured(lambda: mt.feedDistance, factor, 1),
            "rapid_distance": measured(lambda: mt.rapidDistance, factor, 1),
            "timed_operations": added,
            "excluded_suppressed": suppressed,
            }
            # The per-op sum disagrees with the aggregate above, so both are published and neither
            # is derived from the other.
            rec.update(_op_time_block(cam, ops, args, factor))
            results.append(rec)
        except Exception as e:
            # The whole-collection call raises where any operation in the setup is ERRORED, which
            # would hide every good per-operation reading behind one message.
            results.append(_per_operation_only(cam, label, ops, suppressed, args, factor, e))

    # A setup whose own total raised contributes nothing to the grand total while its per-operation
    # rows ARE published, so the document figure is named as the partial it is.
    excluded = [r["setup"] for r in results if "setup_total_unavailable" in r]
    payload = {
            "setup_count": len(results),
        "total_machining_time_seconds": round(grand, 1),
        "total_machining_time_hms": _hms(grand),
        "setups": results,
        "units": unit,
    "note": _TIME_NOTE + (_TOTAL_UNAVAILABLE_NOTE if excluded else ""),
    "assumptions": {"feed_scale_percent": feed_scale,
            "rapid_feed_cm_per_s": rapid_feed,
            "tool_change_seconds": tool_change},
    }
    if excluded:
        payload["total_excludes_setups"] = excluded
    return ok(payload)


def _hms(seconds) -> str:
    try:
        s = int(round(seconds))
    except Exception:
        return "0:00:00"
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"

_NC_PROGRAM_NOTE = (
    "operation_count is NCProgram.filteredOperations - unsuppressed operations in scope; "
    "posted_operations counts those reading hasToolpath True. item_count is NCProgram.operations, "
    "the SETUPS/folders it stores. empty_toolpath_count counts held operations that generated and "
    "cut nothing, empty_toolpaths names them, capped; a name several share carries its position in "
    "that list.")

_NC_EMPTY_NAME_CAP = 20


def _held_row_label(name, position):
    """One held operation labelled by its position in filteredOperations; '' where the name did
    not read."""
    return f"{name} (operation {position})" if name else ""


# filteredOperations excludes suppressed operations. Posting an unsuppressed operation without
# a toolpath can still require a modal response; hasToolpath alone does not prove posting completed.
def _program_held_ops(nc, cam=None) -> dict:
    """{operation_count, posted_operations, toolpath_unread?, empty_toolpath_count, empty_toolpaths?}
    for ONE NC program off filteredOperations; {} where that property does not read. A held list can
    draw operations from several setups, so shared names are told_apart by position."""
    ops = safe(lambda: list(nc.filteredOperations))
    if ops is None:
        return {}
    posted, unread = toolpath_present_tally(ops)
    out = {"operation_count": len(ops), "posted_operations": posted}
    if unread:
        out["toolpath_unread"] = unread
    empty_rows = []
    empty_count = 0
    for position, raw in enumerate(ops[:_MAX_ITEMS], 1):
        op = adsk.cam.Operation.cast(raw)
        if op is None:
            continue
        facts = op_state_facts(op, cam)
        if is_empty_toolpath(facts):
            empty_count += 1
            empty_rows.append((facts["name"], _held_row_label(facts["name"], position)))
    out["empty_toolpath_count"] = empty_count
    if empty_rows:
        # told_apart judges EVERY empty row and the cap is applied after, so a listed name whose
        # namesake falls outside the cap is still replaced.
        out["empty_toolpaths"] = told_apart(empty_rows)[:_NC_EMPTY_NAME_CAP]
    return out


def _nc_program_unit_record(params):
    """Read the optional native NC unit parameter and its own choices, if available."""
    parameter = safe(lambda: params.itemByName("nc_program_unit"))
    if parameter is None:
        return None
    record = {
        "expression": safe(lambda: parameter.expression),
        "value": safe(lambda: parameter.value.value),
    }
    value = safe(lambda: parameter.value)
    choices = None
    try:
        getter = getattr(value, "getChoices", None) if value is not None else None
        if callable(getter):
            choices = getter()
    except Exception:
        choices = None
    if isinstance(choices, (tuple, list)) and len(choices) == 3 and isinstance(choices[0], bool):
        if choices[0] is False:
            record["get_choices"] = {"ok": False}
        elif (isinstance(choices[1], (tuple, list))
              and isinstance(choices[2], (tuple, list))
              and len(choices[1]) == len(choices[2])):
            record["get_choices"] = {"ok": True, "titles": list(choices[1]),
                                      "values": list(choices[2])}
    return record


def get_nc_programs_handler() -> dict:
    """The document's NC programs: name, machine, post configuration, operation counts, and the
    post parameters the post exposes."""
    cam, err = get_cam()
    if err:
        return error(err)

    programs = []
    try:
        ncs = cam.ncPrograms
        for i in range(ncs.count):
            nc = ncs.item(i)
            entry = {
            "name": safe(lambda: nc.name),
            "operation_count": None,
            "machine": machine_label(safe(lambda: nc.machine)),
            "post": safe(lambda: nc.postConfiguration.description) if safe(lambda: nc.postConfiguration) else None,
            "post_parameters": [],
            }
            # MEASURED: NCProgram.operations holds the SETUPS/folders assigned to the program, so
            # its length is an item count; the operations figure is the filtered read below
            # (measure_api row cam-ncprogram-operations-hold-containers).
            items = safe(lambda nc=nc: list(nc.operations))
            if items is not None:
                entry["item_count"] = len(items)
            entry.update(_program_held_ops(nc, cam))
            unit_record = _nc_program_unit_record(safe(lambda: nc.parameters))
            if unit_record is not None:
                entry["nc_program_unit"] = unit_record
            params = safe(lambda: nc.postParameters)
            if params is not None:
                try:
                    for j in range(params.count):
                        p = params.item(j)
                        entry["post_parameters"].append({
                        "name": safe(lambda: p.name),
                        "title": safe(lambda: p.title),
                        "expression": safe(lambda: p.expression),
                        })
                except Exception:
                    pass
            programs.append(entry)
    except Exception as e:
        return error(f"Could not read NC programs: {e}")

    return ok({"nc_program_count": len(programs), "nc_programs": programs,
               "note": _NC_PROGRAM_NOTE})

# ── inspection results - the recorded probing measurements cam_get(include=['inspection']) reads ──

# Nothing in the API bounds the point count on a path, so the per-point read is capped.
_INSPECTION_ROW_DEFAULT = 50
_INSPECTION_ROW_CAP = 200

_OUT_OF_TOLERANCE = ("above_tolerance", "below_tolerance", "unprojected")

# A never-probed document reads CAM.inspectionResults as None on some documents and as an EMPTY
# collection on others; both are a zero-measure answer.
_INSPECTION_ABSENT_NOTE = (
    "No inspection results on this document: CAM.inspectionResults reads None, so there is no "
    "results folder to read. Results are recorded by a probing cycle on the machine; nothing in "
    "this server creates them.")

# CAMMeasure exposes inspectionPathResults and nothing else - no name, no operation, no id, and no
# call enumerates the names itemByName() would take, so a measure is addressable only by index.
_INSPECTION_UNREADABLE_NOTE = (
    "CAM.inspectionResults could not be read on this document - the property RAISED, and "
    "'read_error' carries the platform text. That is an UNREADABLE state, not an absence of "
    "results: a gated CAM member raises rather than reading empty.")

_INSPECTION_INDEX_NOTE = (
    "Measures are addressed by INDEX: a measure folder exposes no name through the API (its browser "
    "name is not readable, and no call lists the legal names), so no name is echoed back.")


def _read_inspection_results(cam) -> tuple:
    """(collection_or_None, raise_text_or_None) - a zero answer and a RAISING property kept
    apart, with the platform text."""
    reason = {}

    def read():
        try:
            return cam.inspectionResults
        except Exception as exc:
            reason["text"] = str(exc).strip() or repr(exc)
            raise

    results = safe(read, _MISSING)
    if results is _MISSING:
        return None, reason.get("text") or "the property read raised."
    return results, None


def _point_state_map() -> dict:
    """InspectionPointState value -> wire name, keyed off the live enum members."""
    return {getattr(adsk.cam.InspectionPointState, member, object()): name for member, name in (
        ("WithinTolerance", "within_tolerance"), ("AboveTolerance", "above_tolerance"),
        ("BelowTolerance", "below_tolerance"), ("Unprojected", "unprojected"))}


def _point_state_name(value, state_names) -> str:
    """The wire name for one point's state: 'unknown' when the state cannot be read, str(value) for a
    member this build does not name. Only a NAMED out-of-tolerance state is counted as one."""
    if value is None:
        return "unknown"
    return state_names.get(value, str(value))


def _xyz(pt, f):
    """[x, y, z] for a Point3D/Vector3D, scaled out of Fusion's internal CM (InspectionPointResult:
    "All values are in the Fusion's internal units which for positional and length values is CM").
    None when the point/vector itself is absent."""
    if pt is None:
        return None
    return [measured(lambda: pt.x, f), measured(lambda: pt.y, f), measured(lambda: pt.z, f)]


def _point_row(p, path_index, point_index, state, f) -> dict:
    """One measured point. Lengths go through measured(), not safe(read, 0.0): a deviation of 0.0 is
    an ANSWER ("dead on nominal"), so an unreadable field must read null instead of masquerading
    as one."""
    return {"path": path_index, "index": point_index, "state": state,
            "deviation": measured(lambda: p.deviation, f),
            "error": measured(lambda: p.error, f),
            "offset": measured(lambda: p.offset, f),
            "nominal": _xyz(safe(lambda: p.nominalPosition), f),
            "contact": _xyz(safe(lambda: p.contact), f),
            "projected": _xyz(safe(lambda: p.projectedPoint), f),
            "delta": _xyz(safe(lambda: p.delta), f)}


def _measure_paths(m) -> list:
    """One measure's InspectionPathResults as a list. CAMMeasure.inspectionPathResults is documented
    to return null when the measure holds none, so an absent collection reads as zero paths."""
    return list(iter_collection(safe(lambda: m.inspectionPathResults)))


def _measure_rollup(m, index, f, state_names) -> dict:
    """ONE measure's rollup: the per-state tally (terse - zero buckets dropped, so a clean measure
    reads {'within_tolerance': N}), the actionable out_of_tolerance count, and the worst (highest
    error) out-of-tolerance point. Exception-first: a clean measure carries no 'worst'."""
    tally = {}
    total = 0
    oot = 0
    worst = None
    worst_rank = None
    paths = _measure_paths(m)
    for pi, path in enumerate(paths):
        for qi, p in enumerate(iter_collection(safe(lambda path=path: path.pointResults))):
            total += 1
            state = _point_state_name(safe(lambda p=p: p.state), state_names)
            tally[state] = tally.get(state, 0) + 1
            if state not in _OUT_OF_TOLERANCE:
                continue
            oot += 1
            err = measured(lambda p=p: p.error, f)
            # Rank by MAGNITUDE: the identity ranking if error is unsigned, and the right one if it
            # is signed (a below-tolerance point would otherwise sort under every above-tolerance
            # one). The row still publishes the raw value, sign included.
            rank = None if err is None else abs(err)
            if worst is None or (rank is not None and (worst_rank is None or rank > worst_rank)):
                worst = {"path": pi, "point": qi, "state": state,
                         "deviation": measured(lambda p=p: p.deviation, f), "error": err}
                worst_rank = rank
    row = {"index": index, "path_count": len(paths), "point_count": total,
           "out_of_tolerance": oot}
    if tally:
        row["states"] = tally
    if worst:
        row["worst"] = worst
    return row


def _measure_points(paths, path_filter, f, state_names, cap) -> tuple:
    """(rows, points_in_scope, out_of_tolerance_in_scope, truncated) for one measure's paths, or one
    of them (path_filter). Only out-of-tolerance points become rows - the narrowing that keeps the
    deep read bounded however many points the path carries."""
    rows = []
    total = 0
    oot = 0
    truncated = False
    for pi, path in enumerate(paths):
        if path_filter is not None and pi != path_filter:
            continue
        for qi, p in enumerate(iter_collection(safe(lambda path=path: path.pointResults))):
            total += 1
            state = _point_state_name(safe(lambda p=p: p.state), state_names)
            if state not in _OUT_OF_TOLERANCE:
                continue
            oot += 1
            if len(rows) >= cap:
                truncated = True
                continue
            rows.append(_point_row(p, pi, qi, state, f))
    return rows, total, oot, truncated


def _parse_measure_scope(raw) -> tuple:
    """'<measure>' or '<measure>/<path>' -> (measure_index, path_index_or_None, None); a value that
    is neither -> (None, None, reason naming it)."""
    parts = [s.strip() for s in str(raw).split("/")]
    if len(parts) > 2:
        return None, None, (f"'measure' takes '<measure index>' or '<measure index>/<path index>' - "
                            f"'{raw}' has {len(parts)} parts.")
    idx = []
    for part in parts:
        if not part.isdigit():
            return None, None, (f"'measure': '{part}' is not a non-negative index. A measure folder "
                                "has no API-readable name, so the scope is '<measure index>' or "
                                "'<measure index>/<path index>'.")
        idx.append(int(part))
    return idx[0], (idx[1] if len(idx) == 2 else None), None


def _row_cap(max_results) -> int:
    return clamp_rows(max_results, _INSPECTION_ROW_DEFAULT, _INSPECTION_ROW_CAP)


def get_inspection_results_handler(measure: str = "", max_results: int = 0,
                                   units: str = "mm") -> dict:
    """The recorded probing results: a per-measure state rollup by default, or one measure's (or one
    path's) out-of-tolerance points when 'measure' scopes it. Lengths are scaled out of CM."""
    cam, err = get_cam()
    if err:
        return error(err)
    unit = (units or "mm").strip().lower()
    f = CM_TO_UNIT.get(unit)
    if f is None:
        return error(f"Unknown units '{units}'. Valid: mm, cm, in.")

    results, read_error = _read_inspection_results(cam)
    if read_error is not None:
        return ok({"available": False, "readable": False, "measure_count": 0, "measures": [],
                   "units": unit, "read_error": read_error, "note": _INSPECTION_UNREADABLE_NOTE})
    if results is None:
        return ok({"available": False, "readable": True, "measure_count": 0, "measures": [],
                   "units": unit, "note": _INSPECTION_ABSENT_NOTE})
    count = safe(lambda: results.count, 0) or 0
    state_names = _point_state_map()
    scope = (measure or "").strip()

    if not scope:
        rows = []
        truncated = False
        for i, m in enumerate(iter_collection(results)):
            if i >= _MAX_ITEMS:
                truncated = True
                break
            rows.append(_measure_rollup(m, i, f, state_names))
        note = _INSPECTION_INDEX_NOTE + (
            " Pass measure='<index>' (or '<index>/<path>') for that measure's out-of-tolerance "
            "points." if count else
            " The results collection is present but holds no measures.")
        return ok({"available": True, "measure_count": count, "measures": rows,
                   "measures_truncated": truncated, "units": unit, "note": note})

    mi, pi, perr = _parse_measure_scope(scope)
    if perr:
        return error(perr)
    if mi >= count:
        return error(f"measure index {mi} is out of range - this document holds {count} measure(s)"
                     + (f" (index 0 to {count - 1})." if count else ".") + " " +
                     _INSPECTION_INDEX_NOTE)
    m = safe(lambda: results.item(mi))
    if m is None:
        return error(f"measure index {mi} did not resolve to a measure folder.")
    paths = _measure_paths(m)
    if pi is not None and pi >= len(paths):
        return error(f"path index {pi} is out of range - measure {mi} holds {len(paths)} path(s)"
                     + (f" (index 0 to {len(paths) - 1})." if paths else "."))

    rows, total, oot, truncated = _measure_points(paths, pi, f, state_names, _row_cap(max_results))
    out = {"available": True, "measure": mi, "path_count": len(paths), "point_count": total,
           "out_of_tolerance": oot, "filter": "out_of_tolerance", "returned": len(rows),
           "truncated": truncated, "points": rows, "units": unit,
           "note": ("Out-of-tolerance points only (above/below tolerance and unprojected); "
                    "within-tolerance points are counted, not listed. " + _INSPECTION_INDEX_NOTE)}
    if pi is not None:
        out["path"] = pi
    return ok(out)


# ── the machine slice: what the setup's assigned machine allows, off _cam_common.machine_limits ───

_MACHINE_SLICE_NOTE = (
    "Spindle and axis limits come from the machine's kinematics parts (Machine.elements -> the "
    "kinematics element -> parts). A setup parameter named machine_dimension_x/y/z is a different "
    "number - measured -1 on a job whose axes read 762/406/508 mm - so it is never read as a "
    "travel. A tool-station or spindle field reading 0 is left out rather than published as a "
    "limit of 0.")


def get_machine_limits_handler(setup: str = "", units: str = "mm") -> dict:
    """Per setup: the assigned machine's spindle speed range and per-axis travels."""
    cam, err = get_cam()
    if err:
        return error(err)
    unit = (units or "mm").strip().lower()
    factor = CM_TO_UNIT.get(unit)
    if factor is None:
        return error(f"Unknown units '{units}'. Valid: mm, cm, in.")

    want = (setup or "").strip()
    if want:
        node, rerr = resolve_cam_node(cam, want, kinds=("setup",), label="setup")
        if rerr:
            return error(rerr)
        targets = [(node.name, node.obj)]
    else:
        targets = [(safe(lambda s=s: s.name), s) for s in setups(cam)]

    rows = []
    for label, s in targets:
        m = safe(lambda s=s: s.machine)
        rec = {"setup": label, "machine": machine_label(m)}
        if m is None:
            rec["kinematics_readable"] = False
            rec["blocked_by"] = setup_blockers(s)   # the shared code vocabulary, minted once
        else:
            rec.update(machine_limits(m, factor, unit))
        rows.append(rec)
    return ok({"setup_count": len(rows), "setups": rows, "units": unit,
               "note": _MACHINE_SLICE_NOTE})
