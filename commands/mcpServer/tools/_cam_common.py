# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Shared CAM substrate: resolves the active document's CAM product and judges job health, for
cam_get and the CAM action/poll tools (cam_get_status, cam_activate_setup, ...) to reuse."""

import collections
import json
import math
import time

import adsk.core
import adsk.cam

from ._common import (counted, measured, named_with_remainder, iter_collection, read_flag, safe,
                      told_apart)
from ._write_guard import _active_identity, document_key, on_key_renamed

MAP_BLURB = (
    "the CAM substrate every CAM tool starts from: get_cam (the document's CAM product); the ONE "
    "tree walk and its EXACT by-name resolvers (walk_cam_tree, resolve_cam_node, find_setup, "
    "find_operation - a duplicate name is refused); the parameter helpers; the per-op state "
    "classifiers and readiness verdicts (op_state_facts, ready_verdict, live_readiness); the "
    "machine catalog; the bounded library walk")

app = adsk.core.Application.get()


def expression_error(p):
    """(error, warning) off a CAM parameter - a broken expression is stored verbatim and its value
    reads back 0.0, so only .error reveals it, while .warning fires on valid expressions too."""
    err = (safe(lambda: p.error) or "").strip()
    warn = (safe(lambda: p.warning) or "").strip()
    # A .warning string can arrive with its template tokens uninterpolated ('${self.title}').
    if "${" in warn:
        warn += " [the ${...} token is an uninterpolated platform template - cosmetic]"
    return (err or None), (warn or None)


def unquote_expression(expr):
    """The VALUE inside a CAM parameter's stored expression - a string parameter stores it
    single-quoted; None stays None and text with no matching outer pair is returned as read."""
    if expr is None:
        return None
    s = str(expr)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def quote_expression(text):
    """The single-quoted expression a CAM string parameter is written as, with each apostrophe in
    the text escaped so the expression stays closed. unquote_expression takes the wrapper back off."""
    return "'" + str(text).replace("'", "\\'") + "'"


# The platform's own refusal for a value outside a CAM enumeration parameter's set, matched
# case-insensitively: Fusion words it '3 : Invalid enumeration value.'
_ENUM_REFUSAL = "invalid enumeration value"


def matched_quoting(current, request):
    """(the expression to WRITE, whether this call added the quotes) - a parameter whose CURRENT
    expression is quoted holds a string, so bare request text is wrapped to match what it stores."""
    text = str(request)
    if current is None or unquote_expression(current) == str(current):
        return text, False                 # nothing quoted to match
    if unquote_expression(text) != text:
        return text, False                 # already quoted as sent
    return quote_expression(text), True


def choice_expressions(p):
    """The values a CHOICE parameter takes, off ChoiceParameterValue.getChoices() - which answers
    (ok, titles, values) - or None where this parameter's value carries no choice set."""
    got = safe(lambda: p.value.getChoices())
    if not got or len(got) < 3 or not got[0]:
        return None
    values = safe(lambda: [str(v) for v in got[2]])
    return values or None


def enumeration_remedy(message, written, read_call, param=None):
    """The clause a platform refusal naming an INVALID ENUMERATION VALUE carries - the expression
    this call actually wrote, then `param`'s own values where getChoices answers them, else the
    `read_call` that shows what it holds. '' for every other message."""
    if _ENUM_REFUSAL not in (message or "").lower():
        return ""
    lead = (f" Fusion's message names an ENUMERATION value; the expression written was {written}. ")
    choices = choice_expressions(param) if param is not None else None
    if choices:
        return lead + f"This parameter's own values: {named_with_remainder(choices)}."
    return lead + f"{read_call} reads the expression this parameter holds - pass one of its own values."


# The operation's own 'strategy' CAM parameter, whose value is the platform's INTERNAL id
# ('parallel_new') - a different vocabulary from Operation.strategy ('parallel'), which is the name
# createInput takes. createInput('contour') raises: 'contour' is not a name in either.
_STRATEGY_PARAM = "strategy"

STRATEGY_PAIR_NOTE = (
    "'strategy' is the operation's own strategy PARAMETER - the platform's internal id - and "
    "'strategy_name' is Operation.strategy. cam_create_operation and "
    "cam_get(include=['strategies']) take strategy_name; the id is not a name either of them "
    "accepts.")


def strategy_pair(op) -> dict:
    """{strategy, strategy_name} for ONE operation: the internal id its 'strategy' parameter holds,
    and the createInput name Operation.strategy reads. Either is null where it did not read."""
    params = safe(lambda: op.parameters)
    p = safe(lambda: params.itemByName(_STRATEGY_PARAM)) if params is not None else None
    return {"strategy": unquote_expression(safe(lambda: p.expression)) if p is not None else None,
            "strategy_name": safe(lambda: op.strategy)}


def clamp_rows(max_results, default: int, ceiling: int) -> int:
    """The row cap a capped CAM read runs under: a non-numeric 'max_results' falls back to
    `default`, and the result is held inside 1..`ceiling`."""
    try:
        n = int(max_results or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, ceiling))


def parse_parameters(parameters):
    """(dict {name: expression}, error) for a 'parameters' request given as a dict or a
    'name=value, ...' string."""
    if isinstance(parameters, dict):
        out = {str(k).strip(): str(v) for k, v in parameters.items() if str(k).strip()}
        return out, None
    if isinstance(parameters, str):
        out = {}
        for chunk in parameters.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                return None, f"'{chunk}' is not 'name=value'. Use name=value pairs, or a JSON object."
            k, _, v = chunk.partition("=")
            if k.strip():
                out[k.strip()] = v.strip()
        return out, None
    return None, "Provide 'parameters' as an object {name: value} or a 'name=value, ...' string."


def get_cam():
    """The active document's CAM product, or (None, reason) - readable in any workspace."""
    doc = safe(lambda: app.activeDocument)
    if not doc:
        return None, "No active document."
    products = safe(lambda: doc.products)
    if products is None:
        return None, "Could not access document products."
    cam = safe(lambda: adsk.cam.CAM.cast(products.itemByProductType('CAMProductType')))
    if not cam:
        return None, ("This document has no CAM (Manufacture) product yet - a fresh design gains "
                      "one on first entry: call view_switch_workspace('manufacture') once, then "
                      "retry this call.")
    return cam, None


def tool_holder(t):
    """A CAM Tool's holder identity {name, product_id, vendor, segment_count}, or None - read out
    of the tool's JSON, which is where adsk.cam.Tool carries it."""
    raw = safe(lambda: t.toJson())
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    h = d.get("holder") if isinstance(d, dict) else None
    if not isinstance(h, dict):
        return None
    out = {}
    if h.get("description"):
        out["name"] = h["description"]
    if h.get("product-id"):
        out["product_id"] = h["product-id"]
    if h.get("vendor"):
        out["vendor"] = h["vendor"]
    segs = h.get("segments")
    if isinstance(segs, list) and segs:
        out["segment_count"] = len(segs)
    return out or None


# key -> the CAM parameter carrying it on an adsk.cam.Tool, each reading a number in cm. All four
# names are present on a bundled milling tool, tool_cornerRadius reading 0 on a square end
# (measure_api cam-tool-dimension-parameter-names).
_TOOL_DIMENSION_PARAMS = (("diameter", "tool_diameter"),
                          ("flute_length", "tool_fluteLength"),
                          ("corner_radius", "tool_cornerRadius"),
                          ("overall_length", "tool_overallLength"))


def tool_dimensions(t, factor, unit):
    """A CAM Tool's cutting geometry {diameter, flute_length, corner_radius, overall_length, units},
    each scaled from cm by 'factor' and null where the parameter is absent or does not read."""
    params = safe(lambda: t.parameters)
    out = {}
    for key, pname in _TOOL_DIMENSION_PARAMS:
        p = safe(lambda pname=pname: params.itemByName(pname)) if params is not None else None
        out[key] = measured(lambda p=p: p.value.value, factor) if p is not None else None
    out["units"] = unit
    return out


def setups(cam):
    """Every Setup in the document, as a list - the basis for the tree walk below."""
    return list(iter_collection(safe(lambda: cam.setups)))


def setup_names(cam):
    """Every setup's name, for a 'not found, available: ...' message - built one way everywhere."""
    return [safe(lambda s=s: s.name) for s in setups(cam)]


