# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""cam_activate_setup - make a CAM setup active and fit the view (a state-changer, not a read; it
stays a standalone tool rather than a cam_get slice)."""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from ._cam_common import get_cam, find_setup

app = adsk.core.Application.get()


def activate_setup_handler(setup: str = "") -> dict:
    want = (setup or "").strip()
    if not want:
        return error("Provide 'setup' - the name of the setup to activate.")
    cam, err = get_cam()
    if err:
        return error(err)

    # The resolver's own refusal is returned verbatim: it is the one place that knows whether the
    # name was ABSENT or AMBIGUOUS, and only it can say which.
    target, _names, serr = find_setup(cam, want)
    if not target:
        return error(serr)

    try:
        target.activate()
    except Exception as e:
        return error(f"Failed to activate '{want}': {e}")
    # Gate on 'is not True', not 'is False': an isActive that does not READ leaves the activation
    # unconfirmed, and unconfirmed is not activated (the sibling flag reads gate the same way -
    # cam_show_toolpath._activate_owning_setup, cam_edit_operation._set_suppressed).
    state = safe(lambda: target.isActive)
    if state is False:
        return error(f"activate() ran but '{want}' still reads isActive=false - the setup did not "
                     "become active.")
    if state is not True:
        return error(f"activate() ran but isActive cannot be read on '{want}', so the activation is "
                     "UNCONFIRMED. Re-read the setups with cam_get.")

    # Fit the view so a subsequent view_screenshot frames the setup. A fit that does not run leaves
    # the activation standing, so it is REPORTED rather than raised - and the note states only what
    # this call observed, never a fit it did not see happen.
    fit_failure = None
    try:
        vp = app.activeViewport
        if not vp:
            fit_failure = "there is no active viewport"
        # Gated on 'is not True' for the same reason the isActive read above is: fit() is declared
        # bool ('Returns true if successful'), so anything else is an answer this call cannot read
        # a completed fit out of.
        elif vp.fit() is not True:
            fit_failure = "Viewport.fit() did not answer true"
    except Exception as e:
        fit_failure = str(e)

    out = {"activated": safe(lambda: target.name), "view_fit": fit_failure is None}
    if fit_failure is None:
        out["note"] = "Setup activated and view fit. Use view_screenshot to capture it."
    else:
        out["fit_error"] = fit_failure
        out["note"] = (f"Setup activated. The view fit did not complete ({fit_failure}), so the "
                       "camera may not frame this setup - orient it with view_set before "
                       "view_screenshot.")
    return ok(out)


_activate_tool = Tool.create_with_string_input(
    name="cam_activate_setup",
    description="Activate a CAM setup and fit the view for view_screenshot.",
    input_param_name="setup",
    input_param_description="Setup name (from cam_get).",
).strict_schema()
activate_setup_item = Item.create_tool_item(
    tool=_activate_tool, write="write", handler=activate_setup_handler, run_on_main_thread=True,
    # isActive is re-read after activate() and anything but True is an error; the view fit is the
    # second effect and reports its own outcome (view_fit / fit_error) rather than being claimed.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_cam_activate_setup.py"
                      "::test_activate_that_does_not_take_is_an_error",
        rung="value")
)


def register_tool():
    register(activate_setup_item)
