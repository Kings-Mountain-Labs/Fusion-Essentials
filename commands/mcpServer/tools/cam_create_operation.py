# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Create a CAM milling operation in a setup: pick a strategy, a tool by (library_url, index)
reference, and add it. Generating is opt-in (generate=true) - the geometry selection comes first."""

import re

import adsk.core
import adsk.cam

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import apply_rename, counted, named_with_remainder, ok, error, read_flag, safe
from ._cam_common import get_cam, find_setup, operation_nodes, register_future, setups
# _read_tool_number is the one tool_number read; _tp is the one tool-parameter value read.
from .cam_edit_tools import _read_tool_number, _tp

app = adsk.core.Application.get()


def tool_index_of(value):
    """A 'tool_index' request as an INT, or None for anything that is not one. The wire can deliver
    it as TEXT, and an ORDER comparison against text RAISES out of the handler instead of refusing,
    so every index check reads through here first."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# The 'tool_index' the schema defaults to when the caller names none.
_NO_INDEX = -1

_INDEX_ABSENT = ("Provide 'tool_index' (with 'tool_scope=document' for this doc's library, or "
                 "'tool_library_url' for a shared one) - both from cam_edit_tools.")


def index_request_error(value):
    """The refusal for a 'tool_index' that cannot address a tool, or None when it can. An ABSENT
    index asks for one; a value that is PRESENT but not a whole number NAMES the offending value,
    since 'provide an index' reads as a bug report to a caller who provided one."""
    if value is None or value == _NO_INDEX:
        return _INDEX_ABSENT
    i = tool_index_of(value)
    if i is None:
        return (f"tool_index {value!r} is not a whole number - pass the integer index "
                "cam_edit_tools lists beside each tool.")
    if i < 0:
        return (f"tool_index {i} is negative - pass the 0-based index cam_edit_tools lists beside "
                "each tool.")
    return None


def _index_in_range(index, n, what):
    """(int index, None) when `index` addresses one of `n` items, else (None, refusal) - `what`
    names the library in it. The ONE place a tool index is turned into a number and bounded."""
    i = tool_index_of(index)
    if i is None:
        return None, (f"tool_index {index!r} is not a whole number - pass the integer index "
                      f"cam_edit_tools lists beside each tool ({what} holds {n}).")
    if not (0 <= i < n):
        return None, f"tool_index {i} out of range ({what} has {n} tools)."
    return i, None


def _doc_tool_at(cam, index):
    """Fetch a Tool from THIS document's tool library by index (cam.documentToolLibrary). This is the
    library cam_edit_tools writes to at scope='document' - so an agent can create an op against a
    tool it just made in the doc, with no URL plumbing. Returns (tool, None) or (None, error)."""
    dtl = safe(lambda: cam.documentToolLibrary)
    if dtl is None:
        return None, "This document has no document tool library."
    n = safe(lambda: dtl.count, 0) or 0
    if n == 0:
        return None, ("The document tool library is empty. Add a tool first "
                      "(cam_edit_tools scope='document' action='add').")
    index, ierr = _index_in_range(index, n, "the document library")
    if ierr:
        return None, ierr
    t = safe(lambda: dtl.item(index))
    return (t, None) if t is not None else (None, f"No tool at document index {index}.")


def _tool_at(library_url, index):
    """Fetch a Tool by (library_url, index) - the reference handle cam_edit_tools (list) returns for a
    SHARED library (local/cloud/hub/Fusion samples). Returns (tool, None) or (None, error)."""
    libs = safe(lambda: adsk.cam.CAMManager.get().libraryManager.toolLibraries)
    if not libs:
        return None, "Tool libraries unavailable."
    url = safe(lambda: adsk.core.URL.create(library_url))
    if not url:
        return None, f"Bad tool_library_url '{library_url}'."
    lib = safe(lambda: libs.toolLibraryAtURL(url))
    if not lib:
        return None, f"Could not load tool library at '{library_url}'."
    n = safe(lambda: lib.count, 0) or 0
    index, ierr = _index_in_range(index, n, "the library")
    if ierr:
        return None, ierr
    t = safe(lambda: lib.item(index))
    if t is None:
        return None, f"No tool at index {index}."
    return t, None