# One node of the CAM tree: kind is the collection that yielded it, path the 'Setup / Folder / Op'
# breadcrumb, parent the node that contained it (a name carrying ' / ' splits the path wrong).
CamNode = collections.namedtuple("CamNode", ["obj", "kind", "name", "setup", "path", "parent"],
                                 defaults=(None,))


# The segment a breadcrumb carries for a level whose own name did not read: a None joined into an
# f-string would print the literal 'None' and read as a complete address.
_UNREAD_SEGMENT = "(name unread)"


def _segment(name):
    """One breadcrumb segment: the name, or _UNREAD_SEGMENT when the read answered None."""
    return _UNREAD_SEGMENT if name is None else name


# A parent (Setup/CAMFolder/CAMPattern) keeps its operations, folders and patterns in three
# SEPARATE collections, so only same-kind children of one parent sit in a single ordered list.
CHILD_COLLECTIONS = {"operation": "operations", "folder": "folders", "pattern": "patterns"}


def _walk_children(parent, setup_name, path, out, parent_node=None):
    """Collect CamNodes for everything nested under `parent` (a Setup/CAMFolder/CAMPattern) -
    setup.allOperations DROPS the folder/pattern containers, so those are reached by recursing
    `.folders`/`.patterns`; a parent exposing no `.operations` degrades to that flatten."""
    ops = safe(lambda: getattr(parent, CHILD_COLLECTIONS["operation"]))
    if ops is not None:
        for o in iter_collection(ops):
            nm = safe(lambda o=o: o.name)
            out.append(CamNode(o, "operation", nm, setup_name,
                               f"{path} / {_segment(nm)}", parent_node))
        for kind in ("folder", "pattern"):
            for c in iter_collection(safe(lambda k=kind: getattr(parent, CHILD_COLLECTIONS[k]))):
                nm = safe(lambda c=c: c.name)
                child_path = f"{path} / {_segment(nm)}"
                child_node = CamNode(c, kind, nm, setup_name, child_path, parent_node)
                out.append(child_node)
                _walk_children(c, setup_name, child_path, out, child_node)
        return
    for o in iter_collection(safe(lambda: parent.allOperations)):
        op = adsk.cam.Operation.cast(o)
        if op is not None:
            nm = safe(lambda op=op: op.name)
            out.append(CamNode(op, "operation", nm, setup_name,
                               f"{path} / {_segment(nm)}", parent_node))


def _setup_node(s):
    # The setup is the ROOT segment of every path under it, so it is disclosed the same way: a setup
    # whose name did not read leaves the marker rather than an empty leading segment, which would
    # make ' / Op' read as an operation with no container at all.
    nm = safe(lambda: s.name)
    return CamNode(s, "setup", nm, nm, _segment(nm), None)


def tree_nodes(setup_obj):
    """CamNodes for ONE setup subtree: the setup itself, then every operation/folder/pattern nested
    anywhere under it - the setup-scoped slice of walk_cam_tree."""
    node = _setup_node(setup_obj)
    nodes = [node]
    _walk_children(setup_obj, node.name, node.path, nodes, node)
    return nodes


def walk_cam_tree(cam):
    """Every node of the CAM tree as CamNode(obj, kind, name, setup, path, parent): each Setup plus
    all operations/folders/patterns nested anywhere under it. The ONE traversal every CAM tool walks
    and resolves names over."""
    nodes = []
    for s in setups(cam):
        nodes.extend(tree_nodes(s))
    return nodes


def operation_nodes(cam):
    """Every OPERATION node of the CAM tree - walk_cam_tree filtered to kind == 'operation'."""
    return [n for n in walk_cam_tree(cam) if n.kind == "operation"]


def op_labels(nodes):
    """What each operation row is NAMED by: its own name, or - where several rows in this list share
    that name - its 'Setup / ... / op' path, plus the row's POSITION where that path repeats too.
    The substitution is _common.told_apart: a name that already identifies one row is left alone."""
    per_path = {}
    for n in nodes:
        per_path[n.path] = per_path.get(n.path, 0) + 1
    rows = []
    for position, n in enumerate(nodes, 1):
        disc = n.path
        if disc and per_path[disc] > 1:
            disc = f"{disc} (operation {position})"
        rows.append((n.name, disc))
    return told_apart(rows)


# The address that picks ONE of several nodes sharing a name: '<name>#<n>', n counting from 1 over
# the same-named nodes in the walk's own order - the order the ambiguity refusal lists them in.
_ORDINAL_SEP = "#"


def _ordinal_address(want):
    """('<base name>', <1-based ordinal>) when `want` is spelled '<name>#<n>', else (want, None) -
    split on the LAST separator, so a node whose own name carries one is still addressable."""
    base, sep, tail = want.rpartition(_ORDINAL_SEP)
    if sep and base.strip() and tail.strip().isdigit():
        return base.strip(), int(tail.strip())
    return want, None


def _ordinal_reading(pool, asked):
    """How `asked` reads as a '<name>#<n>' address over `pool`: (the node it addresses or None, the
    base name, the ordinal or None, every node named `base`)."""
    base, ordinal = _ordinal_address(asked)
    if ordinal is None:
        return None, base, None, []
    same = [n for n in pool if (n.name or "").lower() == base.lower()]
    hit = same[ordinal - 1] if 1 <= ordinal <= len(same) else None
    return hit, base, ordinal, same


def _candidate_label(node, ordinal):
    """One row of an ambiguity refusal: this node's '<name>#<n>' address plus its path, or its
    operation count where the node is a setup (whose path is its own bare name)."""
    addr = f"{node.name}{_ORDINAL_SEP}{ordinal}"
    if node.kind != "setup":
        return f"{addr} (at {node.path})"
    n = len(operations_under(node.obj))
    return f"{addr} ({n} operation{'' if n == 1 else 's'})"


def _free_sibling_addresses(pool, same):
    """The '<name>#<n>' addresses over `same` that no node in `pool` CARRIES as a literal name -
    a literal name wins here, so those addresses are no handle on the sibling they count to."""
    carried = {(n.name or "").lower() for n in pool}
    return [addr for addr in (f"{n.name}{_ORDINAL_SEP}{i}" for i, n in enumerate(same, 1))
            if addr.lower() not in carried]


def resolve_cam_node(cam, name, kinds=("operation",), setup=None, label=None, nodes=None):
    """(CamNode, None) for the one node of a kind in `kinds` matching `name` case-insensitively and
    exactly; no hit lists the available names, and 2+ hits are REFUSED with each duplicate's
    '<name>#<n>' address. `setup` scopes the walk, `nodes` reuses a walk the caller already made."""
    if setup is not None:
        nodes = tree_nodes(setup)
    elif nodes is not None:
        pass                                  # the caller's own walk, reused rather than repeated
    elif set(kinds) == {"setup"}:
        nodes = [_setup_node(s) for s in setups(cam)]
    else:
        nodes = walk_cam_tree(cam)
    label = label or "/".join(kinds)
    asked = (name or "").strip()
    want = asked.lower()
    pool = [n for n in nodes if n.kind in kinds]
    matches = [n for n in pool if (n.name or "").lower() == want]
    addressed, base, ordinal, same = _ordinal_reading(pool, asked)
    if not matches:
        # A literal name wins over the ordinal address: the plain match above runs first, so a node
        # actually NAMED 'Face#2' resolves by its own name, and '#n' is read only where no node
        # carries that spelling.
        if addressed is not None:
            return addressed, None
        if ordinal is not None and same:
            rows = [_candidate_label(n, i) for i, n in enumerate(same, 1)]
            return None, (f"'{asked}' addresses item {ordinal} of the {len(same)} named "
                          f"'{base}', which are numbered 1 to {len(same)}. Address one of: "
                          f"{named_with_remainder(rows)}.")
        available = [n.name for n in pool if n.name]
        # Capped by NAME COUNT, with the remainder COUNTED - never by character, which can end the
        # list mid-name and print a spelling no caller can pass back.
        return None, (f"No {label} named '{name}'. Available: "
                      f"{named_with_remainder(available) or '(none)'}.")
    if len(matches) == 1 and addressed is not None and addressed is not matches[0]:
        # Both readings resolve to DIFFERENT nodes and no spelling on this input separates them, so
        # nothing is picked. The ordinal reading dies once fewer than `ordinal` nodes carry `base`,
        # and the renames are ordered highest address first so each one leaves the rest addressable.
        free = _free_sibling_addresses(pool, same)
        need = len(same) - ordinal + 1
        chosen = list(reversed(free[-need:])) if len(free) >= need else []
        remedy = ""
        if need == 1 and chosen:
            remedy = (f" Rename any ONE of the items named '{base}', addressed here as "
                      f"{named_with_remainder(free)}, so fewer than {ordinal} carry '{base}' - "
                      f"'{asked}' then reads only as the {label} carrying it.")
        elif chosen:
            remedy = (f" Rename {need} of the items named '{base}' - {named_with_remainder(chosen)}"
                      f" - in the order given, so fewer than {ordinal} carry '{base}' and '{asked}' "
                      f"then reads only as the {label} carrying it. Take them in that order: each "
                      f"rename leaves one FEWER item named '{base}', and the highest number first "
                      "keeps every address still to come inside the set that remains.")
        return None, (f"'{asked}' reads two ways here: the {label} CARRYING that name, and item "
                      f"{ordinal} of the {len(same)} named '{base}' "
                      f"({_candidate_label(addressed, ordinal)}). Both exist, so the address does "
                      "not identify one." + remedy)
    if len(matches) > 1:
        rows = [_candidate_label(n, i) for i, n in enumerate(matches, 1)]
        return None, (f"'{name}' is ambiguous - {len(matches)} CAM items share that name: "
                      f"{named_with_remainder(rows)}. Retry with one of those '<name>#<n>' "
                      "addresses; the number counts the items of that name in the order listed "
                      "here.")
    return matches[0], None


