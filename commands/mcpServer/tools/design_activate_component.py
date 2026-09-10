# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Make an existing component the active edit target, or return to the root component. WRITES."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _inputs

_OCCURRENCE = _inputs.OccurrenceRef("occurrence",
        description="'' or 'root' is the root.")

# A read that DECLINED, kept apart from the None the design answers at the root: safe() collapses
# both to None, and only one of them confirms anything.
_UNREAD = object()


def handler(occurrence: str = "") -> dict:
    """Make an EXISTING component the active edit target, or return to the root component with ''
    (or 'root'). WRITES (UI edit target)."""
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    want = (occurrence or "").strip()

    # Return to root: activating the root clears any occurrence edit target. Occurrence carries no
    # deactivate() - activate() and isActive are its whole activation surface - so the root is
    # reached through the design, and the design's own two reads are what the write is judged by.
    if want == "" or want.lower() == "root":
        did = bool(safe(lambda: design.activateRootComponent(), False))
        if not did:
            return error("Design.activateRootComponent() returned false - the edit target did not "
                         "return to the root component.")
        at_root = safe(lambda: design.isRootComponentActive)
        still = safe(lambda: design.activeOccurrence, _UNREAD)
        # An unreadable read-back confirms nothing, so anything but a True here refuses.
        if at_root is not True or still is not None:
            held = ("unread" if still is _UNREAD
                    else safe(lambda: still.fullPathName) if still is not None else None)
            return error("activateRootComponent() returned true but the design reads "
                         f"isRootComponentActive={at_root} and activeOccurrence={held!r} - "
                         "the edit target is not confirmed at the root.")
        return ok({
        "activated": "root",
        "active_component": safe(lambda: design.activeComponent.name),
        "is_root_component_active": at_root,
        # the None the DESIGN answered (a read that declined never reaches here), never a literal
        "active_occurrence": None if still is None else safe(lambda: still.fullPathName),
        "note": "Root component is the active edit target - new geometry builds at the root.",
        })

    occ, occ_err = _OCCURRENCE.resolve(want)
    if occ_err:
        return error(occ_err)

    did = bool(safe(lambda: occ.activate(), False))
    if not did:
        return error(f"Occurrence.activate() returned false for '{occurrence}' - could not make it the "
                     "active edit target.")
    # Two occurrences of ONE component read the same activeComponent.name, so the component name
    # cannot tell them apart - the INSTANCE the design reports is what confirms this activation.
    active = safe(lambda: design.activeOccurrence)
    now_path = safe(lambda: active.fullPathName) if active is not None else None
    want_path = safe(lambda: occ.fullPathName)
    at_root = safe(lambda: design.isRootComponentActive)
    if at_root is not False or now_path is None or now_path != want_path:
        return error(f"activate() returned true for '{occurrence}' but the design reads "
                     f"activeOccurrence={now_path!r} and isRootComponentActive={at_root} - the "
                     "activation is not confirmed on that instance.")
    return ok({
    "activated": safe(lambda: occ.name),
    "component": safe(lambda: occ.component.name),
    "active_occurrence": now_path,
    "active_component": safe(lambda: design.activeComponent.name),
    "note": ("This component is now the active edit target - sketch_create / model_extrude / "
            "sketch_dimension build into it. Activate 'root' (or '') to return to the root."),
    })


TOOL_DESCRIPTION = ("Make an existing component the active edit target - new features build "
            "into it.")

tool = (
    Tool.create_simple(
        name="design_activate_component",
        description=TOOL_DESCRIPTION)
    .add_input_property(*_OCCURRENCE.as_property())
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler,
    run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_design_activate_component.py::TestActivateComponent"
                      "::test_a_sibling_instance_that_did_not_take_is_refused"))


def register_tool():
    register(item)