# A tool number prefixes a description as '#<n> - ', measured live: the operation's COPY always
# carries it, and a DOCUMENT-library tool's own description already does while a shared sample
# library's does not - so it is stripped from BOTH sides before they are compared.
_OP_TOOL_PREFIX = re.compile(r"^#\d+ - ")


def _tool_facts(t):
    """A CAM Tool's (description, tool_number) - two fetches of one tool are different Python
    objects, so a read-back is compared on what the tool READS, never on identity."""
    return (safe(lambda: t.description), _read_tool_number(t))


def _names_the_same_tool(read_back, library):
    """Whether the description read off Operation.tool names `library`'s tool - the number prefix
    stripped from BOTH, since either side can carry one. None when either side did not read, which
    settles nothing. EQUALITY after the strip: '#1 - 16mm Flat Endmill' does not name a 6mm one."""
    if not read_back or not library:
        return None
    return _OP_TOOL_PREFIX.sub("", read_back) == _OP_TOOL_PREFIX.sub("", library)


# The probing strategies and the tool_type a probe reads. A cutting tool is ACCEPTED at create and
# only reports at generate, so the type is checked here instead of leaving that for the toolpath.
_PROBE_STRATEGIES = ("probe", "probe_geometry", "inspect_surface")
_PROBE_TOOL_TYPE = "probe"

_INSPECT_SURFACE = "inspect_surface"

# MEASURED: inspectSurfacePositions takes no write through the API, so no call here lands the
# operation's first inspection point.
_INSPECT_POINTS_NOTE = (
    " Its inspection points are UI-only: no API write lands one in inspectSurfacePositions - place "
    "them in Fusion.")


def _probe_tool_refusal(strategy, tool):
    """The refusal for a probing strategy handed a tool whose tool_type is not a probe, else None -
    a type that did not read is no verdict and refuses nothing."""
    if strategy not in _PROBE_STRATEGIES:
        return None
    kind = _tp(tool, "tool_type")
    if kind is None or str(kind).strip().lower() == _PROBE_TOOL_TYPE:
        return None
    return (f"Strategy '{strategy}' needs a PROBE and the requested tool reads tool_type "
            f"{str(kind)!r}, so nothing was created. A face mill on a probing strategy generated "
            "with 'Tool (face mill) is not supported for the strategy.' Take a probe instead: "
            "cam_edit_tools(action='add', scope='document', add_tools=[{'from_type': 'probe'}]), "
            "or copy one from the shipped 'Probes' library "
            "(cam_edit_tools(action='list', scope='fusion')).")


def _operation_name_clash(cam, want, current=""):
    """The refusal for a name operations already answer to, shared by the create and the rename.
    None when the name is free, empty, or already the caller's own (`current`)."""
    want = (want or "").strip()
    if not want or want.lower() == (current or "").strip().lower():
        return None
    taken = [n for n in operation_nodes(cam) if (n.name or "").lower() == want.lower()]
    if not taken:
        return None
    # Measured: Operation.name DEDUPES silently rather than refusing - 'Face1' with one taken lands
    # 'Face11', with no separator - so a taken name is refused here instead of landing unasked-for.
    return (f"{len(taken)} operation(s) already answer to '{want}'. Operation.name dedupes rather "
            f"than refusing, so it would land as something like '{want}1' - a name nothing asked "
            "for. Pick one no operation carries; cam_get(include=['operations']) lists them.")


# An OperationStrategy's classification flags, wire key -> API property. A strategy answers several
# of them, so they are published as read. The short spellings (is2D, isDrilling) are not members of
# cam.OperationStrategy and read as nothing at all.
_STRATEGY_FLAGS = (("is_2d", "is2DStrategy"), ("is_3d", "is3DStrategy"),
                   ("is_drilling", "isDrillingStrategy"), ("is_milling", "isMillingStrategy"),
                   ("is_rotary", "isRotaryStrategy"), ("is_turning", "isTurningStrategy"),
                   ("is_finishing", "isFinishingStrategy"),
                   ("is_additive", "isAdditiveStrategy"), ("is_cutting", "isCuttingStrategy"),
                   ("is_support", "isSupportStrategy"), ("is_suppressible", "isSuppressible"))


