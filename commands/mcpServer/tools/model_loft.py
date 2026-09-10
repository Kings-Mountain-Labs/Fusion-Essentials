# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: loft a body through an ORDERED list of profiles, optionally shaped by rails
or a centerline. WRITES; the result's isSolid is read back off the feature, never assumed.
"""

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _geom
from . import _inputs
from . import _assert
from . import _sketch_detail
from ._surface_common import _OPERATION_KEYS, _feature_operation, _result_body_report

app = adsk.core.Application.get()

# scope_input: a {sketch, profile_index} element addresses a sketch BY NAME, and Fusion numbers
# sketches per component from 1, so a name two components carry is refused with the remedy spelled
# as this tool's own 'component' input.
_LOFT_PROFILES = _inputs.ProfileRefList("profiles", required=True, scope_input="component")
_LOFT_RAILS = _inputs.GeometryHandleList("rails", require="any", required=False)
_LOFT_CENTERLINE = _inputs.GeometryHandle("centerline", require="any", required=False)

# LoftCenterLineOrRails.addRail takes a SketchCurve (measured: addRail(SketchArc) returns a
# LoftCenterLineOrRail), but find_geometry mints handles for BRep faces/edges/vertices only - so a
# spine drawn before any body exists needs this second spelling, '<sketch>/<type>:<index>'.
_SKETCH_CURVE_SEP = "/"


def _sketch_curve_ref(raw):
    """The '<sketch>/<type>:<index>' parts of a sketch-curve ref, or None when it is not one."""
    if not isinstance(raw, str) or _SKETCH_CURVE_SEP not in raw or _inputs.is_handle(raw):
        return None
    name, _, ref = raw.strip().rpartition(_SKETCH_CURVE_SEP)
    kind = ref.rpartition(":")[0].strip().lower()
    return (name.strip(), ref.strip()) if name.strip() and kind in _common.ENTITY_REF_KINDS else None


def _resolve_sketch_curve(design, raw, label):
    """(SketchCurve, None) for a '<sketch>/<type>:<index>' ref, or (None, error)."""
    name, ref = _sketch_curve_ref(raw)
    sketch, ambiguous = _common.find_sketch(
        design, name, remedy="Name the component that owns it in 'component'.")
    if ambiguous:
        return None, f"{label} '{raw}': {ambiguous}"
    if sketch is None:
        return None, (f"{label} '{raw}' names no sketch '{name}'. Available: "
                      + (", ".join(n for n in _common.all_sketch_names(design) if n) or "(none)"))
    curve = _common.resolve_entity_ref(sketch, ref)
    if curve is None:
        return None, (f"{label} '{raw}': sketch '{name}' has no '{ref}'. "
                      "sketch_get(include_entities=true) lists its entity ids.")
    return curve, None


def _resolve_guides(design, raw, kind, label):
    """(entities, error) for rails/centerline: each item is a find_geometry handle OR a sketch-curve
    ref, resolved item by item so the two spellings can be mixed in one call."""
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    if any(_sketch_curve_ref(i) for i in items):
        out = []
        for i in items:
            if _sketch_curve_ref(i):
                ent, err = _resolve_sketch_curve(design, i, label)
            else:
                ent, err = kind.resolve(i) if not isinstance(kind, _inputs.GeometryHandleList) \
                    else _inputs.GeometryHandle.resolve(kind, i)
            if err:
                return None, err
            out.append(ent)
        return out, None
    return kind.resolve(raw)


def _cut_check_bodies(comp):
    """The solid bodies a cut/intersect loft can act on: every solid directly in the feature's host
    component, resolved ONCE before the mutation and re-read afterwards (_geom.volumes keys on
    id()). A loft takes no participant-body scoping, so there is no narrower sample."""
    return [b for b in _common.iter_collection(safe(lambda: comp.bRepBodies))
            if safe(lambda b=b: b.isSolid)]


def handler(profiles=None, rails=None, centerline="", operation="new",
            as_surface=None, is_closed=None, component: str = "") -> dict:
    """Loft a body through an ORDERED list of profiles, optionally shaped by rails OR a centerline."""
    op_key = (operation or "new").strip().lower()
    if op_key not in _OPERATION_KEYS:
        return error(f"Unknown operation '{operation}'. Use: new, join, cut, intersect.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")

    secs, perr = _LOFT_PROFILES.resolve(profiles, component)
    if perr:
        return error(perr)
    if not secs or len(secs) < 2:
        return error(f"Loft needs at least 2 profiles (got {len(secs) if secs else 0}).")

    has_rails = rails not in (None, "", [])
    has_centerline = bool((centerline or "").strip()) if isinstance(centerline, str) else centerline not in (None, [])
    if has_rails and has_centerline:
        return error("centerLineOrRails takes a centerline OR rails, not both.")

    rail_ents = []
    if has_rails:
        rail_ents, rerr = _resolve_guides(design, rails, _LOFT_RAILS, "rails")
        if rerr:
            return error(rerr)
    center_ent = None
    if has_centerline:
        center_ent, cerr = _resolve_guides(design, centerline, _LOFT_CENTERLINE, "centerline")
        if cerr:
            return error(cerr)
        if isinstance(center_ent, list):
            center_ent = center_ent[0] if center_ent else None

    # Host the loft on the profiles' OWNING component: handing another component's native profile to
    # features.createInput raises 'InternalValidationError : bSet', so the feature - and its body
    # - is built on the sketch's owner, not the active component.
    root = _inputs.profile_host_component(secs[0], None, target_component(design))
    op = _feature_operation(op_key)
    try:
        loft_input = root.features.loftFeatures.createInput(op)
    except Exception as e:
        return error(f"Could not start loft: {e}")

    # Add sections IN ORDER - this ordering is the whole game (do NOT sort/reorder).
    try:
        for sec in secs:
            loft_input.loftSections.add(sec)
    except Exception as e:
        return error(f"Could not add loft sections: {e}")

    # centerline XOR rails on the LoftCenterLineOrRails object.
    try:
        if center_ent is not None:
            loft_input.centerLineOrRails.addCenterLine(center_ent)
        else:
            for r in rail_ents:
                loft_input.centerLineOrRails.addRail(r)
    except Exception as e:
        return error(f"Could not set loft centerline/rails: {e}")

    if as_surface is not None:
        try:
            loft_input.isSolid = not bool(as_surface)
        except Exception as e:
            return error(f"Could not set loft solid/surface mode: {e}")

    # isClosed has no independent effect the result bodies show, so the set-then-read-back on the
    # input is what proves it took (a SWIG proxy accepts an unknown property name silently). Two
    # sections are enough for a closed loft - measured, so there is no >=3 guard.
    if is_closed is not None:
        cerr = _common.set_verified(loft_input, "isClosed", bool(is_closed),
                                    f"is_closed={bool(is_closed)}", "LoftFeatureInput")
        if cerr:
            return error(cerr)

    # cut/intersect MATERIAL evidence: the volumes the operation must move, sampled BEFORE the add.
    # A loft whose swept shape misses the body still reports a healthy feature, so only this
    # before/after pair can say material actually changed.
    check_bodies = _cut_check_bodies(root) if op_key in ("cut", "intersect") else []
    vol_before = _geom.volumes(check_bodies)
    # A join is told "grew a body" from "made a second one" by the host's body NAMES before the add.
    bodies_before = _common.component_body_names(root) if op_key == "join" else None

    try:
        feature = root.features.loftFeatures.add(loft_input)
    except Exception as e:
        # Lead with the API's OWN message - a cut through air says "No target body" and blaming
        # the profiles buried it (measured); the compatibility explanation is the fallback cause.
        return error(f"Loft failed: {e}. Common causes: a cut/intersect with no body in the loft's "
                     "path (the API says 'No target body' for that), or incompatible profiles (a "
                     "mix of open/closed, or a self-intersecting path - profiles must be the same "
                     "kind and orderable into a single sweep).")
    if not feature:
        return error(_common.no_feature_error(design, "Loft"))

    volume_delta_cm3 = None
    # These live outside the census branch: the empty-result gate below reads them to decide whether
    # material movement was PROVEN, and an un-run census must read as "proved nothing", not as an
    # absent variable. readable=False is exactly that case - no body's volume read at both ends.
    delta, readable, consumed = 0.0, False, []
    if check_bodies:
        delta, readable = _geom.volume_delta(check_bodies, vol_before)
        # A body whose volume read BEFORE and reads unreadable now was consumed whole - a real effect
        # that contributes no delta, so it must not be counted as "nothing moved".
        consumed = [b for b in check_bodies
                    if vol_before.get(id(b)) is not None and _geom.signed_volume(b) is None]
        if readable:
            volume_delta_cm3 = round(delta, 6)
        if readable and not consumed and abs(delta) < _common.NO_VOLUME_CHANGE_CM3:
            where = safe(lambda: root.name) or "the host component"
            return error(f"Loft reported success but this {op_key} changed nothing - every solid "
                         f"body in '{where}' measures the volume it had before and none was "
                         "consumed, so the lofted shape does not overlap any of them. Check the "
                         "profiles bracket the target body (an 'intersect' whose target lies "
                         "entirely INSIDE the lofted solid also reads this way). "
                         + _common.failed_effect_remedy(design, feature))

    body_names, _flags = _result_body_report(feature)
    # An empty result set is a failure except for a cut/intersect PROVEN to have moved material: a
    # body consumed whole, or a volume delta that READ and cleared the no-change band. A census
    # whose volumes never read buys no exemption.
    moved = bool(consumed) or (readable and abs(delta) >= _common.NO_VOLUME_CHANGE_CM3)
    if not body_names and not moved:
        return error("Loft reported success but the feature owns no result body - nothing was "
                     "built. " + _common.failed_effect_remedy(design, feature))
    # read_flag, not safe(): an unreadable isSolid is not a surface, and the note below is worded
    # off this value rather than off the falsiness of an absent read.
    is_solid = _common.read_flag(lambda: feature.isSolid)
    if is_solid is True:
        shape = "Result is a SOLID."
    elif is_solid is False:
        shape = "Result is a SURFACE - pair with model_stitch/model_thicken to close it."
    else:
        shape = ("The feature's isSolid flag could not be read back, so whether the result is a "
                 "solid or a surface is UNVERIFIED.")
    note = ("Lofted through %d profiles in order. " % len(secs)) + shape
    join_clause = _common.join_new_body_clause(op_key, bodies_before, body_names)
    if join_clause:
        note += " " + join_clause
    payload = {
        "lofted": True,
        "feature": safe(lambda: feature.name),
        "operation": op_key,
        "profiles_count": len(secs),
        "rails_count": len(rail_ents),
        "has_centerline": center_ent is not None,
        "is_solid": is_solid,
        "result_bodies": body_names,
        "note": note,
    }
    if is_solid is None:
        payload["unverified"] = ["is_solid"]
    if is_closed is not None:
        payload["is_closed"] = bool(is_closed)
    # Published only where the before/after pair was READABLE: a null here would read as "no material
    # moved" rather than "the measurement could not be taken", so the key is simply absent instead.
    if volume_delta_cm3 is not None:
        payload["volume_delta_cm3"] = volume_delta_cm3
    return ok(payload)


TOOL_DESCRIPTION = (
"Loft through an ordered list of profiles; model_stitch closes a surface loft."
)

tool = (
    Tool.create_simple(name="model_loft", description=TOOL_DESCRIPTION)
    .add_input_property("profiles", _LOFT_PROFILES.schema())
    .add_input_property("rails", _LOFT_RAILS.schema())
    .add_input_property("centerline", _LOFT_CENTERLINE.schema())
    .add_input_property(*_inputs.boolean_op(default="new").as_property())
    .add_input_property("as_surface", {"type": "boolean"})
    .add_input_property("is_closed", {"type": "boolean"})
    .add_input_property(*_sketch_detail.COMPONENT_SCOPE)
    .add_required_input("profiles")
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_assert.FeatureHealthy()],
                             verification=Verification(
                                 kind="inline", rung="geometry",
                                 evidence_test="tests/unit/test_model_loft.py::TestLoft"
                                               "::test_cut_that_moves_no_volume_is_an_error"))


def register_tool():
    register(item)
