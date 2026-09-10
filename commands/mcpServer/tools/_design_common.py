# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""DESIGN MODE awareness: the mode read design_get's 'mode' slice returns, and the base-feature
scope runner every mutation that needs one goes through. Many adsk.* mutation methods are valid in
only ONE of Fusion's two design modes, or only inside an OPEN base-feature edit scope."""

from ._common import ok, error, safe
from . import _common
from . import _inputs
from ._common import timeline_health as _timeline_health

MAP_BLURB = (
    "DESIGN MODE: get_mode_handler - the designType + capability can{} map design_get's 'mode' "
    "slice returns, derived from the mode so it agrees with ModeGuards; health_handler - the "
    "timeline error/warning rollup of its default slice; run_in_base_feature - a mutation "
    "needing a base-feature scope in a PARAMETRIC design, run direct in a DIRECT one; "
    "base_feature_run_wrapper - its always-finish wrapper")


def health_handler() -> dict:
    """The active design's timeline health: feature error/warning rollup + a healthy flag."""
    design = _common.design()
    if not design:
        return error("No active design.")
    errors, warnings, total = _timeline_health(design)
    return ok({"timeline_features": total, "error_count": len(errors),
        "warning_count": len(warnings), "errors": errors, "warnings": warnings,
        "healthy": len(errors) == 0})


# ── shared mode reads (all via the ONE true reader) ─────────────────────────

def _timeline_feature_count(design):
    """The parametric timeline's feature count, or None where there is no timeline (design.timeline
    raises in a direct design, which reads as None rather than as a broken timeline)."""
    tl = safe(lambda: design.timeline)
    if tl is None:
        return None
    return safe(lambda: tl.count, 0)


def _base_feature_count(design):
    """Count base features across the design (0 in a direct design, which has none)."""
    root = safe(lambda: design.rootComponent)
    if root is None:
        return 0
    total = 0
    counted_any = False
    for comp in _common.all_components(design):
        if comp is None:
            continue
        bf = safe(lambda c=comp: c.features.baseFeatures)
        if bf is None:
            continue
        counted_any = True
        total += safe(lambda b=bf: b.count, 0)
    return total if counted_any else 0


def _capability_map(mode):
    """The actionable `can{}` payload, derived purely from `mode` so it agrees with the ModeGuards."""
    parametric = mode == _inputs.MODE_PARAMETRIC
    direct = mode == _inputs.MODE_DIRECT
    return {
    "construction_point_by_coordinate": direct,   # setByPoint(Point3D) - direct-only
    "construction_axis_by_line": direct,          # setByLine(InfiniteLine3D) - direct-only
    "construction_plane_by_offset": parametric or direct,  # setByOffset - valid in both
    "timeline_ops": parametric,                   # a timeline exists only in parametric
    "base_feature_scope": parametric,             # base features are a parametric-only scope
    "convert_to_direct": parametric,              # parametric -> direct (destructive)
    "convert_to_parametric": direct,              # direct -> parametric
    }


# ── modelling-mode read (get_mode_handler - design_get's mode slice) ──────────────

def get_mode_handler() -> dict:
    """Report the active design's modelling mode and its capability map. Read-only."""
    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    mode = _inputs.current_design_type(design)
    tl_count = _timeline_feature_count(design)
    return ok({
        "design_type": mode,
        "has_timeline": tl_count is not None,
        "timeline_feature_count": tl_count,
    "base_feature_count": _base_feature_count(design),
    "in_base_feature_edit": _inputs._in_base_feature_scope(design),
    "can": _capability_map(mode),
    "note": ("Capability map is keyed by mode requirement; call design_set_mode to convert, or "
            "model_base_feature to open a base-feature scope."),
    })


# ── the base-feature scope runner every mutation that needs one goes through ─────

def base_feature_run_wrapper(open_scope, inner_op):
    """Run inner_op inside the scope open_scope() -> (base_feature, error or None) opens, ALWAYS
    finishing in a finally: (base_feature, inner_result), or (None, that error)."""
    bf, err = open_scope()
    if err is not None:
        return None, err
    started = bf.startEdit()
    if started is False:
        return bf, error("Could not enter base-feature edit (startEdit returned false).")
    try:
        result = inner_op(bf)
    finally:
        # ALWAYS finish - a leaked open base-feature edit corrupts every later call this session -
        # and on the CAPTURED bf, since a lookup cannot find a scope while designType reads direct.
        safe(lambda: bf.finishEdit())
    return bf, result


def run_in_base_feature(design, comp, inner_op):
    """The entry point for any mutation that may need a base-feature scope: inner_op runs inside one
    in a PARAMETRIC design (receiving the open BaseFeature) and directly in a DIRECT design
    (receiving None, the valid 'no scope' argument). Returns (inner_op's result, error or None)."""
    mode = _inputs.current_design_type(design)
    if mode != _inputs.MODE_PARAMETRIC:
        # Direct (or unknown): no base-feature scope - run the op directly. inner_op gets None.
        return inner_op(None), None

    if comp is None:
        return None, error("No component to open a base-feature scope in.")

    def open_scope():
        base_features = safe(lambda: comp.features.baseFeatures)
        if base_features is None:
            return None, error("This component has no baseFeatures collection - cannot open a "
    "base-feature scope for the parametric operation.")
        bf = base_features.add()
        if not bf:
            return None, error("BaseFeatures.add() returned nothing - could not open a "
    "base-feature scope.")
        return bf, None

    _bf, result = base_feature_run_wrapper(open_scope, inner_op)
    # base_feature_run_wrapper returns the inner result as `result`; an open/startEdit failure comes
    # back as a _common.error() dict in that slot. Normalise to (result, error).
    if isinstance(result, dict) and result.get("isError") is True:
        return None, result
    return result, None