def _compatible_strategies(setup):
    """The setup's OperationStrategy objects, or None when the vector would not READ - an
    unreadable vocabulary is not an empty one. OperationStrategyVector is a raw std::vector binding
    (len() and [i] answer, .count and item() do not), so it is walked by ITERATION."""
    return safe(lambda: list(setup.operations.compatibleStrategies))


def _strategy_rows(setup):
    """Every strategy the setup can create, as {name, title, allowed, <classification flags>} -
    'allowed' is isGenerationAllowed. None, never [], when compatibleStrategies did not read."""
    strategies = _compatible_strategies(setup)
    if strategies is None:
        return None
    out = []
    for s in strategies:
        nm = safe(lambda s=s: s.name)
        if not nm:
            continue
        row = {"name": nm, "title": safe(lambda s=s: s.title),
               "allowed": read_flag(lambda s=s: s.isGenerationAllowed)}
        for key, prop in _STRATEGY_FLAGS:
            row[key] = read_flag(lambda s=s, prop=prop: getattr(s, prop))
        out.append(row)
    return out


def read_strategies(setup: str = ""):
    """Every setup's strategy vocabulary with its entitlement tallies. Read-only;
    cam_get(include=['strategies']) is the wire surface, and 'setup' scopes it to one setup through
    the shared find_setup (whose refusal is returned verbatim)."""
    cam, cerr = get_cam()
    if not cam:
        return error(cerr)
    if (setup or "").strip():
        target, _names, serr = find_setup(cam, setup)
        if not target:
            return error(serr)
        targets = [target]
    else:
        targets = setups(cam)
    rows = []
    for s in targets:
        strategies = _strategy_rows(s)
        if strategies is None:
            # A 0 vocabulary here would read as "this setup offers nothing", which is a different
            # fact from "the list did not read" - so every tally is null and the row says which.
            rows.append({"setup": safe(lambda s=s: s.name), "strategies_read": False,
                         "strategy_count": None, "allowed_count": None, "blocked_count": None,
                         "strategies": []})
            continue
        row = {"setup": safe(lambda s=s: s.name),
               "strategy_count": len(strategies),
               "allowed_count": sum(1 for r in strategies if r["allowed"] is True),
               "blocked_count": sum(1 for r in strategies if r["allowed"] is False),
               "strategies": strategies}
        unreadable = sum(1 for r in strategies if r["allowed"] is None)
        if unreadable:
            row["unreadable_count"] = unreadable      # absent = every flag answered
        rows.append(row)
    return ok({"setup_count": len(rows), "setups": rows})


# The refusal for a strategy the setup OFFERS but this license will not generate. Creating one
# succeeds and the operation then carries no toolpath and no error or warning of its own, so this
# guard replaces a SILENT failure rather than a platform raise.
_BLOCKED_STRATEGY = (
    "Strategy '{strategy}' reads isGenerationAllowed false in setup '{setup}', so nothing was "
    "created. Creating it would have SUCCEEDED and then never generated, carrying no toolpath and "
    "no error or warning text of its own. Check this license's Machining Extension entitlement, or "
    "pick one cam_get(include=['strategies'], setup='{setup}') reads as allowed.")

# An unreadable entitlement flag is not a blocked strategy, so no refusal is fabricated from it.
_UNCHECKED_ENTITLEMENT = (
    " The strategy's isGenerationAllowed did not read, so no entitlement pre-flight ran - this "
    "operation may never generate.")

# An unreadable VOCABULARY is not an empty one either: with no rows to check against, createInput
# accepting the strategy is the only gate that ran, and both pre-flights are reported as skipped.
_UNCHECKED_VOCABULARY = (
    " The setup's compatibleStrategies did not read, so neither the compatibility nor the "
    "entitlement pre-flight ran.")

