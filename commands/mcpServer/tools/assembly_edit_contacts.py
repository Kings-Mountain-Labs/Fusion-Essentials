# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lifecycle for a design's contact sets - create, re-member, rename, suppress, delete - plus the two
design-level flags that decide whether any of them does anything. A contact set needs at least 2
DISTINCT members: measured on Fusion 2704.1.39, contactSets.add with one member (or the same member
twice) raises '3 : ContactSetRequest: bad occurrences'.
"""

import re

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import error, ok, safe
from . import _common
from . import _contacts
from . import _inputs

# The shape Fusion's auto-dedupe lands: the requested name followed by ' (N)'. A landed name that
# does NOT match this is a divergence with no cause the tool can read.
_DEDUPED_NAME = re.compile(r"^.+ \(\d+\)$")

_ACTIONS = ("create", "set_members", "rename", "suppress", "unsuppress", "delete",
            "enable_analysis", "disable_analysis", "set_analysis_scope")
_MEMBER_ACTIONS = ("create", "set_members")

_ACTION = _inputs.Choice("action", list(_ACTIONS), required=True)
_MEMBERS = _inputs.TargetRefList(
    "members", with_kinds=True,
    contract="create/set_members: 2 or more DISTINCT occurrences and/or BRep bodies to relate.")
_SCOPE = _inputs.Choice("scope", ["contact_sets", "all_bodies"])


def _flags(design):
    """(isContactAnalysisEnabled, isContactSetAnalysis), each True/False or None when unreadable -
    read through the ONE unreadable-flag read so a raising getter never lands as a confident False."""
    return (_common.read_flag(lambda: design.isContactAnalysisEnabled),
            _common.read_flag(lambda: design.isContactSetAnalysis))


def _analysis_fields(enabled, use_sets):
    return {"analysis_enabled": (None if enabled is None else bool(enabled)),
            # null, not all_bodies, when the flag cannot be read - an unreadable scope is not a scope.
            "scope": (None if use_sets is None else
                      ("contact_sets" if use_sets else "all_bodies"))}


def _inert_clause(design):
    """What the design-level flags mean for the set just written: a contact set takes part only when
    contact analysis is ON and scoped to the sets."""
    enabled, use_sets = _flags(design)
    if enabled is None:
        return " Whether contact analysis is enabled could not be read on this design."
    if not enabled:
        return (" Contact analysis is OFF for this design, so this set is INERT until "
                "action='enable_analysis'.")
    if use_sets is None:
        return " Contact analysis is ON, but its scope could not be read on this design."
    if not use_sets:
        return (" Contact analysis is ON but scoped to ALL bodies, so contact sets are ignored "
                "until action='set_analysis_scope' with scope='contact_sets'.")
    return ""


_UNNAMED_CLAUSE = (" {n} member(s) read back with no readable name and are counted in member_count, "
                   "not named in members; the measured unnamed case is a BODY member - it arrives "
                   "as an object neither cast accepts on this build.")


def _members_or_error(pairs):
    """(entities, occurrence_names, error): the plain list the platform takes, the names its
    read-back must show, or the refusal. Distinctness is checked BEFORE the call so the caller reads
    which member repeated instead of the platform's opaque 'bad occurrences'."""
    ents, expected, keys = [], [], []
    for i, (ent, kind) in enumerate(pairs):
        label = _contacts.member_label(ent) or safe(lambda ent=ent: ent.name) or f"members[{i}]"
        if kind == "mesh":
            return None, None, (f"'members'[{i}] ('{label}') is a MESH body - a contact set holds "
                                "occurrences and BRep bodies. Pass the occurrence that holds it, or "
                                "convert it with mesh_to_brep.")
        key = safe(lambda ent=ent: ent.entityToken) or label
        if key in keys:
            return None, None, (f"'members' names '{label}' twice - a contact set needs at least 2 "
                                "DISTINCT members. Measured: contactSets.add with the same member "
                                "twice raises '3 : ContactSetRequest: bad occurrences'.")
        keys.append(key)
        ents.append(ent)
        if kind == "occurrence":
            expected.append(label)
    if len(ents) < 2:
        return None, None, (f"'members' holds {len(ents)} member(s) - a contact set relates at least "
                            "2 DISTINCT occurrences/bodies. Measured: contactSets.add with one "
                            "member raises '3 : ContactSetRequest: bad occurrences', and with an "
                            "empty list '3 : Invalid input.'")
    return ents, expected, None


