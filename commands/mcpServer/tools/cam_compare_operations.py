# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""cam_compare_operations - diff two operations' CAM parameters (a relational read over two named ops,
not a domain disclosure, so it stays its own tool rather than a cam_get slice)."""

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
import adsk.cam

from ._common import CM_TO_UNIT, iter_collection, ok, error, safe
from ._cam_common import (STRATEGY_PAIR_NOTE, clamp_rows, get_cam, op_settled, op_state_facts,
                          resolve_cam_node, strategy_pair)
from . import _inputs
# The selection parameter tables and the loop/side vocabularies themselves, from the one place
# cam_select_geometry routes and spells them.
from .cam_select_geometry import (_CURVE_PARAM_CANDIDATES, _DIRECT_PARAM, _LOOP_TYPE, _SIDE_TYPE,
                                  _SURFACE_TARGET_PARAM)


_DIFFERENCES_CAP = 200   # two operations can differ across hundreds of CAM parameters; bound the rows
_DIFFERENCES_CEILING = 400   # every row crosses the wire; a caller cannot lift the cap past this

# Every OWN property of the curve-selection subclasses on this build, read through safe(): a class
# not carrying one omits it, so a row states what THIS selection answered. The base's error/warning
# channels are left out - they report the last apply, not what is selected.
_SELECTION_PROPS = ("isOpen", "isOpenAllowed", "isReverted",
                    "extensionType", "extensionMethod",
                    "startExtensionLength", "endExtensionLength",
                    "loopType", "sideType", "isSelectingSamePlaneFaces",
                    "isSetupModelSelected", "silhouetteTolerance",
                    "areHolesIncluded", "minimumHoleDiameter", "minimumCornerRadius",
                    "maximumCornerRadius", "minimumPocketDepth", "maximumPocketDepth")


def _spellings(*pairs):
    """{enum value: spelling} over the pairs this build's enum actually answers - a member it does
    not carry drops out, and a value with no spelling publishes as the raw int."""
    return {value: name for value, name in pairs if value is not None}


# The subset carrying a LENGTH: Fusion answers these in internal cm, so each is scaled into 'units'
# and the row states which unit it is in.
_LENGTH_PROPS = frozenset({"startExtensionLength", "endExtensionLength", "silhouetteTolerance",
                           "minimumHoleDiameter", "minimumCornerRadius", "maximumCornerRadius",
                           "minimumPocketDepth", "maximumPocketDepth"})

# A chain's extension capping and its method - the two enums that make one 2D contour differ from
# another on the SAME chain. Each member is named literally so the harness's enum scan reaches the
# family and live_api_facts pins its int members.
_EXTENSION_TYPE = _spellings(
    (safe(lambda: adsk.cam.ExtensionTypes.BoundaryExtensionType), "boundary"),
    (safe(lambda: adsk.cam.ExtensionTypes.DistanceExtensionType), "distance"))
_EXTENSION_METHOD = _spellings(
    (safe(lambda: adsk.cam.ExtensionMethods.TangentExtensionMethod), "tangent"),
    (safe(lambda: adsk.cam.ExtensionMethods.ClosestBoundaryExtensionMethod), "closest_boundary"),
    (safe(lambda: adsk.cam.ExtensionMethods.ParallelExtensionMethod), "parallel"))

# The object-set parameters: the direct family's own spellings plus every surface role.
_OBJECT_SET_PARAMS = tuple(dict.fromkeys(
    [nm for names in _DIRECT_PARAM.values() for nm in names] + list(_SURFACE_TARGET_PARAM.values())))

# The enum-valued properties, decoded so a row states a spelling rather than an ordinal.
_ENUM_PROPS = ("loopType", "sideType", "extensionType", "extensionMethod")

_SELECTION_ROWS_CAP = 20   # one row per selection; a chain-per-contour op grows this with the model

# Said only on a 0/0 answer where geometry WAS read - the one a caller reads as 'these are the same'.
_NOTHING_DIFFERED_NOTE = (
    " 0 differences is what these reads OPENED matching: the parameter expressions, each selection "
    "set's counts, and the per-selection properties in geometry_properties_read. WHICH edges, "
    "faces or bodies those counts are OF is not read here, and a property no selection answered is "
    "not compared - open both operations in Fusion to compare the picks themselves.")

# The other 0/0: no selection parameter on either operation answered, so the geometry counts are 0
# because nothing was read, not because the two match.
_NO_GEOMETRY_NOTE = (
    " NO selection set answered on either operation - neither carries one this read reaches, or the "
    "reads did not answer. So nothing here compares what the two CUT, whatever the parameter rows "
    "say; the geometry counts are absent evidence, not agreement.")


# The refusal while either named operation is still generating. The geometry half below reads the
# selection objects off both operations, and one such compare on a document whose operations read
# isGenerating true ended the Fusion process.
_GENERATING_REFUSAL = (
    "{n} of the 2 named operations still {verb} generating to do ({names}), so nothing was "
    "compared. This read walks the selection objects on both operations, and one such call on a "
    "document still regenerating ended the Fusion process. Poll cam_get_status until "
    "completed=true, then compare.")


def _generating(pairs):
    """The names of the named operations with generating LEFT to do, in the order they were named.
    op_settled is the judge cam_get_status settles completed on, so the refusal's remedy is
    reachable: the isGenerating flag alone stays true over an operation whose state already
    answered."""
    rows = []
    for op, name in pairs:
        facts = op_state_facts(op)
        if facts.get("is_generating") and not op_settled(facts):
            rows.append(name)
    return rows


def _reverse_enum(family, table):
    """{enum value: the key cam_select_geometry spells it by} over `family`, whose members that tool
    already names literally - a value with no key publishes as the raw int."""
    return _spellings(*((safe(lambda m=member: getattr(family, m)), key)
                        for key, member in table.items()))


def _enum_spellings(prop):
    """{enum value: spelling} for one enum-valued selection property, or {} where none decodes."""
    if prop == "loopType":
        return _reverse_enum(adsk.cam.LoopTypes, _LOOP_TYPE)
    if prop == "sideType":
        return _reverse_enum(adsk.cam.SideTypes, _SIDE_TYPE)
    return _EXTENSION_TYPE if prop == "extensionType" else _EXTENSION_METHOD


def handler(operation_a: str = "", operation_b: str = "",
                                max_results: int = _DIFFERENCES_CAP, units: str = "mm") -> dict:
    """Diff the CAM parameters of two operations (by name) to show what differs."""
    if not (operation_a or "").strip() or not (operation_b or "").strip():
        return error("Provide both 'operation_a' and 'operation_b' (operation names).")
    units_key = (units or "mm").strip().lower()
    inv = CM_TO_UNIT.get(units_key)      # cm -> the caller's units, for every selection LENGTH
    if inv is None:
        return error(f"Unknown units '{units}'. Valid: {', '.join(sorted(CM_TO_UNIT))}.")
    cam, err = get_cam()
    if err:
        return error(err)

    node_a, err_a = resolve_cam_node(cam, operation_a, kinds=("operation",), label="operation")
    if err_a:
        return error(err_a)
    node_b, err_b = resolve_cam_node(cam, operation_b, kinds=("operation",), label="operation")
    if err_b:
        return error(err_b)
    op_a, op_b = node_a.obj, node_b.obj

    busy = _generating(((op_a, safe(lambda: op_a.name) or operation_a),
                        (op_b, safe(lambda: op_b.name) or operation_b)))
    if busy:
        return error(_GENERATING_REFUSAL.format(n=len(busy), names=", ".join(busy),
                                                verb="has" if len(busy) == 1 else "have"))

    params_a, titles_a = _operation_params(op_a)
    params_b, titles_b = _operation_params(op_b)

    all_keys = sorted(set(params_a) | set(params_b))
    differences = []
    same_count = 0
    for k in all_keys:
        a = params_a.get(k)
        b = params_b.get(k)
        if a == b:
            same_count += 1
        else:
            differences.append({"parameter": k,
        "title": titles_a.get(k) or titles_b.get(k) or k,
        "operation_a": a if k in params_a else "(not present)",
        "operation_b": b if k in params_b else "(not present)"})

    total = len(differences)
    cap = clamp_rows(max_results, _DIFFERENCES_CAP, _DIFFERENCES_CEILING)
    differences_out = differences[:cap]
    truncated = total > len(differences_out)

    # The GEOMETRY half: two ops can carry identical parameters and cut different material, because
    # what is selected lives on the selection objects, not in the parameter expressions.
    geom_a, geom_b = _geometry_facts(op_a, inv), _geometry_facts(op_b, inv)
    geometry_differences = []
    geometry_same = 0
    for k in sorted(set(geom_a) | set(geom_b)):
        a, b = geom_a.get(k), geom_b.get(k)
        if a == b:
            geometry_same += 1
        else:
            geometry_differences.append({"parameter": k,
                                         "operation_a": a if k in geom_a else "(not present)",
                                         "operation_b": b if k in geom_b else "(not present)"})

    pair_a, pair_b = strategy_pair(op_a), strategy_pair(op_b)
    out = {
        "operation_a": safe(lambda: op_a.name),
        "operation_b": safe(lambda: op_b.name),
    "tool_a": _op_tool_desc(op_a),
    "tool_b": _op_tool_desc(op_b),
    # Both vocabularies per side: the 'strategy' difference row below carries the internal id, which
    # is not a name cam_create_operation takes.
    "strategy_a": pair_a["strategy"], "strategy_name_a": pair_a["strategy_name"],
    "strategy_b": pair_b["strategy"], "strategy_name_b": pair_b["strategy_name"],
    "same_parameter_count": same_count,
    "difference_count": total,
    "differences": differences_out,
    "truncated": truncated,
    "same_geometry_count": geometry_same,
    "geometry_difference_count": len(geometry_differences),
    "geometry_differences": geometry_differences,
    "note": STRATEGY_PAIR_NOTE,
    }
    # Names the unit every selection LENGTH above is scaled into; the counts carry no unit.
    if geom_a or geom_b:
        out["geometry_units"] = units_key
    if truncated:
        out["note"] += (f" differences was capped at {cap} of {total}; raise max_results to see "
                        "the rest.")
    # No set answering at all is disclosed WHATEVER the parameter diff said: two ops differing only
    # in feed still cut different material through geometry this read never opened.
    if not (geom_a or geom_b):
        out["note"] += _NO_GEOMETRY_NOTE
    elif not total and not geometry_differences:
        # The other zero: the sets were read and MATCHED.
        out["geometry_properties_read"] = list(_SELECTION_PROPS)
        out["note"] += _NOTHING_DIFFERED_NOTE
    return ok(out)


def _operation_params(op):
    """({name: expression}, {name: title}) for one operation's CAM parameters - keyed by NAME,
    which is scope-unique on an op where a parameter TITLE is not."""
    values, titles = {}, {}
    try:
        params = op.parameters
        for p in iter_collection(params):
            name = safe(lambda: p.name)
            if not name:
                continue
            values[name] = safe(lambda: p.expression)
            titles[name] = safe(lambda: p.title) or name
    except Exception:
        pass
    return values, titles


def _selection_row(sel, inv):
    """ONE selection's answered properties: a LENGTH scaled out of internal cm by `inv`, and an enum
    decoded to its member spelling - a value this build's enum does not answer stays the raw int."""
    row = {}
    for prop in _SELECTION_PROPS:
        answered = safe(lambda prop=prop, sel=sel: getattr(sel, prop))
        if answered is None:
            continue
        if prop in _LENGTH_PROPS:
            row[prop] = round(float(answered) * inv, 6)
        elif prop in _ENUM_PROPS:
            row[prop] = _enum_spellings(prop).get(answered, answered)
        else:
            row[prop] = answered
    return row


