# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Control which CAM toolpaths are displayed (show/hide/isolate one operation's path, or a whole
folder), so an agent can study one at a time. Toggles Operation.isLightBulbOn - a plain data
property, unlike the modal simulation/in-process-stock UI commands, which this does not touch.
Toolpaths only render in the Manufacture workspace."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, read_flag, safe
from ._cam_common import get_cam, resolve_cam_node, operation_nodes, operations_under, find_setup

app = adsk.core.Application.get()

_ACTIONS = ("show", "hide", "isolate", "show_folder", "hide_all", "list")


def _set_bulb(o, on):
    """(took, read_back) - set the lightbulb and confirm it took; a bulb whose isLightBulbOn does
    not read answers None, which is unconfirmed rather than done."""
    o.isLightBulbOn = bool(on)
    now = read_flag(lambda: o.isLightBulbOn)
    return (now is not None and now == bool(on)), now


def _op_identity(o):
    """An operation's operationId - two walks of the CAM tree hand back DIFFERENT Python objects
    for one operation, so id() matches nothing across them."""
    return safe(lambda: o.operationId)


def _bulb_word(now):
    """What a bulb read ANSWERED, for a wire sentence: 'shown' / 'hidden' / 'unreadable' - never a
    bare None, which reads as a state the property held rather than a read that did not answer."""
    return "unreadable" if now is None else ("shown" if now else "hidden")


def _activate_owning_setup(cam, setup_name):
    """(activated_name_or_None, warning_or_None) - make the shown toolpath's OWN setup active, since
    the Manufacture workspace renders only the ACTIVE setup's models; already-active is (None, None)."""
    if not setup_name:
        return None, None
    s, _names, serr = find_setup(cam, setup_name)
    if not s:
        return None, f"Could not resolve this operation's setup '{setup_name}': {serr}"
    if safe(lambda: s.isActive) is True:
        return None, None
    try:
        s.activate()
    except Exception as e:
        return None, (f"Setup '{setup_name}' could not be activated ({e}) - the viewport still "
                      "shows the ACTIVE setup's models, not this operation's part.")
    state = safe(lambda: s.isActive)
    if state is False:
        return None, (f"activate() ran but setup '{setup_name}' still reads isActive=false - the "
                      "viewport still shows another setup's models, not this operation's part.")
    if state is not True:
        return None, (f"activate() ran but isActive cannot be read on setup '{setup_name}', so the "
                      "activation is UNCONFIRMED - the viewport may still show another setup's "
                      "models rather than this operation's part.")
    return setup_name, None


def _fit_operation():
    """Fit the camera (plain fit-to-all). Any API refusal raises into the handler's error path."""
    vp = app.activeViewport
    cam = vp.camera
    cam.isFitView = True
    vp.camera = cam
    vp.refresh()