def _verify_membership(cs, name, ents, expected):
    """The read-back both member-writing actions gate on: (payload_fields, error). The count comes
    from len(), which answers for every member; the names confirm the occurrences actually landed."""
    names, total, unnamed = _contacts.membership(cs)
    if total is None:
        return None, (f"The members of contact set '{name}' cannot be read back, so nothing confirms "
                      "what it holds.")
    if total != len(ents):
        return None, (f"Contact set '{name}' holds {total} member(s), not the {len(ents)} requested - "
                      "the membership did not take.")
    missing = [n for n in expected if n not in names]
    if missing:
        return None, (f"Contact set '{name}' does not list {', '.join(missing)} - it reads back "
                      f"{', '.join(names) or 'no named member'}, so the membership did not take.")
    fields = {"members": names, "member_count": total}
    if unnamed:
        fields["members_unreadable"] = unnamed
    return fields, None


def _do_create(design, ents, expected):
    sets = _contacts.contact_sets(design)
    before = _contacts.contact_set_names(design)
    raised, cs = "", None
    try:
        # A PLAIN Python list: ContactSets.add takes std::vector<Ptr<Base>>, and an ObjectCollection
        # raises a SWIG TypeError (measured on Fusion 2704.1.39).
        cs = sets.add(ents)
    except Exception as e:
        raised = str(e)
    if cs is None:
        # A raise is not proof nothing landed, so the COLLECTION decides what is reported: a set
        # that appeared under a raised call is adopted and the raise disclosed, rather than an
        # error over a set the caller can no longer address.
        after = _contacts.contact_set_names(design)
        new = [n for n in after if n not in before]
        if len(new) != 1:
            return error(f"Creating the contact set failed: "
                         f"{raised or 'contactSets.add returned nothing'}. The design holds "
                         f"{len(after)} contact set(s); it held {len(before)}.")
        cs, _err = _contacts.find_contact_set(design, new[0])
        if cs is None:
            return error(f"contactSets.add raised ({raised}) and a set named '{new[0]}' appeared "
                         "that cannot be re-read. Inspect the design with "
                         "assembly_get(include=['contacts']).")
    nm = safe(lambda: cs.name)
    fields, merr = _verify_membership(cs, nm or "(unnamed)", ents, expected)
    if merr:
        return error("The contact set was created but is not what was asked for: " + merr +
                     (f" The create call also raised '{raised}'." if raised else "") +
                     " Inspect it with assembly_get(include=['contacts']).")
    out = {"created": True, "contact_set": nm}
    out.update(fields)
    out["note"] = ((f"Fusion named the set '{nm}' - address it by that name from here."
                    if nm else "The set was created but reports no name - find it with "
                               "assembly_get(include=['contacts']).")
                   + _inert_clause(design))
    if fields.get("members_unreadable"):
        out["note"] += _UNNAMED_CLAUSE.format(n=fields["members_unreadable"])
    if raised:
        out["note"] += (f" The create call raised '{raised}' and the set was found in the design "
                        "afterwards, holding the members read back above.")
    return ok(out)


def _do_set_members(design, cs, name, ents, expected):
    _before_names, before_count, _u = _contacts.membership(cs)
    try:
        # occurencesAndBodies is spelled with ONE 'r' - that misspelling IS the live property. The
        # correctly spelled occurrencesAndBodies is accepted silently and changes nothing (measured
        # on Fusion 2704.1.39), so the assignment is read back rather than trusted.
        cs.occurencesAndBodies = ents
    except Exception as e:
        return error(f"Could not set the members of contact set '{name}': {e}")
    fields, merr = _verify_membership(cs, name, ents, expected)
    if merr:
        return error(merr)
    out = {"contact_set": name, "member_count_before": before_count}
    out.update(fields)
    out["note"] = ("The members read back above are what the set now holds." +
                   _inert_clause(design))
    if fields.get("members_unreadable"):
        out["note"] += _UNNAMED_CLAUSE.format(n=fields["members_unreadable"])
    return ok(out)


def _do_rename(cs, name, new_name):
    want = (new_name or "").strip()
    if not want:
        return error("'new_name' is required for action='rename'.")
    try:
        cs.name = want
    except Exception as e:
        return error(f"Could not rename contact set '{name}': {e}")
    landed = safe(lambda: cs.name)
    if not landed:
        return error(f"Contact set '{name}' reports no name after the rename, so nothing confirms "
                     "it.")
    if landed == name and landed != want:
        return error(f"Renaming contact set '{name}' to '{want}' did not take - it still reads "
                     f"'{landed}'.")
    out = {"contact_set": landed, "previous_name": name, "requested_name": want,
           "note": f"The set is now named '{landed}' - address it by that name from here."}
    if landed != want:
        # Measured: assigning a name another set already holds raises nothing and lands 'Name (1)'.
        # Only a landed name of THAT shape carries the dedupe cause; any other divergence is
        # reported as what was read, with no cause invented for it.
        if _DEDUPED_NAME.match(landed) and landed.startswith(want):
            out["note"] = (f"Fusion landed the name '{landed}', not the requested '{want}' - a name "
                           "already in use is auto-deduped to 'Name (1)'. Address the set by "
                           f"'{landed}' from here.")
        else:
            out["note"] = (f"Fusion landed the name '{landed}', not the requested '{want}' - the "
                           "platform changed it and the reason is not readable from here. Address "
                           f"the set by '{landed}' from here.")
    return ok(out)