def _curve_facts(p, inv):
    """What ONE curve-selection parameter holds: the counts cam_select_geometry reads back off an
    applied selection, plus each selection's own answered properties. None when it does not read."""
    pv = safe(lambda: p.value)
    cs = safe(lambda: pv.getCurveSelections()) if pv is not None else None
    if cs is None:
        return None
    count = safe(lambda: cs.count, 0) or 0
    rows, paths, segments, entities = [], 0, 0, 0
    for i in range(count):
        sel = safe(lambda i=i: cs.item(i))
        if sel is None:
            continue
        # outputGeometry is a Curve3DPathVector and value a BaseVector - plain iterables with no
        # count/item, so they are listed rather than walked by iter_collection.
        out = safe(lambda sel=sel: list(sel.outputGeometry))
        if out is not None:
            paths += len(out)
            segments += sum((safe(lambda q=q: q.count, 0) or 0) for q in out)
        value = safe(lambda sel=sel: list(sel.value))
        if value is not None:
            entities += len(value)
        if len(rows) < _SELECTION_ROWS_CAP:
            rows.append(_selection_row(sel, inv))
    facts = {"selections": count, "curve_paths": paths, "curve_segments": segments,
             "entities": entities, "properties": rows}
    if count > len(rows):
        facts["properties_truncated"] = True   # absent = every selection's row is here
    return facts


