# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The design-PARAMETER substrate: the row every param_* tool publishes, and the by-name lookup."""

import re

from ._common import safe
from . import _common

MAP_BLURB = (
    "the design-PARAMETER substrate: _param_summary - the ONE parameter row every param_* tool "
    "publishes, converting Parameter.value out of DATABASE units (cm/radians) into the "
    "parameter's own and naming the frame it could not convert into; _owner_facts - a MODEL "
    "parameter's maker, each key absent when it did not read; _find_parameter - the exact by-name "
    "lookup, user parameters first")


_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_IDENTIFIER = re.compile(r"[^\W\d]\w*")


def _expression_tokens(expression):
    """Yield expression tokens outside single-quoted literals; this is not a parser."""
    text = expression if isinstance(expression, str) else ""
    i, size = 0, len(text)
    while i < size:
        char = text[i]
        if char.isspace():
            start = i
            while i < size and text[i].isspace():
                i += 1
            yield "space", text[start:i]
            continue
        if char == "'":
            start = i
            i += 1
            while i < size and text[i] != "'":
                i += 1
            if i < size:
                i += 1
            yield "quoted", text[start:i]
            continue
        number = _NUMBER.match(text, i)
        if number:
            i = number.end()
            yield "number", number.group(0)
            continue
        identifier = _IDENTIFIER.match(text, i)
        if identifier:
            i = identifier.end()
            yield "identifier", identifier.group(0)
            continue
        yield "other", char
        i += 1


def _normalized_expression(expression):
    """The non-space lexical tokens of an expression; this is not a full Fusion parser."""
    return tuple((kind, text) for kind, text in _expression_tokens(expression) if kind != "space")


def _expression_identifiers(expression):
    """The unquoted identifier tokens in an expression; this is not a full Fusion parser."""
    return {text for kind, text in _expression_tokens(expression) if kind == "identifier"}


def _owner_facts(p) -> dict:
    """The maker of a MODEL parameter as the row's own keys, each omitted when it did not read."""
    # A UserParameter has no .createdBy and the read raises, which is the guard.
    owner = safe(lambda: p.createdBy)
    if owner is None:
        return {}
    out = {}
    name = safe(lambda: owner.name)
    if isinstance(name, str) and name:
        out["owner"] = name
    out["owner_type"] = type(owner).__name__
    # A dimension carries parentSketch; a feature has no such attribute and the read raises.
    sketch = safe(lambda: owner.parentSketch.name)
    if isinstance(sketch, str) and sketch:
        out["owner_sketch"] = sketch
    role = safe(lambda: p.role)
    if isinstance(role, str) and role:
        out["role"] = role
    return out


def _param_summary(p, units_manager=None) -> dict:
    if units_manager is None:
        d = _common.design()
        units_manager = safe(lambda: d.unitsManager) if d else None
    unit = safe(lambda: p.unit)
    out = {
    "name": safe(lambda: p.name),
    **_owner_facts(p),
    "expression": safe(lambda: p.expression),
    "unit": unit,
    "comment": safe(lambda: p.comment),
    "favorite": safe(lambda: p.isFavorite),
    "value": None,
    }
    # Parameter.value is in DATABASE units (cm / radians), not the parameter's own 'unit': a "50 mm"
    # length reads 5.0 and a "90 deg" angle 1.5708, so it is converted below.
    v = safe(lambda: p.value)
    if v is None:
        out["value"] = safe(lambda: p.textValue)     # text parameters have no numeric value
        return out
    out["value_internal"] = v
    if not unit:
        out["value"] = v
        out["value_units"] = ""
        return out
    converted = None
    if units_manager is not None:
        converted = safe(lambda: units_manager.convert(v, units_manager.internalUnits, unit))
        # Only a real number may be published under the parameter's unit; anything else is reported
        # as the internal value rather than mislabelled.
        if not isinstance(converted, (int, float)) or isinstance(converted, bool):
            converted = None
    out["value"] = converted if converted is not None else v
    out["value_units"] = unit if converted is not None else "internal (cm/radians)"
    return out


def _find_parameter(design, name):
    """Find a parameter by exact name: user parameters first, then all parameters."""
    try:
        p = design.userParameters.itemByName(name)
        if p:
            return p
    except Exception:
        pass
    try:
        for p in design.allParameters:
            if (safe(lambda: p.name) or "") == name:
                return p
    except Exception:
        pass
    return None
