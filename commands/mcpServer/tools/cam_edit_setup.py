# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Edit a CAM setup: any named parameter (WCS orientation/origin, stock size, ...) and/or its
model/fixture/stock body collections. Parameters are validated before any is applied, so a typo
can't half-edit the setup."""

import adsk.core
import adsk.cam

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import apply_rename, ok, error, read_flag, safe
# The machine catalog read + the by-name machine resolver are the shared CAM substrate's (one home,
# so cam_get's catalog, this assignment and cam_create_machine's reachability gate cannot drift).
from ._cam_common import (STOCK_MODES, get_cam, find_setup, enumeration_remedy, expression_error,
                          machine_catalog, machine_label, matched_quoting, parse_parameters,
                          resolve_machine, stock_mode_member, stock_mode_name, unquote_expression)
from .cam_create_setup import setup_name_clash
from . import _inputs

app = adsk.core.Application.get()

# the three editable body collections: input arg -> (Setup attribute, result key)
_BODY_COLLECTIONS = {
    "models": ("models", "models_set"),
    "fixtures": ("fixtures", "fixtures_set"),
    "stock": ("stockSolids", "stock_set"),
}

# target-list input kind: bodies (handle/name) OR component occurrences - reused for all
# three collections. A COMPONENT occurrence keeps the setup's selection when contents are swapped
# (Setup.models/fixtures/stockSolids accept Occurrence, BRepBody, or MeshBody).
_TARGETS = _inputs.TargetRefList("bodies", required=False)

_PARAM_READ = "cam_get(include=['parameters'], setup=...)"

# The stock the setup machines from: Setup.stockMode is the knob this input assigns.
_STOCK_MODE = _inputs.Choice("stock_mode", options=list(STOCK_MODES), required=False)

# WCS geometry-binding, {key: (mode_param, mode_value, cad_param, handle-requirement)}: each key
# drives one CadObjectParameterValue plus the choice-mode it needs. A bound WCS follows that
# geometry, so a design edit moving it invalidates the ops.
_WCS_BINDINGS = {
    "origin":  ("wcs_origin_mode",      "'point'",  "wcs_origin_point",         "any"),
    "z_axis":  ("wcs_orientation_mode", "'axesZX'", "wcs_orientation_axisZ",    "any"),
    "x_axis":  ("wcs_orientation_mode", "'axesZX'", "wcs_orientation_axisX",    "any"),
}
# One handle each. A WCS value may also be a JOINT ORIGIN, by the handle
# assembly_get(include=['joint_origins']) mints or by name - JointOriginRef recognises both.
_WCS_HANDLE = _inputs.GeometryHandle("wcs_handle", require="any")
_WCS_JO = _inputs.JointOriginRef("wcs_jo")


# Said only where two listed rows share a name. A row's identity is description/vendor/model and
# both copies read the same triple, so nothing here tells them apart.
_SHARED_NAME_NOTE = (
    " A row marked name_in_both_locations shares its name with the OTHER location's copy: the name "
    "addresses two machines, an assignment by it reaches the local one, and a setup reading that "
    "machine name does not say which copy it carries. Delete the local copy to reach the shipped "
    "one by name.")


def _object_collection():
    return adsk.core.ObjectCollection.create()


def read_machines(vendor: str = "", machine_type: str = "", max_results: int = 100):
    """The wire wrapper over _cam_common.machine_catalog: every machine in the Local + Fusion360
    locations, filtered by vendor and/or machine_type. Read-only; cam_get(include=['machines']) is
    the wire surface."""
    rows, truncated, err = machine_catalog(vendor, machine_type, max_results)
    if err:
        return error(err)
    note = ("Pass a machine's exact 'name' to cam_edit_setup(machine=...); "
            "machine_type='milling' narrows past the additive printers. Assigning a "
            "simulation_ready machine can be REFUSED - machine_strip_simulation=true "
            "assigns it without its simulation model, and the stripped copy's spindle "
            "maximum and axis ranges read back unchanged through Setup.machine.")
    if any(r.get("name_in_both_locations") for r in rows):
        note += _SHARED_NAME_NOTE
    if truncated:
        # The flag is computed over the LISTED rows, so a collision whose other copy fell past the
        # cap carries no mark at all - said here rather than left to read as 'no collisions'.
        note += (" The listing was CAPPED, and name_in_both_locations is read over the listed rows "
                 "only - a copy past the cap is not marked. Narrow with vendor/machine_type, or "
                 "raise max_results, before reading an unmarked row as unique.")
    return ok({"machines": rows, "count": len(rows), "truncated": truncated, "note": note})


