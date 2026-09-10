# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""MCP building block: resize solid bodies about an anchor point (the Scale feature).

  model_scale -> scale one or more bodies uniformly, or per-axis, about a point that stays put -
                 fit a part to a new envelope, add a shrink/growth allowance. WRITES.
"""

import adsk.core
import adsk.fusion

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe, target_component
from . import _common
from . import _assert
from . import _geom
from . import _inputs
from . import _outputs


# Relative tolerance on the measured volume ratio - a scale is an exact linear map, so a body that
# really scaled lands on f^3 (or x*y*z) far inside this band.
_RATIO_TOL = 1e-3

# Below this volume (cm^3) a before/after ratio is dominated by read-back noise, so the body is
# judged by its bounding box instead.
_MIN_VOLUME_CM3 = 1e-9

# Bounding-box extents (cm) closer than this are the same extent.
_EXTENT_EPS_CM = 1e-7

# Two evaluations of one expression closer than this are the SAME number (the dimensioned-expression
# discriminator in _expression_value).
_UNIT_MATCH_EPS = 1e-12

_AXIS_INPUTS = ("x_factor", "y_factor", "z_factor")
_AXIS_PARAMS = {"x_factor": "xScale", "y_factor": "yScale", "z_factor": "zScale"}

RETURNS = [
    _outputs.ReturnsName("feature", of="feature", consumers=["design_delete_feature"],
                         absent_when="no_timeline_feature"),
    _outputs.ReturnsValue("volume_ratio", "the measured after/before volume change proving the scale took",
                          absent_when="volume_check_skipped"),
    _outputs.ReturnsValue("scale_check", "which evidence carried the verdict: volume_ratio or "
                          "geometry_changed"),
]

# The solid-body kind is this tool's own policy, not an API constraint - setToNonUniform accepts a
# collection holding a sketch at input level - but the volume read-back needs a closed body.
_BODIES = _inputs.BodyRefList("bodies", kind="solid", required=True)
_ANCHOR = _inputs.GeometryHandle("anchor", require="vertex", required=False,
    description="Held fixed; omit for the component origin.")

app = adsk.core.Application.get()


def _positive(label, value, source):
    """The greater-than-zero refusal for a resolved factor, or '' - `source` names where it came
    from."""
    if value <= 0:
        return (f"'{label}' must be greater than zero, but {source} is {value} - a scale factor is a "
                "positive multiplier (2 = twice the size, 0.5 = half). To reflect a body, use "
                "model_mirror.")
    return ""


def _carries_a_unit(label, expr) -> str:
    """The refusal for an expression that carries its own unit - a length or an angle alike."""
    return (f"'{label}' expression '{expr}' carries a unit - scale factors are unitless. Pass a "
            "plain number or a unitless parameter expression.")


def _expression_value(label, expr, design):
    """(number, error) for a parameter-EXPRESSION factor, evaluated through the design's units
    engine so an unresolvable or DIMENSIONED expression is refused by name before any mutation."""
    um = safe(lambda: design.fusionUnitsManager)
    if um is None:
        return None, (f"'{label}': the design's units engine is unavailable, so the expression "
                      f"'{expr}' cannot be checked. Pass a number instead.")
    try:
        unitless = um.evaluateExpression(expr, "")
    except Exception as e:
        return None, (f"'{label}' expression '{expr}' did not evaluate - use a UNITLESS expression "
                      f"like 'ShrinkFactor' or '1/0.98' and confirm the parameter names exist "
                      f"(param_get): {e}")
    if not isinstance(unitless, (int, float)) or isinstance(unitless, bool):
        return None, (f"'{label}' expression '{expr}' did not evaluate to a number. Use a unitless "
                      "expression or a plain number.")
    # The unit discriminator: a unitless PARAM raises under both "mm" and "deg"; a LENGTH one
    # evaluates the same under "" and "mm"; an ANGLE one raises under "mm" but reads its radian
    # value under "deg". So a raise under "mm" alone is not proof of unitlessness.
    try:
        as_length = um.evaluateExpression(expr, "mm")
    except Exception:
        try:
            um.evaluateExpression(expr, "deg")
        except Exception:
            return float(unitless), None            # unreadable as length AND as angle: unitless
        return None, _carries_a_unit(label, expr)
    if (isinstance(as_length, (int, float)) and abs(unitless) > _UNIT_MATCH_EPS
            and abs(unitless - as_length) <= _UNIT_MATCH_EPS):
        return None, _carries_a_unit(label, expr)
    return float(unitless), None


def _factor_value(label, raw, design):
    """(ValueInput, number, error) for one scale factor - a non-numeric string is a parameter
    EXPRESSION, gated here and applied as the number it resolved to."""
    if _inputs.looks_like_expression(raw):
        expr = raw.strip()
        value, eerr = _expression_value(label, expr, design)
        if eerr:
            return None, None, eerr
        perr = _positive(label, value, f"expression '{expr}'")
        if perr:
            return None, None, perr
        # The resolved NUMBER goes in, not the string: createByString raises '3 : invalid
        # expression' at add() for a bare parameter reference, and where it succeeds the feature's
        # scaleFactor.expression reads the evaluated literal anyway.
        return adsk.core.ValueInput.createByReal(value), value, None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None, None, (f"'{label}' must be a number or a parameter-expression string, got "
                            f"{raw!r}.")
    perr = _positive(label, v, "the value given")
    if perr:
        return None, None, perr
    return adsk.core.ValueInput.createByReal(v), v, None


def _resolved_factor(requested, feature, param_attr):
    """One factor as the NUMBER Fusion applied - the feature's own ModelParameter when readable,
    else the number the input resolved to."""
    # Per-axis mode carries xScale/yScale/zScale while scaleFactor reads None; uniform is the mirror.
    v = safe(lambda: getattr(feature, param_attr).value)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return requested


def _measure(body):
    """(volume_cm3, (dx, dy, dz)) for a body - the geometry evidence a scale has to move. Either half
    is None when the read is unavailable."""
    vol = _geom.signed_volume(body)
    bb = safe(lambda: body.boundingBox)
    mn = safe(lambda: bb.minPoint) if bb is not None else None
    mx = safe(lambda: bb.maxPoint) if bb is not None else None
    ext = None
    if mn is not None and mx is not None:
        span = [safe(lambda a=a: float(getattr(mx, a) - getattr(mn, a))) for a in ("x", "y", "z")]
        if all(isinstance(s, float) for s in span):
            ext = tuple(span)
    return vol, ext


def _moved(before, after):
    """True when one body's measured geometry changed: its volume, or any bounding-box extent."""
    (vb, eb), (va, ea) = before, after
    if vb is not None and va is not None and vb > _MIN_VOLUME_CM3 and abs(va - vb) / vb > _RATIO_TOL:
        return True
    if eb is not None and ea is not None:
        return any(abs(a - b) > _EXTENT_EPS_CM for a, b in zip(ea, eb))
    return False


