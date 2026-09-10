# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""SET one APPLICATION preference (app.preferences): read-first-or-refuse, assign, read back, and
publish 'previous' beside 'now'. The member tier table lives in sys_get_preferences."""

import math

import adsk.core

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import ok, error, safe
from . import _outputs
from . import sys_get_preferences as _prefs

app = adsk.core.Application.get()

RETURNS = [
    _outputs.ReturnsValue("previous", "the value read just before the write - pass it back here "
                                      "to restore"),
]

_UNREAD = object()      # a getter that RAISED - distinct from a member that reads None

# json.loads accepts NaN / Infinity / -Infinity, so either can arrive as 'value'.
_NOT_FINITE = ("'{name}' takes a finite number; got {value}. NaN and Infinity are not values a "
               "preference can hold.")


def _resolve_member(path):
    """(group_key, item_name, Member, '') for '<group>.<member>', or '<group>.<item>.<member>' for a
    collection group; ('', '', None, error_text) on a miss, which lists what is available there."""
    keys = _prefs.GROUP_KEYS
    parts = [p.strip() for p in (path or "").split(".")]
    shape = ("'<group>.<member>' (or '<group>.<product>.<member>' for "
             + " / ".join(sorted(_prefs.COLLECTION_KEYS)) + ")")
    if len(parts) < 2 or not all(parts):
        return "", "", None, (f"'{path}' is not a member path - use {shape}, e.g. "
                              f"'display.generalPrecision'. Groups: {', '.join(keys)}.")
    group = next((k for k in keys if k == parts[0].lower()), "")
    if not group:
        return "", "", None, f"Unknown preference group '{parts[0]}'. Groups: {', '.join(keys)}."
    nests = group in _prefs.COLLECTION_KEYS
    if len(parts) != (3 if nests else 2):
        return "", "", None, (f"'{group}' members are addressed as "
                              + (f"'{group}.<product>.<member>'." if nests else f"'{group}.<member>'."))
    item = parts[1] if nests else ""
    members = _prefs.GROUP_MEMBERS[group]
    name = next((n for n in members if n.lower() == parts[-1].lower()), "")
    if not name:
        return "", "", None, (f"'{group}' has no member '{parts[-1]}'. Members: "
                              f"{', '.join(sorted(members))}.")
    return group, item, members[name], ""


def _resolve_holder(prefs, group, item):
    """(object the member lives on, '') - the group itself, or the named item of a collection group
    (resolved through the ONE item walk in sys_get_preferences). ('', error_text) on a miss."""
    if group not in _prefs.COLLECTION_KEYS:
        obj = safe(lambda: getattr(prefs, _prefs.GROUP_ATTR[group]))
        if obj is None:
            return None, f"'{_prefs.GROUP_ATTR[group]}' did not read on this build."
        return obj, ""
    items = _prefs.collection_items(prefs, group)
    hit = [obj for name, obj in items if name.lower() == item.lower()]
    if not hit:
        available = ", ".join(name for name, _obj in items) or "(none)"
        return None, f"'{group}' has no product '{item}'. Products: {available}."
    return hit[0], ""


def _choices(names) -> str:
    """'MemberName=int, ...' - the one rendering of an enum family's legal values, shared by the
    name refusal and the int refusal so a caller is told the same thing either way."""
    return ", ".join(f"{n}={v}" for n, v in sorted(names.items()))


def _coerce(member, previous, value):
    """(value_to_assign, '') or (None, error_text). The type is checked against the CURRENT value,
    which is the only typing the API exposes; a str on an enum member resolves through the live
    enum class, so an illegal member name fails here instead of landing as a wrong int."""
    names = _prefs.enum_names(member.enum)
    if isinstance(value, str) and not isinstance(previous, str):
        if not names:
            return None, (f"'{member.name}' takes {type(previous).__name__}, not text - and no enum "
                          f"member names are readable for it on this build. Current value: {previous!r}.")
        hit = next((v for n, v in names.items() if n.lower() == value.strip().lower()), None)
        if hit is None:
            return None, (f"'{value}' is not a member of the enum '{member.name}' takes. Members: "
                          f"{_choices(names)}.")
        return hit, ""
    if isinstance(previous, bool):
        if not isinstance(value, bool):
            return None, f"'{member.name}' takes a boolean; got {type(value).__name__} {value!r}."
        return value, ""
    if isinstance(previous, int):
        # keyed on the value that was READ, never on the member's name: isAutoLookAtSketch2 is
        # is-prefixed and holds an enum int, not a bool.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, f"'{member.name}' takes an integer; got {type(value).__name__} {value!r}."
        if not math.isfinite(value):
            return None, _NOT_FINITE.format(name=member.name, value=value)
        if isinstance(value, float) and value != int(value):
            return None, f"'{member.name}' takes an integer; got {value!r}."
        # Measured: an int OUTSIDE the member's enum is accepted and stored (materialDisplayUnit=99
        # read back 99), so the read-back gate cannot catch it - it has to be refused before the
        # assignment, on a setting with no undo.
        if names and int(value) not in names.values():
            return None, (f"{int(value)} is not a value of the enum '{member.name}' takes. Members: "
                          f"{_choices(names)}.")
        if member.minimum is not None and int(value) < member.minimum:
            return None, (f"'{member.name}' takes a value of at least {member.minimum}; "
                          f"got {int(value)}.")
        return int(value), ""
    if isinstance(previous, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, f"'{member.name}' takes a number; got {type(value).__name__} {value!r}."
        if not math.isfinite(value):
            return None, _NOT_FINITE.format(name=member.name, value=value)
        if member.minimum is not None and float(value) < member.minimum:
            return None, (f"'{member.name}' takes a value of at least {member.minimum}; "
                          f"got {float(value)}.")
        return float(value), ""
    if isinstance(previous, str):
        if not isinstance(value, str):
            return None, f"'{member.name}' takes text; got {type(value).__name__} {value!r}."
        return value, ""
    return None, (f"'{member.name}' currently reads as {type(previous).__name__} - this tool sets "
                  "boolean / number / text members only.")


def handler(member: str = "", value=None) -> dict:
    """See TOOL_DESCRIPTION."""
    path = (member or "").strip()
    if not path:
        return error("Provide 'member' as the path sys_get_preferences reports it at - it lists "
                     "every member, its value and its tier.")
    if value is None:
        return error(f"Provide 'value' for '{path}' - nothing was written.")

    group, item, target, resolve_error = _resolve_member(path)
    if resolve_error:
        return error(resolve_error + " Nothing was written.")
    label = ".".join(p for p in (group, item, target.name) if p)

    if target.tier == _prefs.TIER_REFUSED:
        return error(f"Refused: '{label}' is read-only through this server - {target.reason}. "
                     "Nothing was written; read it with sys_get_preferences.")

    prefs = safe(lambda: app.preferences)
    if prefs is None:
        return error("app.preferences did not read - nothing was written.")
    group_obj, holder_error = _resolve_holder(prefs, group, item)
    if holder_error:
        return error(holder_error + " Nothing was written.")

    # Read FIRST: a value that cannot be read cannot be restored, and this write has no undo.
    previous = safe(lambda: getattr(group_obj, target.name), _UNREAD)
    if previous is _UNREAD:
        return error(f"Refused: '{label}' raises when read on this build, so its current value "
                     "cannot be captured - and a preference has no undo, so an unrestorable value "
                     "is not written.")

    wanted, coerce_error = _coerce(target, previous, value)
    if coerce_error:
        return error(coerce_error + " Nothing was written.")

    # The assignment is immediate - app.preferences carries no save/apply/commit/reset - and an
    # out-of-enum int is accepted AND stored, so the read-back below gates what landed.
    try:
        setattr(group_obj, target.name, wanted)
    except Exception as e:
        return error(f"Setting '{label}' raised: {e}. It still reads "
                     f"{safe(lambda: getattr(group_obj, target.name), None)!r} (was {previous!r}).")

    now = safe(lambda: getattr(group_obj, target.name), _UNREAD)
    if now is _UNREAD:
        return error(f"'{label}' was assigned {wanted!r} but now RAISES when read, so whether it "
                     f"landed cannot be verified. Its value before the write was {previous!r} - "
                     "restore it by hand if the application misbehaves.")
    if now != wanted:
        return error(f"'{label}' did not take: {wanted!r} was requested, it now reads {now!r}, and "
                     f"it read {previous!r} before the call. Set it again with {previous!r} if the "
                     "value it holds now is wrong.")

    out = {"member": label, "previous": previous, "now": now, "tier": target.tier,
           "note": ("Application preference - it belongs to no document, and there is no undo and "
                    "no version history. Restore by calling this tool with the 'previous' value.")}
    decoded = _prefs.enum_member_name(target.enum, now)
    if decoded:
        out["enum"] = decoded
    return ok(out)


TOOL_DESCRIPTION = (
    "SET one APPLICATION preference, by the path sys_get_preferences reports. No undo.\n"
    + _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="sys_set_preferences", description=TOOL_DESCRIPTION)
    .add_input_property("member", {"type": "string"})
    .add_input_property("value", {"type": ["boolean", "integer", "number", "string"],
            "description": "An enum member NAME may be given as text."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_sys_preferences.py::TestWriteProtocol"
                      "::test_a_silent_noop_setter_is_an_error"))


def register_tool():
    register(item)