# Names a setup OFFERS, each reading isGenerationAllowed true, that operations.add does NOT land:
# it answers with a name while the setup's operation count stays where it was. Value = what does
# the job instead. The count read is what refuses; this table only names the remedy.
_NOT_AN_OPERATION = {
    "hole_recognition": ("hole recognition picks holes for a drilling cycle - create 'drill' (or "
                         "'bore') and aim it with cam_select_geometry(selection='holes')"),
    "folder": "a CAM folder is created and filled by cam_edit_folders",
}


def _not_an_operation_clause(strategy) -> str:
    """What to reach for instead, for a name that is not an operation - '' for every other
    strategy, whose empty landing is a fault rather than a category error."""
    remedy = _NOT_AN_OPERATION.get(strategy)
    return f" '{strategy}' is not an operation: {remedy}." if remedy else ""

# MEASURED on a milling setup whose Z is the world Z, drilling a hole across it: the generate errors
# on the ORIENTATION, binding Z to that hole's face trades it for a SECOND orientation error, and
# flipping Z clears both. The heights read isEditable true, so they are not what refuses.
_DRILLING_AXIS_NOTE = (
    " A drilling cycle cuts along the SETUP's Z: a hole off that Z errored 'Cylindrical face not "
    "in tool orientation!', and binding Z to that face traded it for 'Selected face may not be "
    "safe for cutting at current tool orientation!'. Both cleared after "
    "cam_edit_setup(wcs={'z_axis': <that face handle>}) then "
    "cam_edit_setup(parameters={'wcs_orientation_flipZ': 'true'}), before selecting the holes.")