def _object_set_facts(p, _inv):
    """What ONE direct/surface set parameter holds: how many CAD objects, or None where the list
    did not read."""
    pv = safe(lambda: p.value)
    value = safe(lambda: list(pv.value)) if pv is not None else None
    return None if value is None else {"entities": len(value)}


def _geometry_facts(op, inv):
    """{parameter name: what that selection parameter holds} for every geometry-selection parameter
    this operation carries - read through the same properties cam_select_geometry writes."""
    facts = {}
    for names, reader in ((_CURVE_PARAM_CANDIDATES, _curve_facts),
                          (_OBJECT_SET_PARAMS, _object_set_facts)):
        for nm in names:
            p = safe(lambda nm=nm: op.parameters.itemByName(nm))
            if p is None:
                continue
            record = reader(p, inv)
            if record is not None:
                facts[nm] = record
    return facts


def _op_tool_desc(op):
    try:
        t = op.tool
        return t.description if t else None
    except Exception:
        return None


TOOL_DESCRIPTION = (
    "Compare two CAM operations by name: which parameters and which geometry selections differ, "
    "with the value on each side."
)

tool = (
    Tool.create_with_string_input(
        name="cam_compare_operations",
        description=TOOL_DESCRIPTION,
        input_param_name="operation_a",
        input_param_description="Operation name (from cam_get).",
    )
    .add_input_property("operation_b", {"type": "string",
            "description": "Operation name (from cam_get)."})
    .add_input_property("max_results", {"type": "integer", "description":
            f"Max {_DIFFERENCES_CEILING}."})
    .add_input_property(*_inputs.UNITS.as_property())
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