def _verify(body_names, before, after, expected, remedy):
    """(error_text, evidence) holding each body's measured volume ratio to `expected` - and, where
    that expectation is 1.0 and cannot discriminate, to the geometry having moved at all."""
    ratios, total_before, total_after = [], 0.0, 0.0
    readable = 0
    for name, b, a in zip(body_names, before, after):
        if (b[0] is not None and a[0] is not None) or (b[1] is not None and a[1] is not None):
            readable += 1
        if b[0] is not None and a[0] is not None and b[0] > _MIN_VOLUME_CM3:
            ratios.append((name or "?", a[0] / b[0]))
            total_before += b[0]
            total_after += a[0]
    if not readable:
        return ("Scale reported success but no body's volume or bounding box could be read back, so "
                f"the result could not be verified. Re-read the bodies with model_inspect. {remedy}"), {}

    measured = {"volume_ratio": round(total_after / total_before, 6)} if total_before else {}
    if abs(expected - 1.0) > _RATIO_TOL and ratios:
        # Only the named body is known to be wrong; any other body in the call may have resized fine.
        partial = ("The other bodies in this call may have resized, so the result is PARTIAL. "
                   if len(body_names) > 1 else "")
        for name, got in ratios:
            if abs(got - 1.0) <= _RATIO_TOL:
                return (f"Scale reported success but body '{name}' is unchanged - its volume did not "
                        f"move. {partial}{remedy}"), {}
            if abs(got - expected) > _RATIO_TOL * expected:
                return (f"Scale reported success but body '{name}' changed volume by "
                        f"x{round(got, 6)}, not the x{round(expected, 6)} the requested factors "
                        f"imply. {partial}{remedy}"), {}
        measured["expected_volume_ratio"] = round(expected, 6)
        measured["scale_check"] = "volume_ratio"
        return "", measured

    if not any(_moved(b, a) for b, a in zip(before, after)):
        return ("Scale reported success but every body's volume and bounding box is unchanged - "
                f"nothing was resized. {remedy}"), {}
    measured["scale_check"] = "geometry_changed"
    if "volume_ratio" not in measured:
        # No body offered a before/after volume pair above _MIN_VOLUME_CM3, so the ratio check did
        # not run; the flag licenses the missing volume_ratio and the handler states it.
        measured["volume_check_skipped"] = True
    return "", measured


