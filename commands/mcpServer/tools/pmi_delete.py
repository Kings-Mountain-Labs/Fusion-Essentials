# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block that DELETES one PMI annotation by name. DESTRUCTIVE. deleteMe()'s bool is
gated and the name is re-walked afterwards - a decline or a survivor is an error, never a false ok.
Absence is proved by a COMPLETE walk finding no hit or by the object's own isValid reading false;
with neither, the delete is reported unverified rather than confirmed."""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from . import _pmi

app = adsk.core.Application.get()


def handler(annotation="", component="") -> dict:
    """See TOOL_DESCRIPTION."""
    d = _common.design()
    if not d:
        return error("No active design. Create or open a document first (see doc_new).")
    ann, comp, ferr = _pmi.find_annotation(d, annotation, component)
    if ferr:
        return error(ferr)
    name = safe(lambda: ann.name)
    kind = _pmi.kind_of(ann)
    comp_name = safe(lambda: comp.name)
    if not safe(lambda: ann.isDeletable, True):
        return error(f"'{name}' ({kind}) reports isDeletable=false - the platform refuses to "
                     "delete it (e.g. PMI owned by an imported folder). Nothing was changed.")
    try:
        deleted = bool(ann.deleteMe())
    except Exception as e:
        return error(f"deleteMe() failed: {e}")
    if not deleted:
        return error(f"deleteMe() declined for '{name}' ({kind}) - the annotation was NOT deleted.")
    # The hit COUNT, not find_annotation's None: that resolver answers None for a miss AND for an
    # ambiguity. read_flag, not safe(read, False): an isValid that will not read is unknown, and
    # coercing it to False manufactures the very proof this check looks for.
    holes = {}
    hits, _available = _pmi.annotation_hits(d, name or "", comp_name or "", stats=holes)
    still_valid = _common.read_flag(lambda: ann.isValid)
    if hits:
        return error(f"deleteMe() reported success but '{name}' still resolves in "
                     f"'{comp_name}' ({len(hits)} match) - treat the delete as failed.")
    if still_valid:
        return error(f"deleteMe() reported success and '{name}' no longer resolves, but the "
                     "annotation object still reports isValid - treat the delete as failed.")
    comps_bad = holes.get("components_unreadable", 0)
    items_bad = holes.get("items_unreadable", 0)
    # Two INDEPENDENT proofs of absence, either settling it: a COMPLETE walk for a KNOWN name
    # finding no hit, or isValid reading false. With neither, the delete is reported unverified.
    walk_proves = bool(name) and not (comps_bad or items_bad)
    if not walk_proves and still_valid is not False:
        why = ("the annotation's name did not read, so the re-walk had nothing to look for"
               if not name else
               f"{comps_bad} component(s) and {items_bad} annotation(s) could not be read during "
               "the re-walk")
        return error(
            f"deleteMe() reported success for '{name}' and nothing contradicts it, but the check is "
            f"INCOMPLETE: {why}, and the annotation's own isValid did not read either. A walk that "
            "could not read everything is not a walk that found nothing. Nothing was rolled back - "
            "re-run pmi_get to see what is actually there.")
    remaining = sum(1 for _ in _pmi.walk_annotations(d))
    out = {
        "deleted": name,
        "kind": kind,
        "component": comp_name,
        "remaining_pmi": remaining,
    }
    if comps_bad or items_bad:
        out["components_unreadable"] = comps_bad
        out["items_unreadable"] = items_bad
        out["note"] = (
            f"The delete is confirmed by the annotation object itself (isValid={still_valid}). The "
            f"PMI re-walk could not read {comps_bad} component(s) and {items_bad} annotation(s), "
            "so 'remaining_pmi' counts only what was reachable.")
    return ok(out)


TOOL_DESCRIPTION = (
"Delete ONE PMI annotation by its name from pmi_get. Imported PMI is not re-creatable here."
)

tool = (
    Tool.create_simple(name="pmi_delete", description=TOOL_DESCRIPTION)
    .add_input_property("annotation", {"type": "string",
        "description": "A PMI name from pmi_get."})
    .add_input_property("component", {"type": "string"})
    .add_required_input("annotation")
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_pmi_delete.py::TestDelete"
                      "::test_a_survivor_after_success_is_an_error"))


def register_tool():
    register(item)