def operation_nodes_under(node):
    """Every operation CamNode nested under one setup/folder/pattern NODE, each keeping the
    'Setup / ... / op' breadcrumb that separates two operations of one name."""
    nodes = []
    _walk_children(node.obj, node.setup, node.path, nodes, node)
    return [n for n in nodes if n.kind == "operation"]


def operations_under(parent):
    """Every real Operation nested anywhere under one setup/folder/pattern OBJECT; a caller
    naming the results takes operation_nodes_under instead."""
    nodes = []
    _walk_children(parent, None, "", nodes)
    return [n.obj for n in nodes if n.kind == "operation"]


# The walk builds the parent chain, so it is acyclic by construction; the hop cap is only there so
# a hand-built node can never spin the climb below forever.
_MAX_PARENT_HOPS = 64


def owning_setup(node):
    """The Setup OBJECT a CamNode sits under, off the walk's parent links; None where the chain
    does not reach one."""
    seen = 0
    while node is not None and seen <= _MAX_PARENT_HOPS:
        if node.kind == "setup":
            return node.obj
        node = node.parent
        seen += 1
    return None


def find_setup(cam, name):
    """(setup, available_names, error) for the unique Setup named `name` - the error is
    resolve_cam_node's own refusal text, which callers return verbatim."""
    node, err = resolve_cam_node(cam, name, kinds=("setup",), label="setup")
    return (node.obj if node else None), setup_names(cam), err


def walk_operations(cam):
    """Every real Operation across every setup, folder/pattern-nested INCLUDED."""
    return [n.obj for n in operation_nodes(cam)]


def _operation_listing(pool, name):
    """The available list an operation miss carries: each duplicate's 'Setup / op' breadcrumb where
    several share `name`, every operation name otherwise."""
    want = (name or "").strip().lower()
    dupes = [n for n in pool if (n.name or "").lower() == want]
    return [n.path for n in dupes] if len(dupes) > 1 else [n.name for n in pool]


def find_operation(cam, name):
    """(operation, available_names) for the unique Operation named `name` anywhere in the CAM tree;
    a DUPLICATED name is refused and the list carries each duplicate's 'Setup / op' path."""
    pool = operation_nodes(cam)
    want = (name or "").strip().lower()
    matches = [n for n in pool if (n.name or "").lower() == want]
    return (matches[0].obj if len(matches) == 1 else None), _operation_listing(pool, name)


def resolve_operation(cam, name, label="operation"):
    """(node, error, available) - the unscoped operation resolve plus the available list, both off
    ONE walk, for a caller wording its own remedy."""
    pool = operation_nodes(cam)
    node, err = resolve_cam_node(cam, name, kinds=("operation",), label=label, nodes=pool)
    return node, err, _operation_listing(pool, name)


def first_line(text) -> str:
    """The first line of a fault message, '' when there is none."""
    lines = (text or "").strip().splitlines()
    return lines[0] if lines else ""


def first_error_line(obj):
    """First line of an object's .error, '' if none."""
    return first_line(safe(lambda: obj.error))


def first_warning_line(obj):
    """First line of an object's .warning, '' if none."""
    return first_line(safe(lambda: obj.warning))


# The rail-PAIR drive parameter: cam_select_geometry feeds two contours into it, and cam_get_status
# keys its rail triage on carrying it. One name, or a rename leaves the triage pointing at nothing.
SWARF_CONTOURS_PARAM = "swarfContours"


def is_rail_driven(op) -> bool:
    """Whether an operation carries the rail-PAIR drive parameter - what a rail promise is keyed on."""
    return safe(lambda: op.parameters.itemByName(SWARF_CONTOURS_PARAM)) is not None


def op_is_suppressed(facts: dict) -> bool:
    """Whether an operation is SUPPRESSED - answered from EITHER the isSuppressed flag or
    operation_state Suppressed (2), since a raised flag arrives as False."""
    return bool(facts.get("is_suppressed") or facts.get("operation_state") == 2)


def op_settled(facts: dict) -> bool:
    """Whether an operation has nothing left to generate: error or suppressed, or IsValid (0) with a
    toolpath to show for it - hasToolpath True, or the empty class's own flag shape. The isGenerating
    FLAG can read true over such an operation, so no poll settles on that flag alone."""
    if facts.get("has_error") or op_is_suppressed(facts):
        return True
    # MEASURED across one regeneration: the state leaves 0 the instant a launch lands (0 -> 3 -> 1)
    # and returns to 0 only once the work is done, while isGenerating stayed true for a further
    # 1.1 s after the Future completed - so the state leads the flag and cannot complete early.
    if facts.get("operation_state") != 0:
        return False
    # A state-0 op with NO toolpath has produced nothing yet, so the second signal is what separates
    # 'finished' from 'about to start' - is_empty_toolpath covers the op that generated and cuts
    # nothing, which is finished too.
    return facts.get("has_toolpath") is True or is_empty_toolpath(facts)


def unsettled_count(tally: dict) -> int:
    """How many operations a tally is still waiting on: the isGenerating count less the ones whose
    own state has already answered."""
    return max(0, (tally.get("generating", 0) or 0) - (tally.get("generating_settled", 0) or 0))


def settled_clause(tally: dict) -> str:
    """The disclosure for a scope holding operations that read isGenerating true over a state that
    already answered - '' where the two counts agree."""
    n = tally.get("generating_settled", 0) or 0
    if not n:
        return ""
    return (f" {n} operation(s) read isGenerating true while their own state reads valid, errored "
            "or suppressed; this read settles completion on those states, not on the flag.")


def counts_as_warning(facts: dict) -> bool:
    """Whether an op's warning counts toward the readiness overlay - an errored op's warning adds
    nothing to its error, and suppression discards the toolpath, so neither demotes a verdict."""
    return (bool(facts.get("has_warning")) and not facts.get("has_error")
            and not op_is_suppressed(facts))


# The three knobs cam.getMachiningTime takes: feed scale percent, rapid feed cm/s, tool change s.
_TIME_PROBE_ARGS = (100.0, 10.58, 1.5)

# A Manual NC operation carries no toolpath by construction, reading state IsValid (0) with
# hasToolpath False - the empty class's own flag shape. Its op.strategy reads exactly 'manual'.
_MANUAL_NC_STRATEGY = "manual"


def _machining_time(cam, op, state, has_toolpath):
    """The seconds cam.getMachiningTime estimates for ONE operation, None where it was not read."""
    # getMachiningTime raises on an operation holding no toolpath, and costs a platform
    # computation, so it runs only where the flags already say one generated.
    if cam is None or state != 0 or has_toolpath is not True:
        return None
    return measured(lambda: cam.getMachiningTime(op, *_TIME_PROBE_ARGS).machiningTime)