def handler(action: str = "", operation: str = "", folder: str = "", fit: bool = False) -> dict:
    """See TOOL_DESCRIPTION."""
    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        return error(f"Unknown action '{action}'. Valid: {', '.join(_ACTIONS)}.")
    cam, err = get_cam()
    if err:
        return error(err)

    if action == "list":
        rows = []
        for node in operation_nodes(cam):
            o = node.obj
            rows.append({"setup": node.setup, "op": node.name,
        "has_toolpath": safe(lambda o=o: o.hasToolpath),
        "valid": safe(lambda o=o: o.isToolpathValid),
        "suppressed": safe(lambda o=o: o.isSuppressed),
        "shown": safe(lambda o=o: o.isLightBulbOn)})
        return ok({"action": "list", "operation_count": len(rows), "operations": rows})

    if action == "hide_all":
        n = 0
        failed = 0
        for node in operation_nodes(cam):
            o = node.obj
            if safe(lambda o=o: o.hasToolpath):
                took, _now = _set_bulb(o, False)
                if took:
                    n += 1
                else:
                    failed += 1
        app.activeViewport.refresh()
        out = {"action": "hide_all", "hidden_count": n}
        if failed:
            out["toggle_failures"] = failed
            # Worded on the read, not on a state: a bulb that reads back true and one that does not
            # read at all both FAIL to read back false, and this count cannot tell them apart.
            out["note"] = (f"{failed} operation(s) did not read back isLightBulbOn=false after the "
                           "hide.")
        return ok(out)

    if action == "show_folder":
        if not folder.strip():
            return error("Provide 'folder' - the folder or setup name to show.")
        fnode, ferr = resolve_cam_node(cam, folder, kinds=("setup", "folder"), label="folder/setup")
        if ferr:
            return error(ferr + " Use cam_show_toolpath(list) or cam_get(include=['operations']).")
        ops, matched = operations_under(fnode.obj), fnode.name
        # hide everything, then show this folder's generated ops. The mass-hide's own read-backs are
        # KEPT: an op that would not go dark is still on screen, which is the opposite of what
        # 'show only this folder' promised.
        hide_failed = []
        for node in operation_nodes(cam):
            took, _now = _set_bulb(node.obj, False)
            if not took:
                hide_failed.append((_op_identity(node.obj), node.name))
        shown = []
        failed = []
        shown_ids = set()
        for o in ops:
            if safe(lambda o=o: o.hasToolpath):
                took, _now = _set_bulb(o, True)
                if took:
                    shown.append(safe(lambda o=o: o.name))
                    oid = _op_identity(o)
                    if oid is not None:
                        shown_ids.add(oid)
                else:
                    failed.append(safe(lambda o=o: o.name))
        # A hide that did not take on an op this call then SHOWED ends lit as asked, so it drops -
        # matched by operationId, the identity two walks share. An id that did not read matches
        # nothing and its read-back is reported instead.
        still_lit = [nm for oid, nm in hide_failed if oid is None or oid not in shown_ids]
        activated, setup_warning = _activate_owning_setup(cam, fnode.setup)
        app.activeViewport.refresh()
        out = {"action": "show_folder", "folder": matched, "shown": shown,
        "shown_count": len(shown),
        "note": "Only this folder's generated toolpaths are shown."}
        if activated:
            out["setup_activated"] = activated
            out["note"] += (f" Activated setup '{activated}' so the viewport renders THIS folder's "
                            "part - only the active setup's models are displayed.")
        if setup_warning:
            out["setup_activation_warning"] = setup_warning
            out["note"] += " " + setup_warning
        if failed:
            out["toggle_failures"] = failed
            out["note"] = (f"{len(failed)} operation(s) did not read back isLightBulbOn=true after "
                           "the show - see toggle_failures. " + out["note"])
        if still_lit:
            out["hide_failures"] = still_lit
            out["note"] = (f"{len(still_lit)} operation(s) did not read back isLightBulbOn=false "
                           "after the hide - see hide_failures; their toolpaths may still be "
                           "drawn. " + out["note"])
        return ok(out)

    # show / hide / isolate a single operation - the shared resolver REFUSES a duplicated name
    # (naming each candidate's setup path) instead of silently toggling the wrong toolpath.
    if not operation.strip():
        return error(f"Provide 'operation' - the operation name to {action}.")
    onode, oerr = resolve_cam_node(cam, operation, kinds=("operation",), label="operation")
    if oerr:
        return error(oerr + " Use cam_show_toolpath(list) to see every operation.")
    o = onode.obj
    name = onode.name

    if action == "hide":
        took, now = _set_bulb(o, False)
        if not took:
            return error(f"isLightBulbOn did not take for '{name}' - it reads back "
                         f"{_bulb_word(now)}.")
        app.activeViewport.refresh()
        return ok({"action": "hide", "operation": name})

    still_lit = []
    if action == "isolate":
        # The mass-hide's read-backs are KEPT: an op that would not go dark is still drawn, and
        # 'isolate' is the one action that promised nothing else would be.
        target_id = _op_identity(o)
        hide_failed = []
        for node in operation_nodes(cam):
            hid, _now = _set_bulb(node.obj, False)
            if not hid:
                hide_failed.append((_op_identity(node.obj), node.name))
        # The target is shown immediately below, so its own refused hide ends lit as asked and is
        # dropped - matched by operationId, since this walk and the resolver that produced `o` hold
        # different objects for one operation. An id that did not read matches nothing.
        still_lit = [nm for oid, nm in hide_failed
                     if target_id is None or oid is None or oid != target_id]
    took, now = _set_bulb(o, True)

    # The toggle is judged BEFORE the toolpath branch: a bulb that did not take is an error whether
    # or not the operation has a path to draw, so the pathless warning cannot carry a failed toggle.
    if not took:
        return error(f"isLightBulbOn did not take for '{name}' - it reads back {_bulb_word(now)}.")
    if not safe(lambda: o.hasToolpath):
        app.activeViewport.refresh()
        out = {"action": action, "operation": name,
        "warning": "This operation has no generated toolpath yet - nothing to display. "
        "Generate it first (cam_generate).",
        "has_toolpath": False}
        # an isolate that reached here still ran its mass-hide, so its read-backs are disclosed on
        # this arm too rather than dropped with the early return
        if still_lit:
            out["hide_failures"] = still_lit
        return ok(out)

    # BEFORE the fit: the displayed model is the active setup's, so the operation's own setup has to
    # be active or the fit frames another setup's part.
    activated, setup_warning = _activate_owning_setup(cam, onode.setup)

    fitted = False
    if fit:
        _fit_operation()   # raises on an API refusal, so reaching the payload means it applied
        fitted = True
    app.activeViewport.refresh()
    note = ("Toolpath shown. Toolpaths render in the Manufacture workspace; pair with "
            "view_screenshot.")
    out = {"action": action, "operation": name, "fit": fitted, "setup": onode.setup}
    if activated:
        out["setup_activated"] = activated
        note += (f" Activated setup '{activated}' (the operation's own): the viewport renders only "
                 "the ACTIVE setup's models, so the toolpath would otherwise sit beside another "
                 "setup's part.")
    if setup_warning:
        out["setup_activation_warning"] = setup_warning
        note += " " + setup_warning
    if still_lit:
        out["hide_failures"] = still_lit
        note += (f" {len(still_lit)} operation(s) did not read back isLightBulbOn=false during the "
                 "hide - see hide_failures; their toolpaths may still be drawn.")
    out["note"] = note
    return ok(out)


TOOL_DESCRIPTION = (
    "Show or hide CAM toolpaths to inspect one operation's path at a time. They render only in the "
    "MANUFACTURE workspace; pair with view_screenshot."
)

tool = (
    Tool.create_simple(name="cam_show_toolpath", description=TOOL_DESCRIPTION)
    .add_input_property("action", {"type": "string", "enum": list(_ACTIONS)})
    .add_required_input("action")
    .add_input_property("operation", {"type": "string",
            "description": "Operation name (show/hide/isolate)."})
    .add_input_property("folder", {"type": "string",
            "description": "Folder or setup name (show_folder)."})
    .add_input_property("fit", {"type": "boolean",
            "description": "Fit the camera after showing."})
    .strict_schema()
)

item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # show / hide / isolate error on a bulb that does not read back the value set - an unreadable
    # isLightBulbOn included, since an unconfirmed toggle is not a done one. The bulk arms publish
    # the ops whose toggle did not take (toggle_failures / hide_failures) instead of erroring.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_cam_show_toolpath.py::TestBulbReadBack"
                      "::test_hide_of_a_stuck_bulb_is_an_error",
        rung="value"))


def register_tool():
    register(item)