def _resolve_bodies(names):
    """Resolve a list of body handles/names OR component occurrence names to entities the
    CAM API accepts (Occurrence / BRepBody / MeshBody) via the shared TargetRefList kind.
    Returns (entities, None) or (None, error)."""
    return _TARGETS.resolve(names)


def _resolve_wcs_value(value):
    """Resolve one WCS binding value: a JOINT ORIGIN (its handle OR name, via JointOriginRef) OR a
    find_geometry face/edge/vertex handle. JO is tried first (it also recognises a JO entityToken); a
    non-JO handle falls back to the geometry handle. Returns (entity, is_joint_origin, error)."""
    jo, jerr = _WCS_JO.resolve(value)
    if jo is not None:
        return jo, True, None
    ent, herr = _WCS_HANDLE.resolve(value)
    if ent is not None:
        return ent, False, None
    # both failed: surface the more relevant error (the handle error for a token, the JO error for a name)
    return None, False, ((herr if _inputs.is_handle(value) else jerr) or herr or jerr)


def _resolve_wcs(wcs):
    """Resolve a {origin/z_axis/x_axis: value} WCS request to {key: entity}. Each value is a Joint Origin
    (handle or name) or a find_geometry handle. Returns (resolved, None) or (None, error). Empty ->
    ({}, None)."""
    if not isinstance(wcs, dict):
        return None, ("'wcs' must be an object like {'origin': <handle-or-JO>, 'z_axis': <handle>} - a "
                      "find_geometry handle or a Joint Origin per axis you want to bind.")
    unknown = [k for k in wcs if k not in _WCS_BINDINGS]
    if unknown:
        return None, (f"'wcs' has unknown key(s): {', '.join(unknown)}. "
                      f"Bindable: {', '.join(_WCS_BINDINGS)}.")
    resolved = {}
    for key, value in wcs.items():
        if value in (None, "", []):
            continue
        ent, is_jo, err = _resolve_wcs_value(value)
        if err:
            return None, f"wcs.{key}: {err}"
        # The platform ACCEPTS a Joint Origin for the ORIGIN binding but throws
        # InternalValidationError binding one to an AXIS (verified live) - refuse with the
        # working recipe instead of surfacing the raw platform error.
        if is_jo and key != "origin":
            return None, (f"wcs.{key}: a Joint Origin can bind the WCS ORIGIN only - the platform "
                          "rejects one as an axis. Bind z_axis/x_axis to a face (its normal) or a "
                          "straight edge via a find_geometry handle; to center a WCS on a Joint "
                          "Origin, pass it as wcs.origin.")
        resolved[key] = ent
    return resolved, None


def _bind_cad_param(setup, cad_param_name, entity):
    """Bind one CadObjectParameterValue to a geometry entity IN PLACE (its .value takes a list; the
    CAMParameter.value itself has no setter). Returns the bound-entity count read back, or (None, err)."""
    p = safe(lambda: setup.parameters.itemByName(cad_param_name))
    if p is None:
        return None, f"setup has no parameter '{cad_param_name}'."
    cad = safe(lambda: p.value)
    if cad is None:
        return None, f"could not read '{cad_param_name}' value object."
    try:
        cad.value = [entity]                     # MUTATION - in place; do NOT reassign p.value
    except Exception as e:
        return None, f"could not bind '{cad_param_name}' to the geometry: {e}"
    after = safe(lambda: setup.parameters.itemByName(cad_param_name).value.value)
    return (len(list(after)) if after else 0), None


