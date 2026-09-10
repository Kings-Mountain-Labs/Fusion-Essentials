# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: boolean combine of solid BODIES (join / cut / intersect).

  model_combine -> the Combine feature: fuse, subtract, or intersect one or more TOOL bodies into a
                   TARGET body. Optionally keep the tool bodies. WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _geom
from . import _inputs
from . import _assert

_TARGET = _inputs.BodyRef("target", required=True)
_TOOLS = _inputs.BodyRefList("tools", required=True)

app = adsk.core.Application.get()

_OPERATION_KEYS = ("join", "cut", "intersect")   # combine needs an existing target; no "new"


def _join_verdict(result_bodies, result_lumps, input_lumps, direct_no_feature,
                  before_bodies, after_bodies, tool_count, keep_tools, new_component):
    """(payload fields, warning sentence) for a JOIN that did not fuse - ({}, "") when nothing read
    says it failed."""
    # A kept tool body rides in the feature's own result set, so more than one result body there is
    # not a failed fuse.
    if len(result_bodies) > 1 and not keep_tools:
        named = ", ".join(n for n in result_bodies if n)
        return ({"disjoint_join": True, "fused": False, "result_body_count": len(result_bodies)},
                f"WARNING: this join fused NOTHING - it left {len(result_bodies)} separate bodies "
                f"({named}), which is what a join of pieces that do not touch produces.")

    if result_lumps is not None and result_lumps > 1:
        total_in = (sum(input_lumps) if all(isinstance(n, int) for n in input_lumps) else None)
        fields = {"disjoint_join": True, "input_lump_total": total_in}
        if total_in is not None:
            fields["fused"] = total_in != result_lumps
        return (fields,
                f"WARNING: this join produced a {result_lumps}-lump body - {result_lumps} pieces "
                "that do not touch each other"
                + (f" (nothing fused: the result holds the same {total_in} lumps the inputs did)"
                   if total_in == result_lumps else "") + ".")

    if (direct_no_feature and not keep_tools and not new_component
            and isinstance(before_bodies, int) and isinstance(after_bodies, int)):
        survived = after_bodies - (before_bodies - tool_count)
        if survived > 0:
            return ({"disjoint_join": True, "fused": False, "unfused_tool_bodies": survived},
                    f"WARNING: {survived} of the {tool_count} tool bodies did not fuse into the "
                    "target - it is still standing after the join, so those pieces do not touch.")
    return {}, ""