def _do_suppress(cs, name, suppressed):
    was = _common.read_flag(lambda: cs.isSuppressed)
    try:
        cs.isSuppressed = bool(suppressed)
    except Exception as e:
        return error(f"Could not set isSuppressed on contact set '{name}': {e}")
    now = _common.read_flag(lambda: cs.isSuppressed)
    # An UNREADABLE flag is not a False: treating it as one would let a swallowed write pass the
    # mismatch gate below and report ok. It is unconfirmed, and says so.
    if now is None:
        return error(f"isSuppressed cannot be read on contact set '{name}' after setting it to "
                     f"{bool(suppressed)}, so the change is UNCONFIRMED. Re-read the set with "
                     "assembly_get(include=['contacts']).")
    if bool(now) != bool(suppressed):
        return error(f"Setting isSuppressed={bool(suppressed)} on contact set '{name}' did not take "
                     f"- it reads {now}.")
    return ok({"contact_set": name, "is_suppressed": bool(now),
               # null, not False, when the prior flag could not be read - an unreadable state is not "off".
               "was_suppressed": (None if was is None else bool(was)),
               "note": f"isSuppressed now reads {bool(now)} on contact set '{name}'; the set stays "
                       "in the design until action='delete'. What suppression does to contact "
                       "behavior is not measured here."})


def _do_delete(design, cs, name):
    try:
        did = cs.deleteMe()
    except Exception as e:
        return error(f"Deleting contact set '{name}' failed: {e}")
    if not did:
        return error(f"Fusion declined to delete contact set '{name}' (deleteMe returned false) - it "
                     "is still in the design.")
    # Re-list: a deleted set must be GONE from the collection, not merely reported so. contact_set_names
    # drops an unreadable name, so `remaining` is the READABLE count left.
    remaining = _contacts.contact_set_names(design)
    if any((n or "").lower() == name.lower() for n in remaining):
        return error(f"deleteMe reported success but contact set '{name}' is still listed - it was "
                     "not deleted.")
    return ok({"deleted": True, "contact_set": name, "remaining": len(remaining),
               "note": "The contact set is no longer listed; 'remaining' counts the readable sets "
                       "left. Undo in Fusion if unintended - the API cannot restore it. Build "
                       "another with action='create'."})


def _do_enable(design, want):
    try:
        design.isContactAnalysisEnabled = bool(want)
    except Exception as e:
        return error(f"Could not set isContactAnalysisEnabled={bool(want)}: {e}")
    enabled, use_sets = _flags(design)
    # An UNREADABLE flag is not a False: treating it as one would pass the mismatch gate below and
    # publish a design-wide physics claim nothing confirmed.
    if enabled is None:
        return error(f"isContactAnalysisEnabled cannot be read after setting it to {bool(want)}, so "
                     "the change is UNCONFIRMED - contact analysis may be in either state. Re-read "
                     "with assembly_get(include=['contacts']).")
    if bool(enabled) != bool(want):
        return error(f"Setting isContactAnalysisEnabled={bool(want)} did not take - it reads "
                     f"{enabled}.")
    out = _analysis_fields(enabled, use_sets)
    if want:
        if use_sets is None:
            out["note"] = ("Contact analysis is ON, but isContactSetAnalysis cannot be read, so what "
                           "it is scoped to is unknown here.")
        else:
            out["note"] = ("Contact analysis is ON and runs " +
                           ("using the design's contact sets."
                            if use_sets else
                            "between ALL bodies, ignoring every contact set - switch with "
                            "action='set_analysis_scope', scope='contact_sets'."))
    else:
        # Measured: with analysis off the scope flag reads False whatever was set, and the scope in
        # force before comes back on re-enable.
        out["note"] = ("Contact analysis is OFF: no contact analysis is performed and every contact "
                       "set is inert. 'scope' reads all_bodies while analysis is off, and the scope "
                       "in force before it was disabled comes back on action='enable_analysis'.")
    return ok(out)