def op_state_facts(op, cam=None) -> dict:
    """ONE safe read of an operation's raw lifecycle state, every classifier here reads from.
    operation_state carries an adsk.cam.OperationStates member: IsValid 0, IsInvalid 1,
    Suppressed 2, NoToolpath 3. `cam` adds machining_time; omitted, it reads None."""
    state = safe(lambda: op.operationState)
    # read_flag keeps True/False/None apart: a coerced False on an unreadable flag would invent
    # the generated-but-empty state is_empty_toolpath reads off this pair.
    has_toolpath = read_flag(lambda: op.hasToolpath)
    return {
        "name": safe(lambda: op.name),
        "strategy": safe(lambda: op.strategy),
        "has_error": bool(safe(lambda: op.hasError, False)),
        "has_warning": bool(safe(lambda: op.hasWarning, False)),
        "is_suppressed": bool(safe(lambda: op.isSuppressed, False)),
        "is_generating": bool(safe(lambda: op.isGenerating, False)),
        "operation_state": state,
        "generating_progress": safe(lambda: op.generatingProgress),
        "has_toolpath": has_toolpath,
        "is_toolpath_valid": read_flag(lambda: op.isToolpathValid),
        "machining_time": _machining_time(cam, op, state, has_toolpath),
    }


def is_empty_toolpath(facts: dict) -> bool:
    """True for the EMPTY class: an operation that generated, is not suppressed, and cuts nothing."""
    # 'valid' is answered off operationState IsValid (0) only, so the empty claim rests on a state
    # that was READ; a state that raised buckets _UNREAD_STATE and stops here.
    if (facts.get("strategy") == _MANUAL_NC_STRATEGY
            or op_primary_state(facts) != "valid"
            or facts.get("is_toolpath_valid") is not True):
        return False
    if facts.get("has_toolpath") is False:
        return True
    # Only a time that READ 0.0 says the operation cut nothing; an unread time is no measurement.
    return facts.get("has_toolpath") is True and facts.get("machining_time") == 0.0


def toolpath_present_tally(ops):
    """(rows reading hasToolpath True, rows whose flag did not read) over a list of operations."""
    # An NC program's filteredOperations holds unsuppressed operations in its scope; hasToolpath
    # separates the ones carrying a path from the ones that read back with none.
    present = unread = 0
    for row in (ops or []):
        flag = read_flag(lambda row=row: row.hasToolpath)
        if flag is None:
            unread += 1
        elif flag:
            present += 1
    return present, unread


def op_state_tally(ops) -> dict:
    """{valid, out_of_date, errored, generating, generating_settled, suppressed, warnings, total,
    active, op_sample, warning_sample} over a list of operations - the poll tally. An ERRORED op is
    its own bucket (it never finishes generating); 'generating' and 'warnings' are overlays on the
    others, and generating_settled counts the flagged ones whose state already answered."""
    valid = ood = errored = generating = suppressed = warnings = total = 0
    generating_settled = 0
    active = None
    op_sample = None
    warning_sample = None
    for raw in (ops or []):
        op = adsk.cam.Operation.cast(raw)
        if op is None:
            continue
        facts = op_state_facts(op)
        total += 1
        if counts_as_warning(facts):
            warnings += 1                            # OVERLAY: the op still lands in a bucket below
            if warning_sample is None:
                warning_sample = {"name": facts["name"], "warning": first_warning_line(op)}
        if facts["has_error"]:
            errored += 1                             # FAILED, not pending - its own bucket
            if op_sample is None:
                op_sample = {"name": facts["name"], "error": first_error_line(op)}
            continue
        state = facts["operation_state"]
        if state == 0:
            valid += 1
        elif state == 2:
            suppressed += 1
        elif state in (1, 3):
            ood += 1
        if facts["is_generating"]:
            generating += 1
            if op_settled(facts):
                generating_settled += 1
            prog = facts["generating_progress"]
            if active is None or (prog and prog not in ("Pending", "0.0%")):
                active = {"op": facts["name"], "progress": prog}
    return {"valid": valid, "out_of_date": ood, "errored": errored, "generating": generating,
            "generating_settled": generating_settled,
            "suppressed": suppressed, "warnings": warnings, "total": total, "active": active,
            "op_sample": op_sample, "warning_sample": warning_sample}


def _warning_phrase(sample) -> str:
    """The 'name - first warning line' clause a readiness verdict names its first warning by."""
    if not sample or not sample.get("name"):
        return "cam_get(include=['operations']) lists which."
    text = (sample.get("warning") or "").strip()
    return f"'{sample['name']}'" + (f" - {text}" if text else " (warning text unreadable).")


# The setup's stock mode: the wire key beside the SetupStockModes member that assigns it.
# 'previous_setup' takes the stock a PRECEDING setup left behind, which is the mill-turn rest flow.
STOCK_MODES = {
    "fixed_box": "FixedBoxStock",
    "relative_box": "RelativeBoxStock",
    "fixed_cylinder": "FixedCylinderStock",
    "relative_cylinder": "RelativeCylinderStock",
    "fixed_tube": "FixedTubeStock",
    "relative_tube": "RelativeTubeStock",
    "from_solid": "SolidStock",
    "previous_setup": "PreviousSetupStock",
}


def stock_mode_member(key):
    """The SetupStockModes value one STOCK_MODES key assigns, or None where this build lacks it."""
    return getattr(adsk.cam.SetupStockModes, STOCK_MODES[key], None)


def stock_mode_name(value):
    """The STOCK_MODES key a Setup.stockMode value reads as, or None when it matches no member."""
    if value is None:
        return None
    return next((key for key in STOCK_MODES if stock_mode_member(key) == value), None)


# The setup-level blocker vocabulary: each code beside the tool that clears it.
_SETUP_BLOCKER_REMEDY = {"no_machine_selected": "cam_edit_setup assigns a machine"}


def setup_blockers(setup) -> list:
    """The verified setup-level blocker codes for ONE setup, present-and-empty when none."""
    return [] if machine_label(safe(lambda: setup.machine)) else ["no_machine_selected"]


def blocked_setup_records(setup_objs) -> list:
    """[{name, blocked_by}] for the setups that carry a blocker - the shape a readiness signal
    publishes and ready_verdict words its clause from. Setups with none are simply absent."""
    rows = []
    for s in setup_objs or []:
        codes = setup_blockers(s)
        if codes:
            rows.append({"name": safe(lambda s=s: s.name), "blocked_by": codes})
    return rows


# How many blocked setups a verdict NAMES; the rest ride as a count, so the sentence stays one line.
_BLOCKED_ROWS_NAMED = 1


def _blocked_phrase(named, total: int) -> str:
    """The 'name (codes)' clause a verdict names its blocked setups by, plus a count of the `total`
    it left out."""
    first = named[0] if named else {}
    name = (first.get("name") or "").strip()
    shown = f"'{name}'" if name else "a setup whose name did not read"
    codes = ", ".join(c for c in (first.get("blocked_by") or []) if c) or "code unreported"
    more = total - len(named)
    return f"{shown} ({codes})" + (f" and {more} more" if more > 0 else "")


def _blocked_remedies(blocked) -> str:
    """The remedy clause for the codes actually PRINTED, deduped in first-seen order; a code with
    no known remedy contributes none."""
    codes = [c for row in (blocked or []) for c in (row.get("blocked_by") or [])]
    remedies = list(dict.fromkeys(_SETUP_BLOCKER_REMEDY[c] for c in codes
                                  if c in _SETUP_BLOCKER_REMEDY))
    return (" " + "; ".join(remedies) + ".") if remedies else ""


def ready_verdict(measure: str, warned: int, warning_sample, blocked=None) -> str:
    """The ONE 'this scope is postable' sentence over the caller's own count clause (`measure`),
    the warned-op count and `blocked` (blocked_setup_records for the caller's setups): a blocker
    withholds the claim, and warnings are stated and named rather than blocking."""
    if blocked:
        named = list(blocked)[:_BLOCKED_ROWS_NAMED]     # the rows the sentence actually prints
        also = f" {warned} active op(s) also carry warnings." if warned else ""
        return (f"{measure}, but {len(blocked)} setup(s) carry blocked_by: "
                f"{_blocked_phrase(named, len(blocked))}"
                f" - 'ready to post' is NOT established.{also}" + _blocked_remedies(named))
    if not warned:
        return f"{measure} - ready to post."
    return (f"{measure}, {warned} with WARNINGS - postable, but read the warnings first: "
            f"{_warning_phrase(warning_sample)}")


# ── strategy entitlement: whether this INSTALLATION will generate a strategy at all ──────────────


def _create_strategy(name):
    """Factored to one line so tests patch this seam."""
    return adsk.cam.OperationStrategy.createFromString(name)