def handler(target: str = "", tools=None, operation: str = "join",
            keep_tools: bool = False, new_component: bool = False) -> dict:
    """See TOOL_DESCRIPTION."""
    op_key = (operation or "join").strip().lower()
    if op_key not in _OPERATION_KEYS:
        return error(f"Unknown operation '{operation}'. Use: join, cut, intersect.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    tgt, terr = _TARGET.resolve(target)
    if terr:
        return error(terr)
    tool_bodies, lerr = _TOOLS.resolve(tools)
    if lerr:
        return error(lerr)

    # Same-body guard keyed on _common.native_identity, not Python identity: the API mints a FRESH
    # wrapper per access, so `is` reads False for two references to one body. The key needs its
    # source-document half too - an entityToken is document-local, so two x-refs answer one token.
    tgt_key = _common.native_identity(tgt)
    coll = adsk.core.ObjectCollection.create()
    for b in tool_bodies:
        b_key = _common.native_identity(b)
        if b is tgt or (tgt_key and b_key and b_key == tgt_key):
            return error("A tool body is the same as the target - pick distinct bodies.")
        coll.add(b)
    if coll.count == 0:
        return error("No valid tool bodies resolved.")

    # Captured BEFORE the mutation: the census host off the TARGET (a combine whose bodies both sit
    # in a sub-component moves nothing the active component can see), BOTH signals (a cut that
    # severs a bar holds the body count flat while the volume drops), and the tool NAMES (consumed).
    host = _common.census_host(tgt, comp)
    before_bodies = _common.body_count(host)
    before_volume = _common.measured(lambda: tgt.volume)
    # A join of bodies that touch collapses lumps, so a result still carrying the inputs' lump total
    # fused nothing - which neither the body count nor the volume can see.
    input_lumps = [_geom.lump_count(b) for b in [tgt] + list(tool_bodies)]
    target_name = safe(lambda: tgt.name)
    tool_names = [safe(lambda b=b: b.name) for b in tool_bodies]
    host_name = safe(lambda: host.name)
    # Per-tool AABB gap, unreadable after the mutation (a cut/intersect consumes its tools). Only
    # the no-effect refusal below reads it: a positive gap proves that tool never touched the target.
    tool_gaps = [_geom.aabb_gap(tgt, b) for b in tool_bodies]

    try:
        ci = comp.features.combineFeatures.createInput(tgt, coll)
        ci.operation = getattr(adsk.fusion.FeatureOperations, _common.OPERATIONS[op_key])
        # set_verified because a SWIG proxy accepts an assignment it then ignores, and neither flag
        # leaves a trace in the result - a swallowed isKeepToolBodies consumes bodies silently.
        for prop, value, label in (("isKeepToolBodies", bool(keep_tools), "keep_tools"),
                                   ("isNewComponent", bool(new_component), "new_component")):
            seterr = _common.set_verified(ci, prop, value, label, "CombineFeatureInput")
            if seterr:
                return error(f"{seterr} Nothing was combined.")
        feature = comp.features.combineFeatures.add(ci)
    except Exception as e:
        return error(f"Combine failed: {e}. (Bodies must overlap for cut/intersect; all bodies "
    "must be solids in the same component.)")
    # combineFeatures.add returns None in a DIRECT design while the boolean lands, so the census
    # below is the verdict there; in parametric a None feature stays an honest error.
    direct_no_feature = _common.direct_feature_absence(design, feature)
    if not feature and not direct_no_feature:
        return error(_common.no_feature_error(design, "Combine"))

    after_bodies = _common.body_count(host)
    if direct_no_feature:
        after_volume = _common.measured(lambda: tgt.volume)
        counted = isinstance(before_bodies, int) and isinstance(after_bodies, int)
        volumed = before_volume is not None and after_volume is not None
        if not counted and not volumed:
            return error("Combine ran in a DIRECT design, which returns no feature object, and "
                         f"neither '{host_name}' body count nor the target's volume could be read "
                         "back - so whether the bodies were combined is UNVERIFIED. Check with "
                         "design_get(include=['tree']) / model_inspect.")
        if not (counted and after_bodies != before_bodies) and not (volumed and after_volume != before_volume):
            seen = [f"'{host_name}' still holds {before_bodies} bodies" if counted
                    else f"'{host_name}' body count could not be read",
                    f"'{target_name}' measures the same volume ({after_volume} cm3)" if volumed
                    else f"'{target_name}' volume could not be read"]
            return error("Combine reported no error but nothing it could measure changed - "
                         + ", and ".join(seen) + ". For cut/intersect the bodies must overlap; "
                         "confirm with design_get(include=['tree']) / model_inspect. "
                         + _common.failed_effect_remedy(design, feature))

    # body-split: CombineFeature.bodies holds what this feature modified/created and the tools were
    # consumed, so for a cut/intersect more than one result body means the target came apart. Direct
    # mode has no feature to read it off, so the check is skipped and the note says so.
    result_objs = [] if direct_no_feature else _common.result_bodies(feature)
    result_bodies = [f["name"] for f in _common.body_facts(result_objs)]
    # With keep_tools the feature's result bodies include the kept tool copies - a kept disjoint tool
    # counts as a split piece - so kept tool NAMES are excluded before the >1 verdict.
    split_pieces = ([n for n in result_bodies if n not in set(filter(None, tool_names))]
                    if keep_tools else result_bodies)

    # cut/intersect no-effect gate, both modes: the API reports success on a DISJOINT tool, volume
    # unchanged and tool consumed. A split target cannot be a no-op, so the gate runs only on a
    # single-piece result; an unreadable volume skips it.
    if op_key in ("cut", "intersect") and len(split_pieces) <= 1:
        after_volume_now = _common.measured(lambda: tgt.volume)
        if (before_volume is not None and after_volume_now is not None
                and abs(after_volume_now - before_volume) <= _common.NO_VOLUME_CHANGE_CM3):
            clear = [f"'{n}' ({g:.1f} cm clear of the target)" if g is not None and g > 0 else f"'{n}'"
                     for n, g in zip(tool_names, tool_gaps)]
            rolled_back = False if feature is None else bool(safe(lambda: feature.deleteMe(), False))
            fate = ("the combine feature was rolled back, restoring the tool bodies" if rolled_back
                    else ("the tool bodies were CONSUMED and could not be restored"
                          if not keep_tools else "the tool bodies were kept"))
            return error(f"This {op_key} changed NOTHING - '{target_name}' measures the same volume "
                         f"({before_volume} cm3) after the combine, which is what a tool that does "
                         f"not overlap the target produces. Tools: {', '.join(clear)}; {fate}. "
                         "Move the tool into the target (model_move) and combine again.")

    # The body a JOIN landed in: the feature's single result body, or the target in direct mode. Its
    # lumps read FRESH off that body - an input reference can go invalid once the feature rebuilds.
    joined = result_objs[0] if len(result_objs) == 1 else (tgt if direct_no_feature else None)
    result_lumps = _geom.lump_count(joined) if joined is not None else None

    payload = {
        "combined": True,
        "operation": op_key,
        "target": target_name,
        "tools": tool_names,
        "kept_tools": bool(keep_tools),
        "new_component": bool(new_component),
        "bodies_remaining": after_bodies,
        "note": "Bodies combined. Pair with view_screenshot to view the result.",
    }
    # Direct mode has no feature object and no feature.bodies - flag the omission, never imply one.
    if direct_no_feature:
        payload["no_timeline_feature"] = True
        payload["note"] += (" " + _common.DIRECT_FEATURE_NOTE + " A cut/intersect that DISCONNECTED "
                            "the target cannot be detected here (that check reads the feature's own "
                            "result bodies) - check with design_get(include=['tree']).")
    else:
        payload["feature"] = safe(lambda: feature.name)
    if result_lumps is not None:
        payload["lump_count"] = result_lumps
    if op_key == "join":
        fields, warning = _join_verdict(
            result_bodies, result_lumps, input_lumps, direct_no_feature,
            before_bodies, after_bodies, len(tool_bodies), keep_tools, new_component)
        payload.update(fields)
        if warning:
            payload["note"] += (" " + warning + " " + _common.failed_effect_remedy(design, feature)
                                + " Move the pieces into contact (model_move) and join again.")
    if op_key in ("cut", "intersect") and len(split_pieces) > 1:
        payload["body_split"] = split_pieces
        payload["note"] += (f" WARNING: this {op_key} DISCONNECTED the target into {len(split_pieces)} "
                            f"separate bodies ({', '.join(n for n in split_pieces if n)}) - reference "
                            "each piece by name; a later op assuming one body may hit the wrong piece.")
    return ok(payload)


TOOL_DESCRIPTION = (
"Boolean-combine solid bodies: join, cut or intersect 'tools' into 'target', the body that survives."
)

combine_tool = (
    Tool.create_simple(name="model_combine", description=TOOL_DESCRIPTION)
    .add_input_property("target", _TARGET.schema())
    .add_input_property("tools", _TOOLS.schema())
    .add_input_property(*_inputs.boolean_op(options=("join", "cut", "intersect"), default="join").as_property())
    .add_input_property("keep_tools", {"type": "boolean",
            "description": "False consumes the tool bodies."})
    .add_input_property("new_component", {"type": "boolean"})
    .strict_schema()
)
combine_item = Item.create_tool_item(tool=combine_tool, write="write", handler=handler, run_on_main_thread=True,
                                     postconditions=[_assert.FeatureHealthy()],
                                     verification=Verification(
                                         kind="inline", rung="geometry",
                                         evidence_test="tests/unit/test_model_combine.py"
                                         "::TestDirectModeNoFeature"
                                         "::test_direct_none_with_nothing_changed_is_an_error"))


def register_tool():
    register(combine_item)
