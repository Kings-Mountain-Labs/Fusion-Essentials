# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Read the active design's parameters - user parameters by default, model/feature ones on request."""

from itertools import islice

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _common
from ._param_common import _param_summary

_MAX_PARAMS = 2000
_ROWS_CAP = 300              # rows emitted per read, either list

# The naming library parts use for their own parameters. The filter below is a NAME test and nothing
# more, so a parameter the modeller happened to name adsk_something is skipped by it too.
_GENERATED_PREFIX = "adsk_"

_OWNER_NOTE = ("Model parameter rows carry their maker: 'owner' (its name), 'owner_type', "
               "'owner_sketch' when the owner lives in a sketch, and 'role' - the slot the "
               "parameter fills on that owner. A key that did not read is absent from the row.")

_NARROW_NOTE = "Narrow with name='<one parameter>' or favorites_only=true."


def _is_generated(nm):
    """True when the parameter's name starts with the library prefix, case-insensitively."""
    return isinstance(nm, str) and nm.lower().startswith(_GENERATED_PREFIX)


def handler(name: str = "", include_model_parameters: bool = False,
            include_generated: bool = False, favorites_only: bool = False) -> dict:
    """See TOOL_DESCRIPTION."""
    design = _common.design()
    if not design:
        return error("No active design (open a document with design geometry).")

    want = (name or "").strip()

    # Single named parameter (search user first, then all).
    if want:
        target = None
        try:
            target = design.userParameters.itemByName(want)
        except Exception:
            target = None
        if not target:
            try:
                for p in design.allParameters:
                    if (safe(lambda: p.name) or "") == want:
                        target = p
                        break
            except Exception:
                pass
        if not target:
            return error(f"Parameter not found: '{name}'.")
        return ok({"parameter": _param_summary(target)})

    # Collection. The AUTHORED set by default: rows whose name starts with the library prefix are
    # counted rather than listed.
    user_params, kept, generated = [], 0, 0
    try:
        ups = design.userParameters
        total = ups.count       # an uncountable collection is a refusal, not an empty read
        walked = min(total, _MAX_PARAMS)
        for p in islice(_common.iter_collection(ups), walked):
            nm = safe(lambda p=p: p.name)
            if not include_generated and _is_generated(nm):
                generated += 1
                continue
            if favorites_only and _common.read_flag(lambda p=p: p.isFavorite) is not True:
                continue
            kept += 1
            if len(user_params) < _ROWS_CAP:
                user_params.append(_param_summary(p))
    except Exception as e:
        return error(f"Could not read user parameters: {e}")

    payload = {"user_parameter_count": total, "matched": kept, "returned": len(user_params),
               "user_parameters": user_params}
    notes = []
    if walked < total:
        # The counts below cover the rows the walk reached, not the whole table.
        payload["walk_truncated"] = True
        notes.append(f"The walk stopped at {walked} of {total} user parameters, so 'matched' and "
                     "'generated_skipped' count only those.")
    if generated:
        payload["generated_skipped"] = generated
        notes.append(f"{generated} of {walked} user parameters were skipped: their names start "
                     f"{_GENERATED_PREFIX} (the naming library parts use). include_generated=true "
                     "lists them.")
    if kept > len(user_params):
        payload["truncated"] = True
        notes.append(f"Listed {len(user_params)} of {kept} matching rows. " + _NARROW_NOTE)

    if include_model_parameters:
        model_params, model_kept, examined = [], 0, 0
        model_walk_truncated = False
        try:
            for p in islice(design.allParameters, _MAX_PARAMS + 1):
                examined += 1
                if examined > _MAX_PARAMS:
                    model_walk_truncated = True
                    break
                nm = safe(lambda p=p: p.name)
                if not isinstance(nm, str) or not nm:
                    return error("Could not read a parameter name while classifying model parameters.")
                try:
                    is_user = design.userParameters.itemByName(nm)
                except Exception as e:
                    return error(f"Could not classify parameter '{nm}' as user or model: {e}")
                if is_user:
                    continue
                model_kept += 1
                if len(model_params) < _ROWS_CAP:
                    model_params.append(_param_summary(p))
        except Exception as e:
            return error(f"Could not read model parameters: {e}")
        payload["model_parameter_count"] = model_kept
        payload["model_parameters"] = model_params
        if model_walk_truncated:
            payload["model_walk_truncated"] = True
            notes.append(f"The model parameter walk stopped at {_MAX_PARAMS} of at least {examined} rows, so "
                         "'model_parameter_count' covers only the observed subset.")
        if model_kept > len(model_params):
            payload["model_parameters_truncated"] = True
            notes.append(f"Listed {len(model_params)} of {model_kept} model parameters. "
                         + _NARROW_NOTE)
        # Advertised only when a row actually carries owner keys - a note describing keys that are
        # not there would send a caller looking for them.
        if any("owner_type" in row for row in model_params):
            notes.append(_OWNER_NOTE)

    if notes:
        payload["note"] = " ".join(notes)
    return ok(payload)


TOOL_DESCRIPTION = (
"Read the active design's parameters - name, expression, value, unit, comment. The authored user "
"parameters by default, or 'name' fetches one. Change one with param_set."
)

tool = (
    Tool.create_simple(name="param_get", description=TOOL_DESCRIPTION)
    .add_input_property("name", {"type": "string",
            "description": "One parameter to fetch."})
    .add_input_property("include_model_parameters", {"type": "boolean"})
    .add_input_property("include_generated", {"type": "boolean",
            "description": f"Also list the {_GENERATED_PREFIX}*-named parameters."})
    .add_input_property("favorites_only", {"type": "boolean"})
    .strict_schema()
)

item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=True)


def register_tool():
    register(item)
