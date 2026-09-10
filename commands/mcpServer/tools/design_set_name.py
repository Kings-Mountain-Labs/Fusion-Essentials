# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: rename a body, mesh body, or component (an occurrence renames its
component). WRITES.

Fusion auto-dedupes a name a sibling already holds - assigning a taken name lands 'Name (1)'
(measured), so the name is read back after the set and the LANDED name is what the result publishes.
"""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, iter_collection, ok, safe
from . import _common
from . import _inputs
from . import _outputs

# The renameable kinds: a FACE has no name and '' (the whole design) has nothing to rename, so both
# are out of allow=. MeshBody.name is settable and reads back off a fresh meshBodies.item() fetch.
_TARGET = _inputs.TargetRef("target", allow=("body", "mesh", "occurrence", "component"))

RETURNS = [
    _outputs.ReturnsName("name", of="landed body/component",
                         consumers=["find_geometry", "design_get"]),
]


def _label(entity, kind):
    """'component X' / 'body X' / 'occurrence Sub:1+Bolt:1' - what the call addressed."""
    if kind == "occurrence":
        return "occurrence '%s'" % (safe(lambda: entity.fullPathName)
                                    or safe(lambda: entity.name) or "?")
    return "%s '%s'" % (kind, safe(lambda: entity.name) or "?")


def _instances_of(design, component):
    """Every occurrence referencing `component` - the instances whose browser name follows a
    component rename."""
    root = safe(lambda: design.rootComponent)
    if root is None:
        return []
    return list(iter_collection(safe(lambda: root.allOccurrencesByComponent(component))))


def handler(target: str = "", new_name: str = "") -> dict:
    """Rename the resolved target and publish the name Fusion actually landed. WRITES."""
    want = (new_name or "").strip()
    if not want:
        return error("'new_name' is required - a non-empty name to give the target.")

    design = _common.design()
    if not design:
        return error("No active design. Open a document first (see doc_open / doc_new).")

    resolved, terr = _TARGET.resolve(target)
    if terr:
        return error(terr)
    entity, kind = resolved

    addressed = _label(entity, kind)
    occurrence = None
    if kind == "occurrence":
        # An occurrence has no name of its own - its browser name is the COMPONENT's name plus the
        # instance number - so an occurrence target renames the component behind it, and every other
        # instance of that component follows.
        occurrence = entity
        entity = safe(lambda: occurrence.component)
        if entity is None:
            return error(f"Could not reach the component behind {addressed} to rename it.")
        kind = "component"

    # Renaming the ROOT component raises '3 : root component name cannot be changed', and that raise
    # aborts the enclosing transaction even when caught, so it must never be attempted. The check is
    # same_component, never `is`: component identity is not stable across reads.
    if kind == "component":
        is_root = _common.same_component(entity, safe(lambda: design.rootComponent))
        if is_root is True:
            return error(f"'{safe(lambda: entity.name)}' is the ROOT component and Fusion refuses to "
                         "rename it ('root component name cannot be changed') - its name IS the "
                         "document name. Rename the document instead (doc_save_as), or target a "
                         "body/sub-component.")
        if is_root is None:
            # The guard exists because the platform's refusal aborts the enclosing transaction even
            # when caught, so an unproven not-root cannot be attempted: the rename is refused here
            # instead of risking that abort.
            return error(f"Whether '{addressed}' is this design's ROOT component could not be read, "
                         "and the platform's refusal to rename the root aborts the enclosing "
                         "transaction even when it is caught - so this rename was not attempted. "
                         "Target a body or a sub-component by name, or rename the document with "
                         "doc_save_as.")

    previous = safe(lambda: entity.name)
    instances = _instances_of(design, entity) if kind == "component" else []
    read_occ = occurrence if occurrence is not None else (instances[0] if instances else None)

    if previous == want:
        return ok({
            "renamed": True,
            "changed": False,
            "kind": kind,
            "addressed": addressed,
            "previous_name": previous,
            "requested_name": want,
            "name": previous,
            "deduped": False,
            "note": f"{addressed} already holds the name '{want}' - nothing changed.",
        })

    try:
        # The MUTATION - not safe-wrapped, so a refusal raises here and is reported instead of
        # being swallowed into a false success.
        entity.name = want
    except Exception as e:
        return error(f"Could not rename {addressed} to '{want}': {e}")

    landed = safe(lambda: entity.name)
    if landed is None:
        return error(f"Renamed {addressed} to '{want}' but the name could not be read back, so the "
                     "rename is unverified. Check the browser (design_get(include=['tree'])).")
    if landed == previous:
        return error(f"The rename did not take - {addressed} still reads '{previous}' after setting "
                     f"the name to '{want}'.")

    deduped = landed != want
    note = f"Renamed: '{previous}' -> '{landed}'."
    if deduped:
        note += (f" Fusion deduped the requested name '{want}' (a sibling already holds it) - the "
                 f"name that LANDED is '{landed}'; refer to that one, not what was requested.")
    if kind == "component" and len(instances) > 1:
        note += (f" This is a component: all {len(instances)} of its instances now read the new "
                 "name.")

    result = {
        "renamed": True,
        "changed": True,
        "kind": kind,
        "addressed": addressed,
        "previous_name": previous,
        "requested_name": want,
        "name": landed,
        "deduped": deduped,
        "note": note,
    }
    if kind == "component":
        result["occurrences"] = len(instances)
        # Read one instance back: it is the evidence that the browser name (and the fullPathName
        # every occurrence-taking tool keys off) followed the component rename.
        result["occurrence_name"] = safe(lambda: read_occ.name) if read_occ is not None else None
        result["occurrence_path"] = (safe(lambda: read_occ.fullPathName)
                                     if read_occ is not None else None)
    return ok(result)


_DESC = (
"Rename a body or component; an occurrence renames its COMPONENT.\n"
+ _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="design_set_name", description=_DESC)
    .add_input_property(*_TARGET.as_property())
    .add_input_property("new_name", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_design_set_name.py::TestGuards"
                      "::test_a_silently_refused_set_is_an_error_not_a_false_ok"))


def register_tool():
    register(item)