def strategy_generation_allowed(name):
    """True / False / None for ONE strategy NAME's isGenerationAllowed, off the OperationStrategy
    factory - which needs no document, CAM product or setup. None where the name did not build
    (createFromString RAISES on an unknown one) or the flag itself did not read."""
    if not name:
        return None
    strat = safe(lambda: _create_strategy(name))
    if strat is None:
        return None
    return read_flag(lambda: strat.isGenerationAllowed)


def entitlement_flags(ops) -> list:
    """One True / False / None per operation - its own strategy's isGenerationAllowed - probed ONCE
    per distinct strategy name. False is what a generation-blocked operation reads; None is a flag
    that did not read, which is no entitlement verdict at all."""
    seen, out = {}, []
    for op in ops or []:
        name = safe(lambda op=op: op.strategy)
        if name not in seen:
            seen[name] = strategy_generation_allowed(name)
        out.append(seen[name])
    return out


# The bucket for an operation whose operationState never answered: not valid, not finished, and no
# claim about the lifecycle - the honest place for a read that raised.
_UNREAD_STATE = "unread"

# The buckets a blocked operation is NOT waiting on generation in - naming one of these points past
# the operations a scope is actually stuck on. _UNREAD_STATE is not one of them: nothing read says
# that operation is done.
_FINISHED_STATES = ("valid", "suppressed")


def entitlement_blocked_names(ops) -> list:
    """The names of the operations that read isGenerationAllowed False AND still have generating
    left to do - one already reading valid, or suppressed, is not what the scope is waiting on."""
    rows = []
    for op, flag in zip(ops or [], entitlement_flags(ops)):
        if flag is not False or op_primary_state(op_state_facts(op)) in _FINISHED_STATES:
            continue
        rows.append(safe(lambda op=op: op.name) or _UNREAD_SEGMENT)
    return rows


def unfinished_verdict(measure: str, ops) -> str:
    """The readiness verdict for a scope with operations left to generate: the entitlement-blocked
    ones NAMED - cam_generate excludes those from its launch - else the plain cam_generate pointer."""
    names = entitlement_blocked_names(ops)
    if not names:
        return f"{measure} - run cam_generate to finish the rest."
    return (f"{measure}; {len(names)} operation(s) in scope read isGenerationAllowed false and "
            f"cam_generate EXCLUDES those from a launch: {named_with_remainder(names)}. Run "
            "cam_generate for the rest.")


def live_readiness():
    """(signal, None) or (None, reason) - the CAM readiness signal for the active document: the op
    tally, the setup- and NC-program-level errors, setups_blocked, one sample per level, and the
    readiness verdict over them."""
    cam, err = get_cam()
    if err:
        return None, err
    samples = {"op": None, "setup": None, "program": None, "warning": None}
    try:
        ops = walk_operations(cam)
        tally = op_state_tally(ops)
        samples["op"] = tally["op_sample"]
        samples["warning"] = tally["warning_sample"]
        setups_errored = 0
        setup_objs = setups(cam)
        for s in setup_objs:
            if safe(lambda s=s: s.hasError, False):
                setups_errored += 1
                if samples["setup"] is None:
                    samples["setup"] = {"name": safe(lambda s=s: s.name), "error": first_error_line(s)}
        blocked = blocked_setup_records(setup_objs)
        programs_errored = 0
        progs = safe(lambda: cam.ncPrograms)
        for i in range(safe(lambda: progs.count, 0) if progs else 0):
            p = safe(lambda i=i: progs.item(i))
            if p is not None and safe(lambda p=p: p.hasError, False):
                programs_errored += 1
                if samples["program"] is None:
                    samples["program"] = {"name": safe(lambda p=p: p.name), "error": first_error_line(p)}
    except Exception as e:
        return None, str(e)
    valid, ood, errored = tally["valid"], tally["out_of_date"], tally["errored"]
    warned = tally["warnings"]
    active_total = valid + ood + errored          # active = everything not suppressed
    if errored or setups_errored or programs_errored:
        readiness = ("BLOCKER: "
                     + ", ".join(b for b in [
                         f"{setups_errored} setup(s)" if setups_errored else "",
                         f"{programs_errored} NC program(s)" if programs_errored else "",
                         f"{errored} operation(s)" if errored else ""] if b)
                     + " have errors - the job will not post until fixed.")
    elif active_total and valid == active_total:
        readiness = ready_verdict(f"{valid} of {active_total} active ops valid",
                                  warned, samples["warning"], blocked)
    elif active_total:
        readiness = unfinished_verdict(f"{valid} of {active_total} active ops valid", ops)
    else:
        readiness = "no active operations to assess."
    return {"valid": valid, "out_of_date": ood, "errored": errored, "generating": tally["generating"],
            "generating_settled": tally["generating_settled"],
            "suppressed": tally["suppressed"], "warnings": warned, "total": tally["total"],
            "active": tally["active"],
            "setups_errored": setups_errored, "programs_errored": programs_errored,
            "setups_blocked": blocked,
            "readiness": readiness, "samples": samples}, None


def op_primary_state(facts: dict) -> str:
    """The ONE lifecycle bucket an op falls in, priority-ordered so each op counts once: suppressed
    (flag) > error > generating > suppressed (state) > no_toolpath > out_of_date > valid, and
    'unread' where operationState answered nothing. A warning is an OVERLAY, counted separately."""
    if facts["is_suppressed"]:
        return "suppressed"
    if facts["has_error"]:
        return "error"
    # The FLAG outranks the state EXCEPT over an operation reading IsValid with a toolpath to show:
    # it stayed true for 1.1 s past the Future's completion (measured), and 'generating' there is
    # read as unfinished work. Spelled out rather than via op_settled, which reaches back here.
    if facts["is_generating"] and not (facts.get("operation_state") == 0
                                       and facts.get("has_toolpath") is True):
        return "generating"
    if op_is_suppressed(facts):     # the STATE half; the flag half already answered above
        return "suppressed"
    state = facts["operation_state"]
    if state == 3:
        return "no_toolpath"
    if state == 1:
        return "out_of_date"
    if state == 0:
        return "valid"
    # operationState reads None where the property RAISED, and every value it answers IS a state, so
    # anything else is no lifecycle answer at all.
    return _UNREAD_STATE


def validity_basis():
    """'manufacture_verified' iff the Manufacture workspace is active - op validity is only
    trustworthy there - else 'unverified_design_workspace'."""
    try:
        ws = app.userInterface.activeWorkspace
        if ws and ws.id == "CAMEnvironment":
            return "manufacture_verified"
    except Exception:
        pass
    return "unverified_design_workspace"


# ── async generation registry - where every launch path parks its live GenerateToolpathFuture ─────

# Live generations by handle, held for the life of the add-in session. Fusion ABANDONS an
# in-progress generation once its GenerateToolpathFuture is garbage-collected, so an entry is
# popped only after that generation completed.
_GENERATIONS = {}
_HANDLE_SEQ = [0]


def _carry_generation_keys(old_key, new_key):
    """Re-stamp every live generation launched under a document key that just changed - a save
    re-keys an open document."""
    for entry in _GENERATIONS.values():
        if entry.get("doc_key") == old_key:
            entry["doc_key"] = new_key


on_key_renamed(_carry_generation_keys)


def futures_count(futures, member):
    """The int `member` ('numberOfOperations' / 'numberOfCompleted') summed over these Futures, or
    None when any ONE of them did not read - both raise until generation spins up, and a partial
    sum would publish a smaller job than the handle covers."""
    total = 0
    for f in futures or []:
        n = counted(lambda f=f: getattr(f, member))
        if n is None:
            return None
        total += n
    return total


def register_future(future, target, scope, skip_valid, target_name="", also=()):
    """(handle, total) - mint a handle for a live generation Future and register it with the
    document it was launched from and, for a scoped launch, the raw `target_name` a status read
    settles this handle's completion on. `also` holds the SIBLING futures of a launch split across
    operations: every one is kept referenced, and the status read settles on all of them."""
    _HANDLE_SEQ[0] += 1
    handle = f"gen{_HANDLE_SEQ[0]}"
    futures = [future] + list(also)
    total = futures_count(futures, "numberOfOperations")
    doc_name, doc_urn = _active_identity()
    _GENERATIONS[handle] = {
        "future": future,
        "futures": futures,
        "target": target,
        "scope": scope,
        "target_name": (target_name or "").strip(),
        "skip_valid": bool(skip_valid),
        "started_at": time.time(),
        "total": total,
        "doc_name": doc_name,
        "doc_urn": doc_urn,
        "doc_key": document_key(),
        "doc": safe(lambda: app.activeDocument),
    }
    return handle, total


