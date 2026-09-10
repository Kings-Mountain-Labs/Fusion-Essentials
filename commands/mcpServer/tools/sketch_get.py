# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: read sketches by zoom level - a summary list of every sketch in the design,
or ONE sketch's overview / full X-ray through the _sketch_detail engine. Read-only.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from ._sketch_detail import DEFERRED_NOTE, _detail_engine, _sketch_summary
from . import _common
from . import _inputs

app = adsk.core.Application.get()


def _shared_component_names(design) -> set:
    """The lower-cased component names carried by MORE THAN ONE component."""
    counts = {}
    for comp in _common.all_components(design):
        nm = (safe(lambda c=comp: c.name) or "").strip().lower()
        if nm:
            counts[nm] = counts.get(nm, 0) + 1
    return {nm for nm, n in counts.items() if n > 1}


# Rows PAGED design-wide, so a sketch-dense document cannot flood the list however its sketches are
# spread over components. max_results is the page size - the same knob and the same default shape
# design_get's tree children use.
_LIST_CAP = 50

_LIST_CAP_NOTE = ("Sketches are paged {cap} per read; 'sketch_count' is the true total. Raise "
                  "max_results for the rows past the page, narrow with component='<name or "
                  "occurrence path>', or read one sketch by sketch_get(sketch_name='<name>').")


def _list_sketches(component: str = "", max_results: int = 0) -> dict:
    """List the design's sketches, each tagged with its owning component, max_results rows per read;
    'component' narrows the list, taking the same component name / occurrence path / handle the
    by-name read scopes by. Two components can wear one NAME, so the rows they own are identical -
    top-level 'placements' carries the occurrence paths that tell them apart."""
    design = _common.design()
    if not design:
        return error("No active design (open or create a document with design geometry).")
    comps, scope_error = _detail_engine().scope_components(design, component)
    if scope_error:
        return error(scope_error)
    try:
        cap = max(1, int(max_results)) if max_results else _LIST_CAP
    except (TypeError, ValueError):
        cap = _LIST_CAP
    sketches, total = [], 0
    try:
        for comp in comps:
            comp_name = safe(lambda c=comp: c.name)
            for sk in _common.iter_collection(safe(lambda c=comp: c.sketches)):
                total += 1
                # The walk runs on PAST the page so sketch_count stays the count of what is there,
                # never the count of what fitted.
                if len(sketches) >= cap:
                    continue
                rec = _sketch_summary(sk)
                rec["component"] = comp_name
                sketches.append(rec)
    except Exception as e:
        return error(f"Could not read sketches: {e}")
    withheld = total > len(sketches)
    payload = {"sketch_count": total, "sketches": sketches}
    if withheld:
        payload["returned"] = len(sketches)
        payload["truncated"] = True
    # Only the ambiguous names THIS RESPONSE's rows carry earn the placement block.
    shared = _shared_component_names(design)
    listed = [r["component"] for r in sketches]
    ambiguous = sorted({n for n in listed if (n or "").strip().lower() in shared})
    in_answer = {(n or "").strip().lower() for n in ambiguous}
    placements = [{"path": p, "component": safe(lambda c=c: c.name)}
                  for p, c in _common.component_placements(design)
                  if (safe(lambda c=c: c.name) or "").strip().lower() in in_answer]
    if ambiguous and placements:
        payload["placements"] = placements
        payload["note"] = (
            "More than one component wears the same name here (" + ", ".join(ambiguous) + "), so a "
            "row's 'component' does not identify which one holds it, and this list does not tell "
            "those rows apart. 'placements' lists every occurrence path placing a component of one "
            "of the names just listed, and no others; passing one back as 'component' reads THAT "
            "component's sketches.")
    if withheld:
        payload["note"] = (payload.get("note", "") + " " + _LIST_CAP_NOTE.format(cap=cap)).strip()
    if any(r.get("compute_deferred") for r in sketches):
        payload["note"] = (payload.get("note", "") + " " + DEFERRED_NOTE).strip()
    return ok(payload)


def handler(sketch_name: str = "", include_entities: bool = False, units: str = "mm",
            component: str = "", max_results: int = 0) -> dict:
    """No 'sketch_name': the summary list, max_results rows per read. With one: that sketch's
    overview (or the full X-ray with include_entities=true) via the _sketch_detail engine, in
    'units' (mm). 'component' scopes BOTH shapes to one component - the answer to a sketch name two
    components share, which Fusion produces by default (it numbers sketches per component from 1)."""
    if (sketch_name or "").strip():
        return _detail_engine().handler(sketch_name=sketch_name, component=component,
                                        include_entities=include_entities, units=units)
    return _list_sketches(component, max_results)


TOOL_DESCRIPTION = (
    "Read the design's sketches, or ONE sketch's overview: counts, constrained state, and profile "
    "handles for model_extrude."
)
tool = (
    Tool.create_simple(name="sketch_get", description=TOOL_DESCRIPTION)
    .add_input_property("sketch_name", {"type": "string",
            "description": "Omit for the paged summary list."})
    .add_input_property("max_results", {"type": "integer",
            "description": f"Summary rows per read (default {_LIST_CAP})."})
    .add_input_property("component", {"type": "string",
            "description": "Component to read within: a name or occurrence fullPathName/handle."})
    .add_input_property("include_entities", {"type": "boolean",
            "description": "Adds the full entity X-ray."})
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler,
                             run_on_main_thread=True)


def register_tool():
    register(item)