def handler(setup: str = "", strategy: str = "", tool_library_url: str = "",
            tool_index: int = -1, tool_scope: str = "", generate: bool = False,
            name: str = "") -> dict:
    """See TOOL_DESCRIPTION."""
    cam, cerr = get_cam()
    if not cam:
        return error(cerr)

    target, _names, serr = find_setup(cam, setup)
    if not target:
        return error(serr)

    # ONE compatibleStrategies walk answers both halves: does the setup offer the strategy, and
    # will this license generate it.
    strategy = (strategy or "").strip()
    rows = _strategy_rows(target)
    chosen = next((r for r in (rows or []) if r["name"] == strategy), None)
    if chosen is None and rows is not None:
        return error(f"Strategy '{strategy}' isn't compatible with setup '{setup}'. "
                     + (f"Compatible: {named_with_remainder([r['name'] for r in rows])}."
                        if rows else "The setup offers no compatible strategies at all."))

    # tool: document library (by index) or a shared library (by url + index)
    tool_scope = (tool_scope or "").strip().lower()
    # Never a bare comparison: the wire can deliver the index as TEXT, and '<' against text raises
    # out of the handler instead of refusing.
    ierr = index_request_error(tool_index)
    if ierr:
        return error(ierr)
    asked_index = tool_index_of(tool_index)
    if tool_scope == "document":
        tool, terr = _doc_tool_at(cam, asked_index)
    elif tool_library_url:
        tool, terr = _tool_at(tool_library_url, asked_index)
    else:
        return error("Provide a tool reference: 'tool_scope=document' + 'tool_index', OR "
                     "'tool_library_url' + 'tool_index' (from cam_edit_tools).")
    if terr:
        return error(terr)

    perr = _probe_tool_refusal(strategy, tool)
    if perr:
        return error(perr)

    # The name is refused BEFORE the add, through the same check the rename arm runs: a deduped
    # name would otherwise land silently and the caller would address the operation by the wrong one.
    clash = _operation_name_clash(cam, name)
    if clash:
        return error(clash)

    # The entitlement pre-flight, LAST before the mutation. `is False`, not a falsiness test: None
    # is the flag that would not read, and refusing on that invents a verdict. No row at all means
    # the vocabulary never read, which is not a verdict either.
    if chosen is not None and chosen["allowed"] is False:
        return error(_BLOCKED_STRATEGY.format(strategy=strategy, setup=setup))

    # createInput can raise on an invalid strategy despite the check (be safe), so guard the mutation.
    try:
        opin = target.operations.createInput(strategy)
    except Exception as e:
        return error(f"createInput('{strategy}') failed: {e}")
    if not opin:
        return error(f"createInput('{strategy}') returned nothing.")
    try:
        opin.tool = tool
    except Exception as e:
        return error(f"Could not assign the tool to a '{strategy}' operation: {e}")

    # SkipGeneration is assigned explicitly rather than relying on the documented default, and read
    # back off the input: the setter can accept the value and keep the default, so an assignment
    # that did not raise is not evidence the input carries the mode.
    mode_set, mode_note = None, None
    if not generate:
        want_mode = adsk.cam.AutomaticGenerationModes.SkipGeneration
        try:
            opin.generationMode = want_mode
            got_mode = safe(lambda: opin.generationMode)
            mode_set = bool(got_mode == want_mode)
            if not mode_set:
                mode_note = (f"generationMode reads back {got_mode!r} after SkipGeneration "
                             f"({want_mode!r}) was assigned.")
        except Exception as e:
            mode_set, mode_note = False, str(e)

    # Measured: a post-add rename (op.name = name) of a bore/circular/thread carrying no faces makes
    # the platform generate it, and that generation's failure parks the call behind a modal. The
    # same create with the name on the INPUT is clean, so the name rides the input.
    want_name = (name or "").strip()
    name_on_input = None
    if want_name:
        try:
            opin.displayName = want_name
            name_on_input = True
        except Exception:
            name_on_input = False

    # counted, not safe(): the landing gate COMPARES these two, and a non-int (an unmodelled
    # property handing back a truthy object) is not a count either side of the add.
    ops_before = counted(lambda: target.operations.count)
    op = target.operations.add(opin)        # MUTATION
    if not op:
        return error("operations.add returned no operation.")
    ops_after = counted(lambda: target.operations.count)
    # A count that does not READ cannot clear the landing, so it is UNCONFIRMED rather than a pass.
    if ops_before is None or ops_after is None:
        unread = " and ".join(word for word, value in (("before", ops_before), ("after", ops_after))
                              if value is None)
        return error(f"operations.add returned '{safe(lambda: op.name)}' but the setup's operation "
                     f"count could not be read {unread} the add, so the operation's landing is "
                     "UNCONFIRMED. Re-read the setup with cam_get(include=['operations']).")
    if ops_after <= ops_before:
        return error(f"operations.add returned '{safe(lambda: op.name)}' but the setup's operation "
                     f"count did not increase ({ops_before} before, {ops_after} after) - the "
                     "operation did not land." + _not_an_operation_clause(strategy))

    # The operation has landed, so a declined name is a DISCLOSURE, not a failed create - the
    # payload publishes the name Operation.name reads back either way.
    if name_on_input:
        op_name, rename_warning = safe(lambda: op.name), None
        if op_name != want_name:
            rename_warning = (f"created, but the requested name '{want_name}' did not take - it is "
                              f"named '{op_name}'.")
    else:
        op_name, rename_warning = apply_rename(op, name)

    # The tool is read off the CREATED operation, never the request echoed back: the assignment was
    # made on the OperationInput, and nothing before this read shows what the operation carries.
    want_desc = _tool_facts(tool)[0]
    carried = safe(lambda: op.tool)
    if carried is None:
        return error(f"Created operation '{op_name}' in setup '{setup}' but Operation.tool reads "
                     "back null - it carries no cutting tool and cannot generate. Assign one with "
                     "cam_edit_operation(tool_scope/tool_library_url, tool_index), or remove it "
                     "with cam_delete.")
    tool_desc, tool_number = _tool_facts(carried)
    named = _names_the_same_tool(tool_desc, want_desc)
    if named is False:
        return error(f"Created operation '{op_name}' in setup '{setup}' but Operation.tool reads "
                     f"{tool_desc!r}, which does not name the requested {want_desc!r} - it carries "
                     "a tool this call did not ask for. Re-assign it with "
                     "cam_edit_operation(tool_scope/tool_library_url, tool_index), or remove it "
                     "with cam_delete.")

    result = {
        "operation": op_name,
        "setup": setup,
        "strategy": strategy,
        "tool": tool_desc,
        "tool_number": tool_number,
        "generation_started": False,
        "note": "Operation created. " + ("" if generate else
                "No toolpath yet: select the geometry it cuts with cam_select_geometry, THEN compute "
                "it (cam_generate, or generate=true here). Generating before the geometry is "
                "selected leaves it reading valid with a selection WARNING and no toolpath "
                "(measured on a 2D Contour)."),
    }
    if mode_note:
        # The read-back is published only when it DISAGREED: absent = generationMode read back
        # SkipGeneration; present = what it read back instead, or why the assignment raised.
        result["generation_mode_note"] = mode_note
    if named is None:
        # absent = both descriptions read and the read-back names the requested tool
        result["tool_identity_checked"] = False

    if generate:
        # The Future MUST be registered, not discarded: if it is garbage-collected Fusion ABANDONS
        # the in-progress generation. _cam_common.register_future is the one registration path, and
        # its handle is what cam_get_status polls.
        gerr, handle = None, None
        try:
            fut = cam.generateToolpath(op)
            if not fut:
                gerr = "generateToolpath returned no future, so no generation is running."
            else:
                handle, _total = register_future(
                    fut, f"operation '{op_name}'", "operation", False,
                    target_name=op_name or "")
        except Exception as e:
            gerr = str(e)
        result["generation_started"] = gerr is None
        if gerr:
            result["generate_error"] = gerr
            result["note"] = f"Operation created but toolpath generation errored: {gerr}"
        else:
            # hasToolpath/isToolpathValid read this early are STALE (the generation is async) -
            # reporting them here would report a false negative, so they are deliberately omitted.
            result["generation_handle"] = handle
            result["note"] = ("Operation created; toolpath generation started (async). Poll it with "
                              f"cam_get_status(handle='{handle}'), or confirm with "
                              "cam_get(include=['operations']) once generation completes.")

    # After the generate arm, which REPLACES the note: the axis rule is what a drilling cycle is
    # aimed by, whichever arm wrote the sentence before it.
    if chosen is not None and chosen.get("is_drilling") is True:
        result["note"] += _DRILLING_AXIS_NOTE
    if strategy == _INSPECT_SURFACE:
        result["note"] += _INSPECT_POINTS_NOTE
    if chosen is None:
        # absent = the read answered and the pre-flight ran, for both keys
        result["strategy_checked"] = False
        result["entitlement_checked"] = False
        result["note"] += _UNCHECKED_VOCABULARY
    elif chosen["allowed"] is None:
        result["entitlement_checked"] = False
        result["note"] += _UNCHECKED_ENTITLEMENT
    if rename_warning:
        result["rename_warning"] = rename_warning
    return ok(result)


TOOL_DESCRIPTION = (
    "Create a CAM milling operation in a setup, with a cutting tool from cam_edit_tools. "
    "Select its geometry with cam_select_geometry, then cam_generate."
)

tool = (
    Tool.create_simple(name="cam_create_operation", description=TOOL_DESCRIPTION)
    .add_input_property("setup", {"type": "string", "description": "Setup name (from cam_get)."})
    .add_input_property("strategy", {"type": "string",
            "description": "e.g. face / adaptive / drill / contour2d."})
    .add_input_property("tool_scope", {"type": "string", "enum": ["document"]})
    .add_input_property("tool_library_url", {"type": "string"})
    .add_input_property("tool_index", {"type": "integer"})
    .add_input_property("generate", {"type": "boolean",
            "description": "Select geometry first."})
    .add_input_property("name", {"type": "string"})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # The synchronous effect only: generate=true launches a background generation this call never
    # reads back. The landing gate is the setup's operation count either side of the add, and
    # Operation.tool is read off the created operation - a disagreement is an error, not a payload.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_cam_create_operation.py::TestToolReadBack"
                      "::test_a_read_back_naming_another_tool_is_refused_and_names_both",
        rung="value"))


def register_tool():
    register(item)