# ── machine library: the locations, the catalog, and the by-name resolver ───────────────────────

# Non-network machine library locations searched for a machine by vendor/model (Fusion360 = the
# bundled sample machines; Local = the user's saved ones). The cloud/network locations are skipped so
# a headless assignment never blocks on a fetch.
_MACHINE_LOCATIONS = ("LocalLibraryLocation", "Fusion360LibraryLocation")

# Machine.capabilities flags -> the 'kind' vocabulary (the bundled library is DOMINATED by
# additive printers, so an unfiltered read floods - machine_type narrows to the relevant kind).
_MACHINE_KINDS = {"milling": "isMillingSupported", "turning": "isTurningSupported",
                  "cutting": "isCuttingSupported", "additive": "isAdditiveSupported"}


def machine_library():
    """The shared MachineLibrary - it hangs off CAMManager.get().libraryManager, not the document's
    CAM product, so no open CAM job is needed. Returns (library, None) or (None, error)."""
    lib = safe(lambda: adsk.cam.CAMManager.get().libraryManager.machineLibrary)
    if lib is None:
        return None, "Could not access the machine library (CAMManager.libraryManager.machineLibrary)."
    return lib, None


def machine_location(lib, machine):
    """'local' when a Local (vendor, model) query answers this machine's id, 'fusion360' by
    elimination, and 'local or fusion360' when that query RAISED. Machine.id is the description, so
    either copy of a shared name reads 'local' - the copy an assignment by that name reaches."""
    vendor, model = (safe(lambda: machine.vendor) or ""), (safe(lambda: machine.model) or "")
    fid = safe(lambda: machine.id)
    try:
        loc = adsk.cam.LibraryLocations.LocalLibraryLocation
        for m in (lib.createQuery(loc, vendor, model).execute() or []):
            if safe(lambda m=m: m.id) == fid:
                return "local"
    except Exception:
        return "local or fusion360"
    return "fusion360"


def machine_kinds(m):
    """ONE machine's 'kind' labels off its capabilities flags - the expensive part of a machine
    row, so a caller needing one machine's kinds calls this instead of walking the catalog."""
    caps = safe(lambda: m.capabilities)
    return [k for k, attr in sorted(_MACHINE_KINDS.items())
            if bool(safe(lambda caps=caps, attr=attr: getattr(caps, attr), False))]


def machine_label(m):
    """Readable machine label: .description, else 'vendor model'. adsk.cam.Machine has no .name."""
    if not m:
        return None
    desc = safe(lambda: m.description)
    if desc:
        return desc
    label = ((safe(lambda: m.vendor) or "") + " " + (safe(lambda: m.model) or "")).strip()
    return label or "(unnamed machine)"


def machine_ident(m):
    """(label, vendor, model) for a Machine - label is the readable name (description or 'vendor model')."""
    return machine_label(m), (safe(lambda: m.vendor) or ""), (safe(lambda: m.model) or "")


# ── the machine's own limits: spindle speed + axis travels, off its kinematics tree ───────────────
# The route is Machine.elements -> the KinematicsMachineElement -> .parts, a TREE whose parts carry
# .children plus an optional .axis / .spindle / .toolStation.
_MACHINE_PART_DEPTH = 8       # the kinematics tree nests one part per axis; bound the recursion
_MACHINE_PART_CAP = 200

# MachineAxis.physicalRange is documented in CM for a linear axis and RADIANS for a rotary one, so
# the axis TYPE decides the unit a travel can be reported in. A build carrying neither member leaves
# the kind None and the range is published unconverted rather than in a guessed unit.
_AXIS_KINDS = (("linear", "LinearMachineAxisType"), ("rotary", "RotaryMachineAxisType"))


def _axis_kind(axis_type):
    """'linear' / 'rotary' for a MachineAxis.axisType value, or None when it matches neither."""
    if axis_type is None:
        return None
    for label, member in _AXIS_KINDS:
        if axis_type == safe(lambda member=member: getattr(adsk.cam.MachineAxisTypes, member)):
            return label
    return None