def handler(bodies=None, factor=None, x_factor=None, y_factor=None, z_factor=None,
            anchor: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    axes = {"x_factor": x_factor, "y_factor": y_factor, "z_factor": z_factor}
    given = sorted(n for n, v in axes.items() if v not in (None, ""))
    non_uniform = len(given) == 3
    if given and not non_uniform:
        missing = sorted(n for n in _AXIS_INPUTS if n not in given)
        return error(f"A per-axis scale needs all three factors - got {', '.join(given)}, missing "
                     f"{', '.join(missing)}. Give all three, or use 'factor' for a uniform scale.")
    has_factor = factor not in (None, "")
    if non_uniform and has_factor:
        return error("Give EITHER 'factor' (uniform) OR x_factor/y_factor/z_factor (per-axis), not "
                     "both.")
    if not non_uniform and not has_factor:
        return error("'factor' is required (the uniform scale factor), or give x_factor, y_factor "
                     "and z_factor together for a per-axis scale.")

    design = _common.design()
    if not design:
        return error("No active design. Create or open a document first (see doc_new).")
    comp = target_component(design)

    # Checked before anything is created, so a bad expression refuses with nothing half-built.
    values, literals = {}, {}
    for label in (_AXIS_INPUTS if non_uniform else ("factor",)):
        vi, literal, ferr = _factor_value(label, axes[label] if non_uniform else factor, design)
        if ferr:
            return error(ferr)
        values[label], literals[label] = vi, literal

    body_ents, berr = _BODIES.resolve(bodies)
    if berr:
        return error(berr)
    if anchor:
        point, aerr = _ANCHOR.resolve(anchor)
        if aerr:
            return error(aerr)
        anchor_label = "vertex"
    else:
        point = safe(lambda: comp.originConstructionPoint)
        if point is None:
            return error("No 'anchor' given and the active component has no origin construction "
                         "point to scale about. Pass a vertex handle from find_geometry.")
        anchor_label = "component origin"

    coll = adsk.core.ObjectCollection.create()
    for b in body_ents:
        coll.add(b)
    # A post-mutation proxy can stop answering .name, so the names are read here too.
    before = [_measure(b) for b in body_ents]
    body_names = [safe(lambda b=b: b.name) for b in body_ents]

    try:
        # createInput always carries the UNIFORM factor; the per-axis path seeds it with 1 and lets
        # setToNonUniform replace it.
        seed = values["factor"] if not non_uniform else adsk.core.ValueInput.createByReal(1.0)
        scale_input = comp.features.scaleFeatures.createInput(coll, point, seed)
        if non_uniform:
            applied = scale_input.setToNonUniform(values["x_factor"], values["y_factor"],
                                                  values["z_factor"])
            if applied is False:
                return error("setToNonUniform refused the per-axis factors, so nothing was scaled. "
                             "Retry as a uniform scale with 'factor'.")
        feature = comp.features.scaleFeatures.add(scale_input)
    except Exception as e:
        return error(f"Scale failed: {e}. (A parameter expression may not resolve - check it with "
                     "param_get - or the factor may collapse the geometry; try a factor closer "
                     "to 1.)")
    # scaleFeatures.add returns None in a DIRECT design while the resize lands; the volume/bbox
    # read-back below needs no feature object. In parametric a None feature stays an error.
    direct_no_feature = _common.direct_feature_absence(design, feature)
    if not feature and not direct_no_feature:
        return error(_common.no_feature_error(design, "Scale"))

    # A feature can be ADDED yet fail to compute.
    if safe(lambda: feature.healthState) == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:
        msg = safe(lambda: feature.errorOrWarningMessage) or "no detail"
        return error(f"Scale feature was created but failed to compute: {msg}. Try a factor closer "
                     "to 1, or a different anchor. "
                     + _common.failed_effect_remedy(design, feature))

    if non_uniform:
        nums = [_resolved_factor(literals[n], feature, _AXIS_PARAMS[n]) for n in _AXIS_INPUTS]
        expected = nums[0] * nums[1] * nums[2]
    else:
        expected = _resolved_factor(literals["factor"], feature, "scaleFactor") ** 3

    # A BRepBody reference held across scaleFeatures.add() stays valid and reads the NEW volume, so
    # before and after measure the same objects.
    after = [_measure(b) for b in body_ents]
    verr, measured = _verify(body_names, before, after, expected,
                             _common.failed_effect_remedy(design, feature))
    if verr:
        return error(verr)

    payload = {
        "scaled": True,
        "bodies": body_names,
        "uniform": not non_uniform,
        "anchor": anchor_label,
        "note": "Bodies resized about the anchor point, which stays put. Factors are unitless: 2 "
                "doubles every dimension and multiplies volume by 8.",
    }
    if direct_no_feature:
        payload["no_timeline_feature"] = True
        payload["note"] += " " + _common.DIRECT_FEATURE_NOTE
    else:
        payload["feature"] = safe(lambda: feature.name)
    # An expression is echoed as written AND as the number applied - the feature stores the number.
    if non_uniform:
        payload["factors"] = {n: _inputs.expression_report(axes[n]) for n in _AXIS_INPUTS}
        if any(_inputs.looks_like_expression(axes[n]) for n in _AXIS_INPUTS):
            payload["resolved_factors"] = {n: round(literals[n], 6) for n in _AXIS_INPUTS}
    else:
        payload["factor"] = _inputs.expression_report(factor)
        if _inputs.looks_like_expression(factor):
            payload["resolved_factor"] = round(literals["factor"], 6)
    payload.update(measured)
    if payload.get("volume_check_skipped"):
        payload["note"] += (" No body offered a before/after volume pair above the floor, so the "
                            "volume check did NOT run and no volume_ratio is reported - the verdict "
                            "rests on the bounding box having moved.")
    return ok(payload)


TOOL_DESCRIPTION = (
    "Resize solid bodies about an anchor point that stays put.\n"
    + _outputs.produces_block(RETURNS)
)

_FACTOR_SCHEMA = {"type": ["number", "string"],
                  "description": "Uniform, greater than zero; an expression must be unitless."}


def _axis_schema(axis):
    """The per-axis factor schema; 'axis' names which one in the handler's own refusals."""
    return {"type": ["number", "string"]}


scale_tool = (
    Tool.create_simple(name="model_scale", description=TOOL_DESCRIPTION)
    .add_input_property(*_BODIES.as_property())
    .add_input_property("factor", _FACTOR_SCHEMA)
    .add_input_property("x_factor", _axis_schema("X"))
    .add_input_property("y_factor", _axis_schema("Y"))
    .add_input_property("z_factor", _axis_schema("Z"))
    .add_input_property(*_ANCHOR.as_property())
    .strict_schema()
)
scale_item = Item.create_tool_item(tool=scale_tool, write="write", handler=handler,
                                   run_on_main_thread=True,
                                   postconditions=[_assert.FeatureHealthy()],
                                   verification=Verification(
                                       kind="inline", rung="geometry",
                                       evidence_test="tests/unit/test_model_scale.py::TestHonesty"
                                       "::test_wrong_volume_ratio_reports_error_not_ok"))


def register_tool():
    register(scale_item)