def _do_scope(design, scope):
    if not scope:
        return error("'scope' is required for action='set_analysis_scope': contact_sets (analysis "
                     "uses the sets) or all_bodies (analysis ignores them).")
    enabled = _common.read_flag(lambda: design.isContactAnalysisEnabled)
    if enabled is None:
        # UNREADABLE is not OFF: the platform's refusal below is a fact about analysis being off,
        # and asserting it over a read that never answered would name a cause nothing observed.
        return error("isContactAnalysisEnabled cannot be read on this design, so whether contact "
                     "analysis is on - the precondition for a scope write - is UNKNOWN. Nothing was "
                     "changed. Re-read with assembly_get(include=['contacts']).")
    if not enabled:
        # Measured: the platform itself refuses the write while analysis is off - assigning
        # isContactSetAnalysis then raises '3 : Contact analysis is disabled.' and nothing lands.
        return error(f"Contact analysis is not enabled on this design (isContactAnalysisEnabled "
                     f"reads {enabled}), and the platform refuses a scope write while it is off - "
                     "assigning isContactSetAnalysis raises '3 : Contact analysis is disabled.'. "
                     "Run action='enable_analysis' first, then set the scope. Nothing was changed.")
    want = scope == "contact_sets"
    try:
        design.isContactSetAnalysis = want
    except Exception as e:
        return error(f"Could not set isContactSetAnalysis={want}: {e}")
    enabled_now, use_sets = _flags(design)
    # An UNREADABLE flag is not a False - the write is unconfirmed, not proven wrong.
    if use_sets is None:
        return error(f"isContactSetAnalysis cannot be read after setting it to {want}, so the scope "
                     "change is UNCONFIRMED. Re-read with assembly_get(include=['contacts']).")
    if bool(use_sets) != want:
        return error(f"Setting isContactSetAnalysis={want} did not take - it reads {use_sets}.")
    out = _analysis_fields(enabled_now, use_sets)
    out["note"] = ("Contact analysis now runs " +
                   ("using the design's contact sets - list them with "
                    "assembly_get(include=['contacts'])."
                    if want else "between ALL bodies, ignoring every contact set."))
    return ok(out)


def handler(action: str = "", name: str = "", members=None, new_name: str = "",
            scope: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    values, verr = _inputs.resolve_inputs([_ACTION, _SCOPE], {"action": action, "scope": scope})
    if verr:
        return verr
    action, scope = values["action"], values["scope"]

    design = _common.design()
    if not design:
        return error("No active design. Open or create a document first (see doc_new).")
    if _contacts.contact_sets(design) is None:
        return error("This design reports no contactSets collection, so contact sets cannot be read "
                     "or changed here.")

    if action in ("enable_analysis", "disable_analysis"):
        return _do_enable(design, action == "enable_analysis")
    if action == "set_analysis_scope":
        return _do_scope(design, scope)

    ents, expected = [], []
    if action in _MEMBER_ACTIONS:
        resolved, mem_err = _inputs.resolve_inputs([_MEMBERS], {"members": members})
        if mem_err:
            return mem_err
        ents, expected, gerr = _members_or_error(resolved["members"])
        if gerr:
            return error(gerr)
    if action == "create":
        return _do_create(design, ents, expected)

    cs, rerr = _contacts.find_contact_set(design, name)
    if rerr:
        return error(rerr)
    nm = safe(lambda: cs.name) or (name or "").strip()

    if action == "set_members":
        return _do_set_members(design, cs, nm, ents, expected)
    if action == "rename":
        return _do_rename(cs, nm, new_name)
    if action in ("suppress", "unsuppress"):
        return _do_suppress(cs, nm, action == "suppress")
    return _do_delete(design, cs, nm)


TOOL_DESCRIPTION = (
    "Maintain the design's contact sets - the named groups of occurrences/bodies Fusion checks for "
    "contact."
)

tool = (
    Tool.create_simple(name="assembly_edit_contacts", description=TOOL_DESCRIPTION)
    .add_input_property(*_ACTION.as_property())
    .add_input_property("name", {"type": "string"})
    .add_input_property(*_MEMBERS.as_property())
    .add_input_property("new_name", {"type": "string", "description": "rename: the name you want."})
    .add_input_property(*_SCOPE.as_property())
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="destructive", handler=handler, run_on_main_thread=True,
    verification=Verification(
        kind="inline", rung="value",
        evidence_test="tests/unit/test_assembly_edit_contacts.py::TestDelete"
                      "::test_a_survivor_after_a_true_delete_is_an_error"))


def register_tool():
    register(item)