def handler(setup: str = "", parameters=None, models=None, fixtures=None, stock=None,
            machine: str = "", machine_strip_simulation: bool = False, wcs=None,
            rename: str = "", stock_mode: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    if not (setup or "").strip():
        return error("Provide 'setup' - the CAM setup name (see cam_get).")

    # parse parameters (may be empty)
    wanted = {}
    if parameters not in (None, "", {}):
        wanted, perr = parse_parameters(parameters)
        if perr:
            return error(perr)

    body_args = {k: v for k, v in (("models", models), ("fixtures", fixtures), ("stock", stock))
                 if v not in (None, "", [])}
    want_machine = (machine or "").strip()
    want_wcs = wcs not in (None, "", {}, [])
    want_rename = (rename or "").strip()

    want_stock_mode = None
    if (stock_mode or "").strip():
        want_stock_mode, merr = _STOCK_MODE.resolve(stock_mode)
        if merr:
            return error(merr)
        # The 'stock' arm below switches the setup to SolidStock to hold the bodies it is handed,
        # so a mode asked for in the same call would decide the setup's stock twice.
        if "stock" in body_args:
            return error(f"'stock_mode={want_stock_mode}' and a 'stock' body list in one call set "
                         "the setup's stock two ways - 'stock' is the from-solid mode with its "
                         "bodies. Pass one or the other.")

    if not (wanted or body_args or want_machine or want_wcs or want_rename or want_stock_mode):
        return error("Nothing to do. Provide 'parameters' {name: expression}, "
                     "'models'/'fixtures'/'stock' body lists, a 'machine', a 'stock_mode', "
                     "a 'wcs' binding, and/or 'rename'.")

    cam, cerr = get_cam()
    if cerr:
        return error(cerr)
    # The resolver's own refusal is returned verbatim: it is the one place that knows whether the
    # name was ABSENT or AMBIGUOUS, and only it can say which.
    target, _names, serr = find_setup(cam, setup)
    if not target:
        return error(serr)

    # ── validate EVERYTHING before applying anything (no half-edited setup) ──
    if want_rename:
        clash = setup_name_clash(cam, want_rename, safe(lambda: target.name) or setup)
        if clash:
            return error(clash)
    sp = safe(lambda: target.parameters)
    resolved_params = {}
    missing = []
    for name in wanted:
        p = safe(lambda name=name: sp.itemByName(name)) if sp else None
        if p is None:
            missing.append(name)
        else:
            resolved_params[name] = p
    if missing:
        return error(f"Setup '{setup}' has no parameter(s): {', '.join(missing)}. "
                     "(Read the setup's parameter names first; only existing ones are settable.)")

    # A parameter reading isEditable False takes the assignment without raising and keeps the
    # expression it held (measured: 274 of a setup's 304 read False), so refuse before any write.
    # read_flag, not safe(..., True): a flag that reads None did not answer, and cannot refuse.
    locked = [name for name, p in resolved_params.items()
              if read_flag(lambda p=p: p.isEditable) is False]
    if locked:
        return error(f"Setup '{setup}' does not accept a write to: {', '.join(locked)} "
                     "(isEditable reads False on each). Nothing was applied. A setup exposes many "
                     "parameters it takes no write to; set one it does - "
                     "cam_get(include=['parameters'], setup=...) marks each refusing row "
                     "editable false.")

    resolved_bodies = {}
    for arg, names in body_args.items():
        bodies, berr = _resolve_bodies(names)
        if berr:
            return error(f"{arg}: {berr}")
        resolved_bodies[arg] = bodies

    resolved_machine = None
    if want_machine:
        m_obj, m_label, m_err = resolve_machine(want_machine)
        if m_err:
            return error(m_err)
        resolved_machine = (m_obj, m_label)

    resolved_wcs = {}
    if want_wcs:
        resolved_wcs, werr = _resolve_wcs(wcs)
        if werr:
            return error(werr)

    # ── apply parameters first (before bodies/machine/wcs, so a rollback here leaves the setup as found) ──
    changed = []
    eval_failures = []
    no_takes = []
    unreadable = []
    written_of = {}
    for name, expr in wanted.items():
        p = resolved_params[name]
        before = safe(lambda p=p: p.expression)
        # A parameter already holding a QUOTED expression stores a string, and Fusion refuses the
        # bare spelling ('3 : Invalid enumeration value.'), so the request is wrapped to match.
        written, quoted = matched_quoting(before, expr)
        written_of[name] = written
        try:
            p.expression = written
        except Exception as e:
            return error(f"Could not set '{name}' = '{expr}' on setup '{setup}': {e}. "
                         f"(Already applied: {', '.join(c['name'] for c in changed) or 'none'}.)"
                         + enumeration_remedy(str(e), written, _PARAM_READ))
        # Read the expression BACK for its evaluation state: the platform stores an unresolvable
        # expression silently (edited==true, .expression echoes the text) - only .error exposes it.
        eval_err, eval_warn = expression_error(p)
        after = safe(lambda p=p: p.expression)
        rec = {"name": name, "before": before, "after": after}
        if quoted:
            rec["quoted"] = True            # absent = the request was written as it was sent
        if eval_warn:
            rec["warning"] = eval_warn
        changed.append(rec)
        if after is None:
            unreadable.append((name, str(expr)))
        elif eval_err:
            eval_failures.append((name, str(expr), eval_err))
        elif unquote_expression(after) != unquote_expression(written):
            # A numeric parameter reads its expression back as written, while a STRING parameter
            # (a setup carries those too) stores it single-quoted - so both sides go through the
            # shared codec rather than comparing bytes.
            no_takes.append((name, str(expr), after))

    # A stored-but-unevaluated expression is a swallowed no-op the platform reports as success, and
    # so is one reading back anything but what was written. Every parameter set here is rolled back
    # to its prior expression, so the setup is left exactly as found.
    if eval_failures or no_takes or unreadable:
        for rec in changed:
            safe(lambda rec=rec: setattr(resolved_params[rec["name"]], "expression", rec["before"]))
        parts = []
        if eval_failures:
            detail = "; ".join(f"'{n}' = '{e}' ({why})" for n, e, why in eval_failures)
            parts.append(f"expression did not evaluate - {detail}")
        if no_takes:
            detail = "; ".join(f"'{n}' = '{e}' (it reads back '{a}')" for n, e, a in no_takes)
            parts.append(f"the assignment did not take - {detail}")
        if unreadable:
            detail = "; ".join(f"'{n}' = '{e}'" for n, e in unreadable)
            parts.append("the expression cannot be read back, so the change is UNCONFIRMED - "
                         + detail)
        if eval_failures:
            remedy = ("(A CAM stock/setup expression must reference existing parameters and "
                      "resolve to a value - check names and units.)")
            remedy += next((c for c in (enumeration_remedy(why, written_of[n], _PARAM_READ)
                                        for n, _e, why in eval_failures) if c), "")
        elif no_takes:
            remedy = ("(Every row in this call passed the isEditable check, so a locked parameter "
                      "is not the reason - re-read the setup with cam_get(include=['parameters'], "
                      "setup=...).)")
        else:
            remedy = "(Re-read the setup with cam_get(include=['parameters'], setup=...).)"
        return error(f"Setup '{setup}': {'; '.join(parts)}. Rolled back all "
                     f"{len(changed)} parameter(s); no change was applied. {remedy}")

    result = {
        "edited": True,
        "setup": safe(lambda: target.name),
        "updated_count": len(changed),
        "changed": changed,
    }

    if want_stock_mode is not None:
        member = stock_mode_member(want_stock_mode)
        if member is None:
            return error(f"This Fusion build's SetupStockModes carries no "
                         f"'{STOCK_MODES[want_stock_mode]}' member, so 'stock_mode="
                         f"{want_stock_mode}' cannot be assigned. Pick another mode.")
        was = stock_mode_name(safe(lambda: target.stockMode))
        try:
            target.stockMode = member
        except Exception as e:
            return error(f"Could not set stock_mode='{want_stock_mode}' on setup '{setup}': {e}.")
        applied = stock_mode_name(safe(lambda: target.stockMode))
        if applied != want_stock_mode:
            return error(f"Stock mode did not take on setup '{setup}': set '{want_stock_mode}' but "
                         f"Setup.stockMode now reads '{applied}'.")
        result["stock_mode_set"] = applied
        result["was_stock_mode"] = was

    for arg, bodies in resolved_bodies.items():
        attr, key = _BODY_COLLECTIONS[arg]
        # Fusion refuses the collection unless its enabling prerequisite is set FIRST: stock solids need
        # stockMode='SolidStock', fixtures need fixtureEnabled=True. Set them here, not in the caller.
        if arg == "stock":
            try:
                target.stockMode = adsk.cam.SetupStockModes.SolidStock
            except Exception as e:
                return error(f"Could not switch setup '{setup}' to from-solid stock (SolidStock mode): {e}.")
        elif arg == "fixtures":
            try:
                target.fixtureEnabled = True
            except Exception as e:
                return error(f"Could not enable fixtures on setup '{setup}': {e}.")
        coll = _object_collection()
        for b in bodies:
            coll.add(b)
        try:
            setattr(target, attr, coll)
        except Exception as e:
            return error(f"Could not set {arg} on setup '{setup}': {e}.")
        got = safe(lambda target=target, attr=attr: getattr(target, attr).count, 0) or 0
        # Read the collection back: setting it and getting 0 is a swallowed no-op, not a success.
        if got != len(bodies):
            return error(f"Set {arg} on setup '{setup}' but it reads back {got} bodies, not "
                         f"{len(bodies)} - the assignment did not take.")
        result[key] = got

    if resolved_machine is not None:
        m_obj, m_label = resolved_machine
        if machine_strip_simulation:
            # Measured on ONE simulation-ready library machine (Fusion 2705.1.4): Setup.machine
            # refused it, and stripping the simulation model from the TRANSIENT resolved copy (the
            # library asset untouched) let it land with spindle maximum and axis ranges unchanged.
            try:
                m_obj.clearSimulationModel()
            except Exception as e:
                return error(f"Could not strip the simulation model from '{m_label}': {e}")
            result["machine_simulation_stripped"] = True
        try:
            target.machine = m_obj                       # Setup.machine takes a transient copy
        except Exception as e:
            hint = ("" if machine_strip_simulation or "simulation" not in str(e).lower() else
                    " That refusal names the simulation model: pass "
                    "machine_strip_simulation=true to assign this machine without its simulation "
                    "model - the spindle maximum and every axis range read back unchanged through "
                    "Setup.machine - or pick a simulation_ready=false machine from "
                    "cam_get(include=['machines']).")
            return error(f"Could not assign machine '{want_machine}' to setup '{setup}': {e}.{hint}")
        # Read Setup.machine back to CONFIRM the assignment took - a swallowed no-op must not report ok.
        applied = machine_label(safe(lambda: target.machine))
        if not applied or applied != m_label:
            return error(f"Machine assignment did not take on setup '{setup}': set '{m_label}' but the "
                         f"setup now reports '{applied}'.")
        result["machine_set"] = applied

    if resolved_wcs:
        wcs_set = {}
        for key, entity in resolved_wcs.items():
            mode_param, mode_value, cad_param, _req = _WCS_BINDINGS[key]
            mp = safe(lambda mode_param=mode_param: target.parameters.itemByName(mode_param))
            if mp is None:
                return error(f"Setup '{setup}' has no WCS mode parameter '{mode_param}'.")
            try:
                mp.expression = mode_value        # e.g. wcs_origin_mode -> 'point'
            except Exception as e:
                return error(f"Could not set WCS mode '{mode_param}={mode_value}' on setup '{setup}': {e}.")
            bound, berr = _bind_cad_param(target, cad_param, entity)
            if berr:
                return error(f"wcs.{key}: {berr}")
            # Binding and reading 0 entities back is a swallowed no-op - a geometry-bound WCS with no
            # geometry is not what was asked for.
            if not bound:
                return error(f"wcs.{key} bound no geometry - '{cad_param}' reads back empty after the "
                             "set. The handle may not be a valid WCS reference for this setup.")
            wcs_set[key] = {"mode": safe(lambda mode_param=mode_param:
                                         target.parameters.itemByName(mode_param).value.value),
                            "bound_entities": bound}
        result["wcs_set"] = wcs_set

    # The rename runs LAST, its name clash already refused above. Setup.name DEDUPES rather than
    # refusing, so a landed name matching neither the request nor the name it held is the setup's
    # new address; only an unmoved name is a declined rename.
    renamed_note = ""
    if want_rename and want_rename == result["setup"]:
        # A rename onto the name it already reads is a NO-OP: writing it again makes the platform
        # dedupe the setup against itself ('LegSetup' -> 'LegSetup1', measured).
        result["renamed"] = False
        result["name_unchanged"] = True
        renamed_note = f" Setup.name already reads '{result['setup']}' - nothing was written."
    elif want_rename:
        was = result["setup"]
        final, _declined = apply_rename(target, want_rename)
        # An unread name settles NOTHING - neither the declined case nor the deduped one - and
        # publishing it would hand back 'None' as the setup's address.
        if final is None:
            return error(f"Set the name of setup '{was}' to '{want_rename}' but Setup.name does "
                         "not read back, so the rename is UNCONFIRMED. Re-read it with cam_get. "
                         "Every other change in this call was applied and is NOT rolled back.")
        if final == was:
            return error(f"Renaming setup '{was}' to '{want_rename}' did not take - Setup.name "
                         f"still reads {final!r}. Every other change in this call was applied and "
                         "is NOT rolled back.")
        result["setup"] = final
        result["was_setup"] = was
        result["renamed"] = True
        renamed_note = f" Setup.name now reads '{final}' (was '{was}')."
        if final != want_rename:
            result["name_deduped"] = True   # absent = the name landed exactly as requested
            renamed_note += f" Address the setup as '{final}' from here."

    result["note"] = (renamed_note.strip() + (" " if renamed_note else "")
                      + "Setup edited. Existing toolpaths are now OUT OF DATE - regenerate with "
                      "cam_generate. A WCS bound via 'wcs' is a LIVE reference to the selected geometry "
                      "or Joint Origin (bound_entities), so the WCS re-derives from it - a self-centering "
                      "Joint Origin keeps the WCS centered as its anchor updates; a design edit that "
                      "moves the reference invalidates the ops.")
    return ok(result)


TOOL_DESCRIPTION = (
    "Edit a CAM SETUP: its machine, its model/fixture/stock selections (bodies or occurrence names, "
    "each REPLACED), its WCS, any other setup parameter, or its name. Browse machines with "
    "cam_get(include=['machines'])."
)

tool = (
    Tool.create_simple(name="cam_edit_setup", description=TOOL_DESCRIPTION)
    .add_input_property("setup", {"type": "string", "description": "Setup name (from cam_get)."})
    .add_input_property("parameters", {"type": "object",
            "description": "{name: expression} (or 'name=value,...'), by this setup's own parameter names."})
    .add_input_property("models", {"type": "array", "items": {"type": "string"}})
    .add_input_property("fixtures", {"type": "array", "items": {"type": "string"}})
    .add_input_property("stock", {"type": "array", "items": {"type": "string"}})
    .add_input_property(*_STOCK_MODE.as_property())
    .add_input_property("machine", {"type": "string",
            "description": "'vendor|model', or a bare model."})
    .add_input_property("machine_strip_simulation", {"type": "boolean"})
    .add_input_property("wcs", {"type": "object",
            "description": "{origin/z_axis/x_axis: a find_geometry handle or a Joint Origin handle/name}."})
    .add_input_property("rename", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # The body, machine, stock_mode and wcs arms each re-read what they wrote and error on a
    # mismatch. The parameter arm errors on the parameter's evaluation channel AND on a read-back
    # that is not the expression written (an unreadable one included), rolling every one back.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_cam_edit_setup.py::TestMachine"
                      "::test_assignment_that_does_not_take_is_error",
        rung="value"))


def register_tool():
    register(item)