def _finite(value):
    """A number only when it is FINITE. An unbounded axis range reads -inf/+inf, and infinity is not
    a travel (nor valid JSON for a strict client), so it is dropped in favour of is_infinite."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return value if math.isfinite(value) else None


def kinematics_parts(machine):
    """Every MachinePart under the machine's kinematics element, flattened, or None when the machine
    exposes no kinematics element at all (which is a different answer from a machine whose tree is
    empty). Bounded on depth and part count."""
    elements = safe(lambda: machine.elements)
    if elements is None:
        return None
    type_id = safe(lambda: adsk.cam.KinematicsMachineElement.staticTypeId())
    if not type_id:
        return None
    element = safe(lambda: elements.defaultItemByType(type_id))
    if element is None:
        # defaultItemByType answers only for the element whose id is the default one; a machine that
        # carries the type under another id is still reachable through the filtered list.
        items = safe(lambda: elements.itemsByType(type_id)) or []
        element = items[0] if items else None
    if element is None:
        return None
    out = []
    _walk_machine_parts(safe(lambda: element.parts), 0, out)
    return out


def _walk_machine_parts(parts, depth, out):
    for p in iter_collection(parts):
        if len(out) >= _MACHINE_PART_CAP:
            return
        out.append(p)
        if depth < _MACHINE_PART_DEPTH:
            _walk_machine_parts(safe(lambda p=p: p.children), depth + 1, out)


def _spindle_records(parts) -> list:
    """{max_rpm, min_rpm} for every spindle in the parts list, highest maxSpeed first; a maxSpeed
    of 0 reads None and sorts last."""
    rows = []
    for part in (parts or []):
        sp = safe(lambda part=part: part.spindle)
        if sp is None:
            continue
        rpm = measured(lambda sp=sp: sp.maxSpeed)
        rows.append({"max_rpm": (rpm if rpm else None),
                     "min_rpm": measured(lambda sp=sp: sp.minSpeed)})
    rows.sort(key=lambda r: (r["max_rpm"] is None, -(r["max_rpm"] or 0.0)))
    return rows


def machine_spindle_max(machine):
    """The machine's highest readable spindle maxSpeed in rpm, or None when no spindle answers
    one - the number every per-operation over-max comparison is made against."""
    if machine is None:
        return None
    rows = _spindle_records(kinematics_parts(machine))
    return rows[0]["max_rpm"] if rows else None


def _axis_record(axis, factor, unit):
    """One axis row: its name, kind, and travel in `unit` when the kind says the range is a length."""
    kind = _axis_kind(safe(lambda: axis.axisType))
    rec = {"name": safe(lambda: axis.name), "kind": kind,
           "has_limits": read_flag(lambda: axis.hasLimits)}
    rng = safe(lambda: axis.physicalRange)
    if rng is None:
        return rec
    infinite = read_flag(lambda: rng.isInfinite)
    rec["is_infinite"] = infinite
    # read at full precision and round ONCE, at the end: rounding radians to 6 places first turns
    # a half-turn into 180.00002 deg.
    lo, hi = _finite(measured(lambda: rng.min, 1.0, 12)), _finite(measured(lambda: rng.max, 1.0, 12))
    if lo is None or hi is None:
        return rec
    if kind == "linear":
        rec["travel"] = round((hi - lo) * factor, 6)
        rec["range"] = [round(lo * factor, 6), round(hi * factor, 6)]
        rec["units"] = unit
    elif kind == "rotary":
        rec["travel_deg"] = round(math.degrees(hi - lo), 6)
        rec["range_deg"] = [round(math.degrees(lo), 6), round(math.degrees(hi), 6)]
    else:
        # No decodable axis type: the range is published as the API returned it, with no unit
        # claimed (see _AXIS_KINDS).
        rec["range_raw"] = [lo, hi]
    return rec


def machine_limits(machine, factor, unit) -> dict:
    """{spindle, axes, tool_stations, kinematics_readable} for ONE machine - the spindle speed range
    and per-axis travels its kinematics tree carries."""
    parts = kinematics_parts(machine)
    if parts is None:
        return {"kinematics_readable": False, "spindle": None, "axes": []}
    spindles = _spindle_records(parts)
    out = {"kinematics_readable": True, "spindle": (spindles[0] if spindles else None), "axes": []}
    if len(spindles) > 1:
        out["spindle_count"] = len(spindles)     # 'spindle' is the fastest of them
    stations = []
    for part in parts:
        ax = safe(lambda part=part: part.axis)
        if ax is not None:
            out["axes"].append(_axis_record(ax, factor, unit))
        st = safe(lambda part=part: part.toolStation)
        if st is not None:
            row = {}
            # An UNSET tool-station field reads 0.0, so a zero is never published as a limit. The
            # values are scaled as CM, the unit the bindings document for physicalRange; no machine
            # reading a NON-zero station has been found to confirm it (PROBE NEEDED).
            for key, getter in (("max_tool_diameter", lambda st=st: st.maxToolDiameter),
                                ("max_tool_length", lambda st=st: st.maxToolLength)):
                value = measured(getter, factor)
                if value:
                    row[key] = value
            if row:
                row["units"] = unit
                stations.append(row)
    if stations:
        out["tool_stations"] = stations
    return out


# ── the op-asks-for vs machine-allows comparison the machine slice makes possible ─────────────────

_OP_SPINDLE_PARAM = "tool_spindleSpeed"


def op_spindle_speed(op):
    """The spindle speed ONE operation asks for, off its own tool_spindleSpeed CAM parameter (where
    its tool preset's speed lands), or None when that parameter is absent or does not read."""
    params = safe(lambda: op.parameters)
    if params is None:
        return None
    p = safe(lambda: params.itemByName(_OP_SPINDLE_PARAM))
    if p is None:
        return None
    return measured(lambda: p.value.value)


def spindle_check(op, machine_max):
    """(over, requested, marker) for one operation against its machine's spindle maximum - over is
    True only ABOVE the maximum, and an unreadable side answers None with a marker naming it."""
    if machine_max is None:
        return None, None, "machine_max_unavailable"
    requested = op_spindle_speed(op)
    if requested is None:
        return None, None, "op_spindle_speed_unreadable"
    return requested > machine_max, requested, None


def _walk_machine_locations(lib, vendor, model, visit):
    """Run the (vendor, model) query in each non-network location in turn, handing
    `visit(location_label, matches)` its matches; `visit` returns True to stop the walk."""
    for loc_name in _MACHINE_LOCATIONS:
        loc = getattr(adsk.cam.LibraryLocations, loc_name, None)
        if loc is None:
            continue
        try:
            matches = lib.createQuery(loc, vendor or "", model or "").execute() or []
        except Exception:
            continue
        if visit(loc_name.replace("LibraryLocation", "").lower(), matches):
            return


def query_machines(lib, vendor, model):
    """Run the machine-library query for (vendor, model) across the Local + bundled Fusion360
    locations, deduped by label. Returns a list of (machine, label, vendor, model); the FIRST
    location that yields any match wins (Local before Fusion360)."""
    found, labels = [], set()

    def visit(_loc_label, matches):
        for m in matches:
            label, v, mo = machine_ident(m)
            if label in labels:      # dedupe identical machines that appear in more than one location
                continue
            labels.add(label)
            found.append((m, label, v, mo))
        return bool(found)           # prefer the first location that yields any match

    _walk_machine_locations(lib, vendor, model, visit)
    return found


def machine_catalog(vendor: str = "", machine_type: str = "", max_results: int = 100):
    """(rows, truncated, error) - the machine CATALOG the 'machine' input resolves from: the Local
    and Fusion360 locations, filtered by vendor and/or machine_type."""
    mt = (machine_type or "").strip().lower()
    if mt and mt not in _MACHINE_KINDS:
        return None, False, (f"Unknown machine_type '{machine_type}'. Valid: "
                             f"{', '.join(sorted(_MACHINE_KINDS))}.")
    try:
        lib = adsk.cam.CAMManager.get().libraryManager.machineLibrary
    except Exception as e:
        return None, False, f"Could not access the machine library: {e}"
    rows, total = [], [0]

    def visit(loc_label, matches):
        for m in matches:
            kinds = machine_kinds(m)
            if mt and mt not in kinds:
                continue
            total[0] += 1
            if len(rows) >= max_results:
                continue
            label, v, mo = machine_ident(m)
            rows.append({"name": label, "vendor": v, "model": mo, "location": loc_label,
                         "kind": kinds,
                         "simulation_ready": bool(safe(lambda m=m: m.hasSimulationModel, False))})
        return False                  # every location is listed, so the walk never stops early

    _walk_machine_locations(lib, vendor, "", visit)
    _mark_shared_names(rows)
    return rows, total[0] > len(rows), None


def _mark_shared_names(rows):
    """Flag every listed row whose NAME another location also lists: that name addresses two
    machines, and nothing in a row tells the copies apart. Read over the LISTED rows only."""
    seen = {}
    for r in rows:
        seen.setdefault((r["name"] or "").lower(), set()).add(r["location"])
    for r in rows:
        if len(seen[(r["name"] or "").lower()]) > 1:
            r["name_in_both_locations"] = True   # absent = this name is listed in one location


# WALL-CLOCK budget for the by-description walk, which enumerates both locations UNFILTERED. It is
# checked BETWEEN machines only - an in-flight query cannot be interrupted - and a trip is its own
# refusal, since a catalog not read to the end proves nothing about what it holds.
_MACHINE_WALK_BUDGET_S = 5.0


def _machines_labelled(lib, targets):
    """(machines, timed_out) - the machines whose LABEL is one of `targets`, each a (lower-cased
    label, required lower-cased vendor or None) pair. The library query keys on the MODEL field, so
    a name carried only by a description is reachable only by reading the catalog."""
    found, answered = [], set()
    deadline = time.monotonic() + _MACHINE_WALK_BUDGET_S
    timed_out = [False]

    def visit(_loc_label, matches):
        here = set()
        for m in matches:
            if time.monotonic() > deadline:
                timed_out[0] = True
                return True
            label, v, mo = machine_ident(m)
            key = (label or "").lower()
            hit = [t for t, req_vendor in targets
                   if t == key and (req_vendor is None or req_vendor == (v or "").lower())]
            if not hit or key in answered:
                continue
            here.update(hit)
            found.append((m, label, v, mo))
        answered.update(here)
        return answered == {t for t, _req in targets}

    _walk_machine_locations(lib, "", "", visit)
    return found, timed_out[0]


def _split_ident_line(request):
    """(description, vendor, model) when `request` is a listing LINE handed back as written -
    'description [vendor|model]' - else None. Machines share descriptions and share vendor|model
    pairs, so only the triple addresses one."""
    if not request.endswith("]"):
        return None
    opened = request.rfind(" [")
    if opened <= 0:
        return None
    vendor, sep, model = request[opened + 2:-1].partition("|")
    if not sep:
        return None
    return request[:opened].strip(), vendor.strip(), model.strip()


def _machines_identified(lib, label, vendor, model):
    """The machines whose whole (description, vendor, model) identity is exactly this one, over the
    (vendor, model) query - lower-cased arguments, undeduped so a tie reaches the caller."""
    found = []

    def visit(_loc_label, matches):
        for m in matches:
            lab, v, mo = machine_ident(m)
            if ((lab or "").lower() == label and (v or "").lower() == vendor
                    and (mo or "").lower() == model):
                found.append((m, lab, v, mo))
        return bool(found)           # Local before Fusion360, as every machine read prefers

    _walk_machine_locations(lib, vendor, model, visit)
    return found


def _exact_machine(cands, machine, vendor, model):
    """The single candidate at the first priority yielding exactly one hit - full LABEL, then
    'vendor model', then model - else None. Same-model variants differ only by description, so the
    label is tried first."""
    ml = (model or "").strip().lower()
    ven = (vendor or "").strip().lower()
    full = (machine or "").strip().lower()

    def _unique(pred):
        # Deduped on the whole IDENTITY: two machines can carry one description.
        hits, seen = [], set()
        for tup in cands:
            _m, label, v, mo = tup
            if pred(label, v, mo):
                key = ((label or "").lower(), (v or "").lower(), (mo or "").lower())
                if key not in seen:
                    seen.add(key)
                    hits.append(tup)
        return hits[0] if len(hits) == 1 else None

    return (_unique(lambda label, v, mo: (label or "").lower() == full)                       # label
            or _unique(lambda label, v, mo: ((v or "") + " " + (mo or "")).strip().lower() == full)  # vendor model
            or _unique(lambda label, v, mo: bool(ml) and (mo or "").lower() == ml             # model (+vendor)
                       and (not ven or (v or "").lower() == ven)))


def resolve_machine(machine):
    """(machine, label, None) for a 'machine' string - vendor|model, vendor/model, a bare model, a
    full description, or a listing line handed back - else (None, None, error): nothing matched, or
    the match was ambiguous and this resolver does not guess."""
    machine = (machine or "").strip()
    # The separator split below also runs over a listing LINE and reads half the description as a
    # vendor, so a miss on a parsed line reports what it PARSED.
    ident = _split_ident_line(machine)
    sep = "|" if "|" in machine else ("/" if "/" in machine else "")
    if sep:
        vendor, model = (p.strip() for p in machine.split(sep, 1))
    else:
        vendor, model = "", machine
    try:
        lib = adsk.cam.CAMManager.get().libraryManager.machineLibrary
    except Exception as e:
        return None, None, f"Could not access the machine library: {e}"

    # FIRST: the refusal's own line, handed back - a description and a vendor|model are each
    # shared, so the whole identity is what resolves.
    if ident:
        by_ident = _machines_identified(lib, ident[0].lower(), ident[1].lower(), ident[2].lower())
        if len(by_ident) == 1:
            return by_ident[0][0], by_ident[0][1], None
        if len(by_ident) > 1:
            return None, None, (f"{len(by_ident)} machines carry that exact description, vendor and "
                                f"model ('{machine}'). Nothing read tells them apart, so this "
                                "resolver will not pick one.")

    cands = query_machines(lib, vendor, model)
    # WIDEN when the model as given matches nothing: the query prefix-matches the MODEL field, but a
    # variant's distinguishing text lives in its DESCRIPTION, which is the label callers pass. The
    # broad token fetches the candidate pool that is LABEL-matched below.
    if not cands:
        v2, broad = vendor, model
        if vendor and model.lower().startswith(vendor.lower() + " "):
            broad = model[len(vendor):].strip()               # 'Haas|Haas VF-2' -> model 'VF-2'
        elif not vendor and " " in machine:
            v2, broad = machine.split(" ", 1)                 # bare 'Haas VF-2...' -> vendor 'Haas'
        broad = broad.split(" ", 1)[0].strip() if broad else broad   # first model token ('VF-2')
        v2 = v2.strip()
        if (v2, broad) != (vendor, model) and (v2 or broad):
            widened = query_machines(lib, v2, broad)
            if widened:
                cands, vendor, model = widened, v2, broad
    # LAST: match the DESCRIPTION over one catalog walk, since the query keys on the MODEL field.
    # Two targets: the whole request against the label, and the half after a 'vendor|description'
    # separator against the label of a machine whose vendor is the other half.
    timed_out = False
    if not cands:
        targets = [(machine.lower(), None)] if machine else []
        if sep and model:
            targets.append((model.lower(), vendor.lower()))
        if targets:
            cands, timed_out = _machines_labelled(lib, targets)

    if timed_out:
        # A catalog read that stopped early says nothing about what the catalog holds, so this is
        # never worded as a miss: cam_create_machine reads a miss as proof that a name is free.
        return None, None, (f"Timed out after {_MACHINE_WALK_BUDGET_S:g}s reading the machine catalog "
                            f"for '{machine}', which the Local and Fusion360 queries did not match. "
                            "Whether a machine carries that name is UNKNOWN - retry, or pass "
                            "'vendor|model', which is queried without the catalog read.")
    if not cands:
        if ident:
            return None, None, (f"No machine matches description '{ident[0]}' with vendor|model "
                                f"'{ident[1]}|{ident[2]}' in the Local or Fusion360 machine "
                                "libraries. Pass a line as the refusal that listed it prints it.")
        return None, None, (f"No machine matches '{machine}' (vendor='{vendor}', model='{model}') in the "
                            "Local or Fusion360 machine libraries. Use the machine name (its description) "
                            "you see in the Manufacture machine library.")
    # EXACT match wins BEFORE refusing ambiguity (the house rule).
    exact = _exact_machine(cands, machine, vendor, model)
    if exact is not None:
        return exact[0], exact[1], None
    if len(cands) > 1:
        # Print the whole IDENTITY per row: descriptions and vendor|model pairs are each shared, so
        # either half alone lists a name that refuses again when passed back.
        names, seen = [], set()
        for (_m, lab, v, mo) in cands:
            if not lab:
                continue
            line = f"{lab} [{v}|{mo}]" if (v and mo) else lab
            if line.lower() not in seen:
                seen.add(line.lower())
                names.append(line)
        return None, None, (f"Ambiguous machine '{machine}' - {len(cands)} matches: "
                            f"{', '.join(names[:8])}. Pass the full line as shown.")
    return cands[0][0], cands[0][1], None


# ── CAM LIBRARY folder walks - the ONE traversal the tool / post / template libraries share ──────
# All three expose one shape off a LibraryLocations root url: childFolderURLs nests, and the leaves
# read per kind (childAssetURLs, childTemplates). An unbounded cloud-tree walk hangs the add-in.

_LIBRARY_MAX_DEPTH = 6
_LIBRARY_MAX_FOLDERS = 1500


def library_children(lib, url, kind):
    """One library folder's children of `kind` ('childFolderURLs' / 'childAssetURLs' /
    'childTemplates') as a list - [] when the accessor is absent on this library or the call raises.
    The raise-tolerant read every library walk and leaf op goes through."""
    return list(safe(lambda: list(getattr(lib, kind)(url)), []) or [])


def walk_library_folders(lib, root, visit, max_depth=_LIBRARY_MAX_DEPTH,
                         max_folders=_LIBRARY_MAX_FOLDERS):
    """Depth-first walk of a CAM library's FOLDER tree from `root`, calling `visit(folder_url)` once
    per folder INCLUDING the root; `visit` returns True to stop the walk. Bounded by `max_depth`
    and `max_folders`, and returns True when a cap stopped it early - the read is INCOMPLETE."""
    truncated = [False]
    visited = [0]

    def walk(url, depth):
        if url is None:
            return False
        if depth > max_depth:
            truncated[0] = True
            return False                      # this branch is too deep; siblings may still fit
        if visited[0] >= max_folders:
            truncated[0] = True
            return True                       # the folder budget is spent everywhere, not just here
        visited[0] += 1
        if visit(url):
            truncated[0] = True
            return True
        for f in library_children(lib, url, "childFolderURLs"):
            if walk(f, depth + 1):
                return True
        return False

    walk(root, 0)
    return truncated[0]


def library_assets(lib, root, max_depth=_LIBRARY_MAX_DEPTH, max_folders=_LIBRARY_MAX_FOLDERS,
                   max_assets=None):
    """(assets, truncated) - every child ASSET url under a library location root, folders recursed.
    An asset url is the addressable identity: .leafName is its name, .toString() its url.
    `max_assets` caps the collection; truncated is True when any cap stopped the walk."""
    assets = []

    def visit(url):
        for a in library_children(lib, url, "childAssetURLs"):
            if max_assets is not None and len(assets) >= max_assets:
                return True                   # full - stop the walk, report truncated
            assets.append(a)
        return False

    truncated = walk_library_folders(lib, root, visit, max_depth=max_depth,
                                     max_folders=max_folders)
    return assets, truncated


# ── addressing ONE asset in a library by name - the substrate every library DELETE resolves on ────


def asset_key(url):
    """One asset url's identity for de-duplication - its string form, or the object itself when the
    url does not stringify (two urls for one asset must not read as two candidates)."""
    return safe(lambda: url.toString()) or url


def asset_leaf(url):
    """One asset url's leafName as stored, stripped - '' when it does not read."""
    return (safe(lambda: url.leafName) or "").strip()


def asset_leaf_keys(url):
    """The names ONE asset answers to, lowercased: its leafName as stored, and its STEM - the part
    before the LAST dot, since a stored leafName carries a file extension the asset's own name
    does not."""
    leaf = asset_leaf(url).lower()
    keys = {leaf}
    stem = leaf.rpartition(".")[0]
    if stem:
        keys.add(stem)
    return keys


def assets_named(assets, wanted):
    """The assets one of whose names - leafName as stored, or its stem - EXACTLY matches
    (case-insensitively) one of `wanted`, deduped by url."""
    keys, hits = set(), []
    for a in assets:
        if asset_leaf_keys(a) & wanted:
            key = asset_key(a)
            if key not in keys:
                keys.add(key)
                hits.append(a)
    return hits
