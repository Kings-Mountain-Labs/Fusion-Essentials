# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The live sweep's shared vocabulary: the wire, the step kinds, and the value predicates.

What every act module and the runner build on. The wire is one `call` plus the health and registry
reads. The step KINDS are `Parked`, `_Refusal`/`_refused` and `predicate_kind` - the classifier that
splits the receipt's covered/called buckets by reading a step's expectation OBJECT through a
bytecode guard, so an expectation that touches its payload without reading into it cannot inflate
the ledger. Then the parts every domain's rows are written out of: the ctx threading helper, the
save-slot extractors, the reusable scratch fixtures (`_box`, `_joint_bench`), the camera and dwell
rows, and the value predicates themselves - one per published payload shape, each reporting what it
read through `_measured`, so a miss is diagnosable from the ledger line alone.

`facade`/`bind_facade` are how the runner reaches the names a consumer patches. tool_verify.py
re-exports this whole surface; import it from there.
"""

import argparse
import dis
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import time
import urllib.request
import zlib

import cloud_config

BASE = "http://127.0.0.1:27182"
MCP = BASE + "/mcp"
SERVER_NAME = "Fusion-Essentials MCP Server"
DOC_PREFIX = "EVAL_sweep"

# Two things this run stores OUTSIDE the document it discards: a machine in the LOCAL machine
# library and a template in the LOCAL template library. Each is taken back out by its own teardown
# beat at the end of the CAM deliverables act, in the same branch that created it and judged on
# that delete's own read-backs. Each name carries a run stamp, which guards the window while the
# asset EXISTS: two overlapping runs, or a run that died before its teardown, must not collide on
# one name - the machine create refuses a duplicate, and cam_save_template always writes a NEW
# template, so a repeated template name leaves one more asset in the library per run.
MACHINE_NAME = "SweepMach3Axis " + time.strftime("%Y%m%d-%H%M%S")
TEMPLATE_NAME = "SweepTmpl " + time.strftime("%Y%m%d-%H%M%S")

# How much of a failing step's payload the ledger keeps. A FAIL row is read to DIAGNOSE, and the
# keys that carry the diagnosis (a measured extent, a change list) sit late in a payload - at 160
# characters they were cut off, which costs a whole live re-run to recover. A passing refusal is
# read only as confirmation, so its note stays short.
NOTE_MAX = 480
REFUSAL_NOTE_MAX = 80
# Pause between steps. UNDER MEASUREMENT: at 0.1 it was 121s of a 402s run - 30% of the budget -
# with no recorded reason, against a file whose own doctrine says an async wait is a bounded poll
# "never by a sleep inside the call". Held at 0.0 while three consecutive runs decide whether it
# was hiding a main-thread race; if they are clean the sleep goes, if one flakes the step that
# flaked gets its own bounded poll rather than a blanket pause.
STEP_SLEEP_S = 0.0

# The shell ceiling a run is launched from: a call is killed at 600 s, and a kill leaves no receipt.
# The sweep is not capped to fit it - it is RESUMABLE instead (tool_verify --run <id> / --resume),
# so a program that outgrows one shell call is walked in chunks that share one receipt.
_SHELL_TIMEOUT_S = 600.0

# The pseudo-tool a showcase beat uses to hold a view on screen. Never dispatched to the server and
# never counted as coverage - STEPS filters it out.
_DWELL = "_dwell"

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
SRC_ROOT = os.path.join(REPO_ROOT, "commands", "mcpServer")
VERIFIED = os.path.join(_HERE, "VERIFIED_TOOLS.md")

_FACADE_NS = None


def bind_facade(namespace):
    """tool_verify.py hands its own namespace here as it finishes importing."""
    global _FACADE_NS
    _FACADE_NS = namespace


def facade(name):
    """`name` as the FACADE module holds it now - the one namespace a consumer can reach and patch.

    The step engine's wire calls and the tables run() walks (call, health_gate, ACTS, STORY,
    EXCLUDED, source_hash, ...) are stubbed by setting them ON tool_verify, so the runner resolves
    them there when a run starts rather than binding its own copies at import. sys.modules is asked
    first because tool_verify is ALSO spec-loaded by path (the completeness lint): each spec load builds a second module object that binds itself here, while the
    imported one is the object a consumer holds a reference to."""
    mod = sys.modules.get("tool_verify")
    ns = mod.__dict__ if mod is not None else _FACADE_NS
    if ns is None:
        raise RuntimeError(f"tool_verify is not imported, so {name!r} cannot be resolved - "
                           "import the harness through tool_verify.py, not its parts")
    return ns[name]


def _post(payload, with_session=False):
    req = urllib.request.Request(
        MCP, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        if with_session:
            headers = getattr(resp, "headers", {})
            return body, headers.get("Mcp-Session-Id")
        return body


def call(tool, arguments):
    """One tools/call. Returns (is_error, payload_or_text)."""
    out = _post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": tool, "arguments": arguments}})
    if "error" in out:
        return True, out["error"].get("message", str(out["error"]))
    result = out["result"]
    text = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "")
            break
    if result.get("isError"):
        return True, text
    try:
        return False, json.loads(text)
    except (ValueError, TypeError):
        return False, text


def attestation_identity(health):
    """The complete loaded implementation, schema, and session identity from one health row."""
    attestation = health.get("attestation") if isinstance(health, dict) else None
    fields = ("implementation_fingerprint", "schema_fingerprint", "load_id")
    if (not isinstance(attestation, dict) or not isinstance(health.get("session_id"), str)
            or not health.get("session_id")):
        return None
    if attestation.get("complete") is not True or attestation.get("loaded_matches_source") is not True:
        return None
    if not all(isinstance(attestation.get(field), str)
               and re.fullmatch(r"[0-9a-f]{64}", attestation[field]) for field in fields[:2]):
        return None
    if not isinstance(attestation.get("load_id"), str) or not attestation.get("load_id"):
        return None
    return {"implementation_fingerprint": attestation["implementation_fingerprint"],
            "schema_fingerprint": attestation["schema_fingerprint"],
            "load_id": attestation["load_id"], "session_id": health["session_id"]}


def health_gate():
    with urllib.request.urlopen(BASE + "/health", timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("server") != SERVER_NAME:
        sys.exit(f"Refusing to run: {BASE} is answering as {data.get('server')!r}, "
                 f"not {SERVER_NAME!r}. Is Autodesk's built-in server on this port?")
    if attestation_identity(data) is None:
        sys.exit("Refusing to run: /health has no complete loaded implementation/schema attestation.")
    return data


def _schema_fingerprint(rows):
    """Canonical fingerprint of the tools/list rows the harness consumed."""
    ordered = sorted(rows, key=lambda row: row.get("name", ""))
    body = json.dumps(ordered, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(body.encode("ascii")).hexdigest()


def registered_tools(health=None, include_rows=False):
    out, response_session = _post(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        with_session=True)
    rows = (out.get("result") or {}).get("tools") if isinstance(out, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict)
                                              or not isinstance(row.get("name"), str)
                                              or not row["name"] for row in rows):
        sys.exit("Refusing to run: tools/list did not return a complete tool registry.")
    if health is not None:
        identity = attestation_identity(health)
        if identity is None or response_session != identity["session_id"]:
            sys.exit("Refusing to run: the server session changed between health and tools/list.")
        if _schema_fingerprint(rows) != identity["schema_fingerprint"]:
            sys.exit("Refusing to run: tools/list does not match the attested schema fingerprint.")
    names = sorted(row["name"] for row in rows)
    return (names, tuple(rows)) if include_rows else names


# --- the DAG ----------------------------------------------------------------------------------
# ctx keys written by steps (via "save") and read by later args-callables.

def _ctx_get(ctx, key, what):
    if key not in ctx:
        raise KeyError(f"needs ctx[{key!r}] ({what}) from an earlier step")
    return ctx[key]


class _Refusal:
    """expect=_refused("...", ...) - a deliberate refusal whose MESSAGE must carry every fragment.

    A bare "refused" passes on ANY error, so a guard that starts refusing for a different reason
    (or a call that fails upstream of the guard) still reads green. Where the refusal's own words
    are the measured fact - the build the platform refuses on, the offending value it names - the
    fragments are what make the row assert it. Substring match, ASCII as it crosses the wire."""

    def __init__(self, fragments):
        self.fragments = fragments

    def missing(self, text):
        return [f for f in self.fragments if f not in text]


def _refused(*fragments):
    return _Refusal(fragments)


class Parked:
    """expect=Parked("reason") - a step deliberately left at a bare "ok", with the reason rendered.

    A parked row's reason otherwise lives only in a source comment, so the receipt shows a plain
    'called' and the reader cannot tell a not-yet-written predicate from a deliberately held one.
    Wrapping the expectation carries the reason onto the ledger ('called (reason)') while the
    expectation itself is judged exactly as if it were passed bare. The wrapper is OPTIONAL: a step
    that gains a real predicate drops it, and ``Parked(reason, predicate)`` keeps the reason on a
    step whose expectation is something other than "ok"."""

    def __init__(self, reason, expect="ok"):
        self.reason = reason
        self.expect = expect


class Needs:
    """expect=Needs("<capability>", expect) - a step that RUNS only where that capability is
    entitled.

    The harness probes each declared capability ONCE, at the first row declaring it
    (CAPABILITY_PROBES), and drops the unmet steps before the step engine sees them, so an
    installation without the entitlement
    reports skipped(<capability> not entitled) instead of a red. The expectation inside is judged
    exactly as if it had been passed bare, and an act declares the same thing for all of its steps
    through verify_program.ACT_NEEDS."""

    def __init__(self, capability, expect="ok"):
        self.capability = capability
        self.expect = expect


def _needs(capability, expect="ok"):
    return Needs(capability, expect)


def _unparked(expect):
    """The expectation a step is actually judged by - the innermost expectation of the Parked and
    Needs wrappers, or the expectation itself. Both wrappers carry ledger routing, never a
    judgement, so neither may change what a step is measured against."""
    while isinstance(expect, (Parked, Needs)):
        expect = expect.expect
    return expect


def step_capability(expect):
    """The capability a step DECLARES, or None - read off the expectation object, so the
    declaration travels with the row it gates however the wrappers are nested."""
    while isinstance(expect, (Parked, Needs)):
        if isinstance(expect, Needs):
            return expect.capability
        expect = expect.expect
    return None


def parked_reason(expect):
    """The ledger reason a Parked wrapper carries, or None - read through a Needs wrapper, so a
    step that is both gated and parked still renders its reason."""
    while isinstance(expect, (Parked, Needs)):
        if isinstance(expect, Parked):
            return expect.reason
        expect = expect.expect
    return None


def _machining_extension_probe():
    """True/False/None for the Machining Extension: workspace_orient's own entitlement block, whose
    observed_generation carries one isGenerationAllowed flag per sentinel strategy. Entitled means
    ALL FOUR read true. None is 'the probe could not read it' - a block that did not answer, or any
    flag published null - which is not an entitlement and is reported as such. The flags need no CAM
    product and no setup; the workspace_orient CALL needs an ACTIVE DOCUMENT, so this probe answers
    None until an act has opened one."""
    is_error, payload = facade("call")("workspace_orient", {})
    if is_error or not isinstance(payload, dict):
        return None
    observed = (payload.get("machining_capabilities") or {}).get("observed_generation")
    if not isinstance(observed, dict) or not observed:
        return None
    if any(not isinstance(v, bool) for v in observed.values()):
        return None
    return all(observed.values())


CLOUD_TIER = "cloud_tier"


def _cloud_tier_probe():
    """True/False/None for the opt-in cloud tier: the operator's local config, then the hub, project
    and FOLDER it names, each confirmed before any act can write.

    The folder is checked here and not left to the first create because data_create_folder is
    mkdir -p: a mistyped path does not fail, it MINTS the missing segments in the operator's hub and
    leaves them there when the run's own deletes then miss. So a destination that does not read is
    not entitled - False, not None, because the cost of guessing is folders in someone's hub, and no
    act may reach a create on a maybe. None is kept for the hub read alone, which decides nothing
    about a path. Each answer prints the sentence an operator acts on, since the receipt row carries
    only the verdict."""
    config, problem = cloud_config.load_config()
    if problem:
        print("cloud tier: " + problem)
        return False
    call = facade("call")
    is_error, payload = call("data_get", {})
    if is_error or not isinstance(payload, dict):
        print(f"cloud tier: data_get did not answer, so the hub named in "
              f"{cloud_config.CONFIG_NAME} could not be checked - {str(payload)[:160]}")
        return None
    hub = payload.get("active_hub")
    if hub != config["hub"]:
        print(f"cloud tier: the active hub is {hub!r} and {cloud_config.CONFIG_NAME} names "
              f"{config['hub']!r} - switch hub in Fusion, or fix the config. Nothing was touched.")
        return False
    projects = [str(p.get("name")) for p in (payload.get("projects") or [])]
    if config["project"] not in projects:
        print(f"cloud tier: hub {hub!r} lists no project named {config['project']!r} - "
              f"fix {cloud_config.CONFIG_NAME}. Nothing was touched.")
        return False
    is_error, folder = call("data_get", {"project": config["project"], "folder": config["folder"],
                                         "recursive": False})
    if is_error or not isinstance(folder, dict):
        print(f"cloud tier: project {config['project']!r} does not answer with folder "
              f"{config['folder']!r} - {str(folder)[:160]} The tier stops here rather than at its "
              "first create, because data_create_folder is mkdir -p: it would CREATE the mistyped "
              f"segments in the hub and leave them there. Fix {cloud_config.CONFIG_NAME}; nothing "
              "was created.")
        return False
    return True


# One probe function per capability name. A capability a step or act declares is looked up here at
# the start of a run; a name with no probe answers None and routes as unmet, so a typo cannot read
# as entitled.
CAPABILITY_PROBES = {"machining_extension": _machining_extension_probe,
                     CLOUD_TIER: _cloud_tier_probe}

# Capabilities that are an OPT-IN TIER rather than a licence. A licence capability may never hold
# back a tool's only step - the same tool has to be driven by an ungated one, or an unentitled
# installation loses that tool's coverage. A tier is the opposite: it exists so that tools which
# touch an operator's own cloud data are held back BY DEFAULT, and being skipped is their resting
# state (test_tool_verify_receipt reads this to keep the two invariants apart).
OPT_IN_TIERS = frozenset({CLOUD_TIER})

# One sentence beside a capability's skip verdict - what an operator does to turn the tier on. A
# capability with no entry says only that it is not entitled.
CAPABILITY_DETAIL = {CLOUD_TIER: "opt-in: " + cloud_config.CONFIG_NAME + " names hub, project, folder"}


def probe_capabilities(names, probes=None):
    """{capability: True | False | None} - each name probed ONCE. The probes are wire reads, so this
    is where they are paid for; the runner asks for one at the first act or step declaring it and
    keeps the answer for the rest of the run."""
    probes = CAPABILITY_PROBES if probes is None else probes
    return {name: (probes[name]() if name in probes else None) for name in sorted(set(names))}


def capability_met(entitlements, capability):
    """True where the run may execute a step declaring `capability`. A step declaring nothing is
    always met; an unreadable probe is UNMET, because a flag that did not answer is not a licence."""
    return capability is None or entitlements.get(capability) is True


def capability_skip_reason(capability, entitlements):
    """The receipt's bucket line for a step the capability tier held back - and, when the probe
    itself could not read, the fact that no flag was ever seen. A capability carrying a
    CAPABILITY_DETAIL adds it, so a skipped row says what to do about it rather than only that it
    happened."""
    unread = entitlements.get(capability) is None
    detail = CAPABILITY_DETAIL.get(capability)
    return (f"{capability} not entitled"
            + (" - the capability probe did not read" if unread else "")
            + (f" ({detail})" if detail else ""))


# Bytecode classes for the value-predicate guard below. A PUSH leaves the argument's fate to a later
# instruction; an INSPECT reads INTO the argument (an attribute or a subscript); anything else
# consumes it without looking inside - a truthiness test, an identity compare, a bare call.
_PUSH_OPS = ("LOAD_CONST", "LOAD_SMALL_INT", "LOAD_GLOBAL", "LOAD_FAST", "LOAD_DEREF", "LOAD_NAME",
             "LOAD_CLOSURE", "PUSH_NULL", "MAKE_FUNCTION", "COPY", "NOP", "RESUME", "CACHE",
             "EXTENDED_ARG")
_INSPECT_OPS = ("LOAD_ATTR", "LOAD_METHOD", "BINARY_SUBSCR")


def _is_inspect(ins):
    """True for an instruction that reads INTO the value on top of the stack. 3.11 emits
    LOAD_METHOD where 3.12+ emits LOAD_ATTR, and 3.14 folds the subscript into BINARY_OP - dis
    renders that oparg as '[]', which is what identifies it there."""
    return (ins.opname.startswith(_INSPECT_OPS)
            or (ins.opname == "BINARY_OP" and ins.argrepr == "[]"))


_ARG_LOAD_OPS = ("LOAD_FAST", "LOAD_DEREF")


def _arg_load_sites(instructions, arg):
    """Indexes where `arg` is pushed and is the value left on top. LOAD_FAST carries the name in
    argval; the fused 3.13+ forms (LOAD_FAST_LOAD_FAST) carry a TUPLE of names, and 3.14 renames
    some to LOAD_FAST_BORROW - so the test is on the opname PREFIX and on either argval shape. A
    predicate whose own inner comprehension closes over the payload makes the argument a CELL, and
    every read of it then compiles to LOAD_DEREF, so that form counts too. A fused push that leaves
    a DIFFERENT name on top is skipped: what the next instruction does describes that other value,
    not this one."""
    sites = []
    for i, ins in enumerate(instructions):
        if not ins.opname.startswith(_ARG_LOAD_OPS):
            continue
        val = ins.argval
        if val == arg or (isinstance(val, tuple) and val and val[-1] == arg):
            sites.append(i)
    return sites


# Calls that consume a value while reading only its TRUTHINESS - handing the payload to one of
# these inspects no more of it than `if payload:` would, so it is not a content read.
_TRUTHY_ONLY_CALLS = {"bool"}


def _callee_name(instructions, i):
    """The NAME a call consuming the argument loaded at `i` is calling - the nearest name pushed
    before the argument - or None when the callee is not a plain name (a lambda built inline, a
    subscripted callable)."""
    for j in range(i - 1, -1, -1):
        ins = instructions[j]
        if ins.opname.startswith(("LOAD_GLOBAL", "LOAD_NAME", "LOAD_DEREF", "LOAD_CLOSURE",
                                  "LOAD_ATTR", "LOAD_METHOD")):
            return ins.argval
        if not ins.opname.startswith(_PUSH_OPS):
            return None
    return None


def _inspects_argument(code, arg):
    """True when the bytecode reads the CONTENT of `arg`: the argument is pushed and the first
    instruction that consumes it reads an attribute off it, subscripts it, or hands it to a call
    that is not one of the truthiness-only builtins. Any one such load site is enough."""
    instructions = list(dis.get_instructions(code))
    for i in _arg_load_sites(instructions, arg):
        for nxt in instructions[i + 1:]:
            if nxt.opname.startswith(_PUSH_OPS):
                continue
            if _is_inspect(nxt):
                return True
            if (nxt.opname.startswith("CALL")
                    and _callee_name(instructions, i) not in _TRUTHY_ONLY_CALLS):
                return True
            break
    return False


def _inspects_payload(expect):
    """True when a callable expectation READS the payload it is handed.

    LOADING the argument is not enough: ``lambda p: p and True``, ``bool(p)`` and
    ``p is not None`` all touch the argument while reading NOTHING out of it, and each would
    otherwise be counted 'covered' while proving no more than a bare "ok". So the argument must be
    subscripted, have an attribute read off it, or be handed to a call that reads its content
    (``str(p)``, ``_helper(p)`` - but not ``bool(p)``, which reads only truthiness). A callable
    with no inspectable code object (a builtin, a C callable) is taken at its word."""
    code = getattr(expect, "__code__", None)
    if code is None:
        return True
    if code.co_argcount < 1:
        return False
    return _inspects_argument(code, code.co_varnames[0])


def predicate_kind(expect):
    """Which BUCKET a step's expectation earns its tool: 'value', 'call' or 'refusal'.

    - 'value'   - a callable predicate that reads keys off the ok payload, so a pass is evidence
                  the effect landed (the ledger's 'covered').
    - 'call'    - the bare "ok" string (or a callable that ignores its payload): the call returned
                  without isError and NOTHING in the payload was read (the ledger's 'called').
    - 'refusal' - "refused" / ``_refused(...)``: a guard beat, which produces no effect to read.

    This is what splits the receipt's two honest buckets apart, so it classifies the step's
    expectation OBJECT - never the tool, the args or the run's outcome. A Parked wrapper is
    transparent here: the reason it carries changes the ledger's wording, never the bucket."""
    expect = _unparked(expect)
    if isinstance(expect, _Refusal) or expect == "refused":
        return "refusal"
    if callable(expect):
        return "value" if _inspects_payload(expect) else "call"
    return "call"


# A portable scratch dir for the export-to-disk tools (design_export/mesh_export/cam_post) so the
# sweep writes NC/CAD/mesh files somewhere writable on any machine, not a session-specific path.
EXPORT_DIR = os.path.join(tempfile.gettempdir(), "eval_sweep_exports").replace("\\", "/")
os.makedirs(EXPORT_DIR, exist_ok=True)

# The one fixture the harness AUTHORS rather than builds through a tool: no tool writes an SVG and
# there is no SVG exporter to round-trip through the way mesh_export -> mesh_insert does. 36 x 16 SVG
# user units - importSVG ignores the file's width/height and viewBox, so at scale=3.7795 (1 user unit
# = 1 mm) this art lands about 36 x 16 mm, which is the band the insert beat measures.
SVG_PATH = EXPORT_DIR + "/eval_logo.svg"
with open(SVG_PATH, "w", encoding="utf-8") as _svg_fixture:
    _svg_fixture.write('<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="20mm" '
                       'viewBox="0 0 40 20"><rect x="2" y="2" width="36" height="16"/></svg>')

# The SK-5 closure fixture: a 96-user-unit square at the SVG origin. At 1/96 inch per user unit and
# scale=1 that is exactly one inch, and the art lands y-DOWN from the sketch origin - so the sketch's
# measured min y is -25.4 mm and nothing else can produce that number. ([F30]/[F51c] measured the raw
# entry point; this fixture carries the same landing through the TOOL.)
# NO width/height/viewBox: the probe those facts came from carried none, and a square that exactly
# FILLS a viewBox is the one shape that cannot tell a top-left anchor from a bottom-left one - so
# stating them here would make the fixture disagree with the measurement it exists to close.
SVG96_PATH = EXPORT_DIR + "/eval_square96.svg"
with open(SVG96_PATH, "w", encoding="utf-8") as _svg96_fixture:
    _svg96_fixture.write('<svg xmlns="http://www.w3.org/2000/svg">'
                         '<rect x="0" y="0" width="96" height="96"/></svg>')


def write_png(path, size=64, rgb=(255, 140, 0)):
    """Write a solid-colour PNG from scratch, and return the path - no image library.

    The one raster fixture the harness authors: the file the cloud tier round-trips through an
    operator's hub (a Fusion design cannot be downloaded, so a non-CAD file is the only one that
    comes back) and the image both drawing harnesses place on a sheet."""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * size
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                 + chunk(b"IDAT", zlib.compress(row * size)) + chunk(b"IEND", b""))
    return path


# A STABLE path, rewritten in place on every import: a stamped name would leave one more file in
# EXPORT_DIR per pytest run. The cloud file it becomes needs no stamp either - each run uploads it
# into a run-stamped folder of its own.
MARKER_PNG = write_png(EXPORT_DIR + "/sweep_marker.png")


# save-extractors: pull a handle/profile off a step's payload into ctx for a later args-callable.
def _fg(key):
    return (key, lambda p: p["matches"][0]["handle"])          # find_geometry -> first handle


def _fgn(key):
    return (key, lambda p: [m["handle"] for m in p["matches"]])  # find_geometry -> all handles


def _prof(key):
    return (key, lambda p: p["profiles"][0]["handle"])          # sketch_get -> first profile handle


def _matched(count, kind=None):
    """find_geometry: the query found EXACTLY this many, of this kind. A bare "ok" passes on a
    query that matched NOTHING - the save extractor then fails with an index error a step later,
    and a query that matched the wrong number drills or fillets the wrong set."""
    def check(p):
        ms = p.get("matches") or []
        return _measured(f"find_geometry matched {count}" + (f" {kind}" if kind else ""),
                         {"count": len(ms), "kinds": [m.get("kind") for m in ms[:6]]},
                         len(ms) == count and (kind is None
                                               or all(m.get("kind") == kind for m in ms)))
    return check


def _face_up_at(x, y, z, tol=0.5):
    """find_geometry(kind='planar_face'): the ONE face found, measured by its own 'position' (the
    face centroid) and its outward normal facing +Z.

    A face query is what a hole is drilled through or a selection is aimed at, and 'nearest_to'
    answers with the nearest face whether or not it is the one meant. The normal is the other half:
    an up-facing face has material under it, which is the direction a through-hole is drilled."""
    def check(p):
        ms = p.get("matches") or []
        m = ms[0] if ms else {}
        pos, nrm = m.get("position"), m.get("normal")
        return _measured(f"one up-facing planar face centred near {[x, y, z]}",
                         {"count": len(ms), "position": pos, "normal": nrm, "kind": m.get("kind")},
                         len(ms) == 1 and m.get("kind") == "planar_face"
                         and isinstance(pos, list) and len(pos) == 3
                         and all(_num(v) and abs(v - w) < tol for v, w in zip(pos, (x, y, z)))
                         and isinstance(nrm, list) and len(nrm) == 3 and nrm[2] > 0.999)
    return check


# build_path's published label - "N edge(s) from 1 seed handle" / "N edge(s) from K handles, used
# exactly". N is read off the BUILT adsk Path, so it is the only witness to what was actually swept.
_PATH_LABEL = re.compile(r"^(\d+) edge\(s\) from (\d+) (?:seed handle|handles, used exactly)$")


def _path_count(label, seeds):
    """The built edge count off a path label, or -1 when the label is not that shape or names a
    different seed count - so a beat asserting the number also asserts the wording it came out of."""
    m = _PATH_LABEL.match(str(label or ""))
    if not m or int(m.group(2)) != seeds:
        return -1
    return int(m.group(1))


# A predicate that RAISES names the numbers it read, and run_steps puts that short sentence in the
# ledger instead of the payload - which truncates at 160 characters, well before a measured extent
# or a change list. Use this shape where a miss has to be diagnosable from the ledger alone.
def _measured(label, got, ok_):
    if not ok_:
        raise AssertionError(f"{label}: measured {got}")
    return True


# Values a LATER step's predicate has to compare against. A predicate is handed the payload alone,
# deliberately, so it cannot drift into reading run state - but a before/after check genuinely needs
# the before, and re-reading it at assert time would only compare the model to itself.
_RECALL = {}


def _recall(key, pick):
    """A save-slot extractor that also parks its value under 'key' for a later predicate."""
    def take(payload):
        _RECALL[key] = pick(payload)
        return _RECALL[key]
    return take


# The band the one-inch square is measured against. The nominal is exactly 25.4 mm, but the sketch's
# bounding box spans the imported PAINT, so it carries half the rect's stroke on each side plus the
# importer's own rounding - measured live at min.y -25.41 / height 25.42, a hundredth or two over.
# The band is wide enough to absorb that and far too narrow to admit any other unit reading.
_SVG96_MM = 25.4
_SVG96_TOL = 0.05


def _svg96_extent(p):
    """The 96-user-unit square at scale 1: one inch square, landing Y-DOWN from the sketch origin
    ([F30]/[F51c] measured the raw entry point at y [-2.54 cm, 0]; the live sweep confirms the sign
    and the size through the tool)."""
    e = p.get("sketch_extent") or {}
    got = {"min": e.get("min"), "width": e.get("width"), "height": e.get("height"),
           "units": e.get("units")}
    y = (e.get("min") or {}).get("y")
    return _measured(f"svg96 extent (want min.y {-_SVG96_MM}, height {_SVG96_MM} mm "
                     f"+/-{_SVG96_TOL})", got,
                     y is not None and abs(y + _SVG96_MM) < _SVG96_TOL
                     and e.get("height") is not None
                     and abs(e["height"] - _SVG96_MM) < _SVG96_TOL)


def _repair_no_op(p):
    """A repair that found nothing of its kind: 'changed' empty AND the note saying so."""
    return _measured("stitch_and_remove was expected to be a no-op the second time",
                     {"changed": p.get("changed"), "note": (p.get("note") or "")[:60]},
                     p.get("repaired") is True and p.get("changed") == []
                     and "found nothing of its kind to fix" in (p.get("note") or ""))


# --- model_* value predicates -----------------------------------------------------------------
# Each reads keys the tool PUBLISHES on the call shape its step uses (taken from the tool source),
# so a pass is evidence the effect landed rather than evidence the call returned. Where a step's
# shape carries no honest value to read, the step stays a bare "ok" and its tool lands in the
# receipt's 'called' bucket - the queue for the next tranche. Each check reports the values it read
# through _measured, so a miss is diagnosable from the ledger line alone.

def _made_component(p):
    """model_create_component(activate=True): the new occurrence answers its own name and full path
    back, and 'activated' is what Occurrence.activate() RETURNED, not what the step asked for."""
    return _measured("component create read-back",
                     {"occurrence": p.get("occurrence"), "full_path": p.get("full_path"),
                      "activated": p.get("activated")},
                     bool(p.get("occurrence")) and bool(p.get("full_path"))
                     and p.get("activated") is True)


def _made_component_inactive(p):
    """model_create_component(activate=False): the same read-back, with activate() not called - so
    'activated' must be False rather than merely falsy."""
    return _measured("component create read-back (not activated)",
                     {"occurrence": p.get("occurrence"), "full_path": p.get("full_path"),
                      "activated": p.get("activated")},
                     bool(p.get("occurrence")) and bool(p.get("full_path"))
                     and p.get("activated") is False)


# which component of a datum plane's normal must be the unit one, per base plane
_PLANE_NORMAL_AXIS = {"xy": 2, "xz": 1, "yz": 0}


def _datum_plane(base):
    """model_construction(kind='plane', mode offset from a world plane): the CREATED datum's own
    normal, read back off the object (not echoed), points along the base plane's axis. Sign is not
    asserted - an offset plane's normal may face either way."""
    i = _PLANE_NORMAL_AXIS[base]

    def check(p):
        n = (p.get("geometry") or {}).get("normal")
        aligned = (isinstance(n, list) and len(n) == 3
                   and abs(n[i]) > 0.999
                   and all(abs(v) < 0.001 for j, v in enumerate(n) if j != i))
        return _measured(f"datum plane normal along the {base} plane's axis",
                         {"name": p.get("name"), "normal": n},
                         bool(p.get("name")) and aligned)
    return check


def _dim_measures(mm, tol=0.05):
    """sketch_dimension: the number the dimension MEASURED, in mm. A dimension that attached to the
    neighbouring entity, or read the right one in centimetres, still returns ok - the measured value
    is the only thing that tells those apart. Translation-invariant, so the layout pass moving the
    bench cannot change it."""
    def check(p):
        r = (p.get("results") or [{}])[0]
        parts = (r.get("value") or "").split()
        try:
            got = float(parts[0])
        except (IndexError, ValueError):
            got = None
        return _measured(f"dimension measures {mm} mm",
                         {"dim_type": r.get("dim_type"), "value": r.get("value")},
                         got is not None and abs(got - mm) < tol)
    return check


def _datum(kind):
    """model_construction in any of its geometry-driven build modes: the datum LANDED. It carries
    the kind that was asked for, an entityToken the next call can point AT, and a geometry read off
    the CREATED object - not an echo of the request, which every mode would satisfy identically."""
    def check(p):
        g = p.get("geometry") or {}
        return _measured(f"{kind} datum landed",
                         {"mode": p.get("mode"), "name": p.get("name"), "geometry": g},
                         p.get("created") is True and p.get("kind") == kind
                         and bool(p.get("handle")) and bool(g))
    return check


def _result_bodies(label):
    """The shared feature-landed read for the solid builders: the feature named itself and
    'result_bodies' is the walk over the bodies that feature actually produced."""
    def check(p):
        return _measured(label + " result", {"feature": p.get("feature"),
                                             "result_bodies": p.get("result_bodies")},
                         bool(p.get("feature")) and bool(p.get("result_bodies")))
    return check


_extruded = _result_bodies("extrude")
_revolved = _result_bodies("revolve")
_swept = _result_bodies("sweep")
_lofted = _result_bodies("loft")


def _material_assigned(p):
    """model_set_material: every applied row carries the material name READ BACK off the body after
    the assignment (a silent no-op leaves a name that does not match and lands in 'failed')."""
    rows = p.get("applied_to") or []
    return _measured("material read-back per body",
                     {"applied_to": rows[:3], "failed": p.get("failed")},
                     bool(rows) and all(r.get("material") for r in rows) and not p.get("failed"))


def _gap_measured(p):
    """model_measure_between (distance mode): a real number came back off measureMinimumDistance -
    the tool refuses a non-numeric value rather than publishing 0, so a number here is the
    measurement. The VALUE is not asserted: the gap is whatever the model holds."""
    d = p.get("distance")
    return _measured("minimum distance",
                     {"mode": p.get("mode"), "distance": d,
                      "closest_point_on_a": p.get("closest_point_on_a")},
                     p.get("mode") == "distance" and isinstance(d, (int, float))
                     and not isinstance(d, bool))


def _relation_measured(relation):
    """model_measure_relation: the named relation's own measurement came back as a number and the
    verdict as a boolean. The verdict VALUE is not asserted where the geometry decides it."""
    def check(p):
        ang = (p.get("measured") or {}).get("angle_deg")
        return _measured(f"{relation} measurement",
                         {"relation": p.get("relation"), "measured": p.get("measured"),
                          "passed": p.get("passed")},
                         p.get("relation") == relation and isinstance(p.get("passed"), bool)
                         and isinstance(ang, (int, float)) and not isinstance(ang, bool))
    return check


def _rebuilt(method, density):
    """mesh_repair(repair_type='rebuild'): the method that ran and the density it ran at, the
    latter read off the feature's own ModelParameter. A method the API silently swapped, or a
    density it clamped, differs here - the request alone would agree with itself."""
    def check(p):
        return _measured(f"rebuild ran {method} at density {density}",
                         {"rebuild_method": p.get("rebuild_method"), "density": p.get("density"),
                          "unverified": p.get("density_unverified")},
                         p.get("repaired") is True and p.get("rebuild_method") == method
                         and p.get("density") == density and "density_unverified" not in p)
    return check


def _relation_read(relation, key):
    """model_measure_relation for the relations that do NOT report an angle. Each relation measures
    its own quantity - an angle for the alignments, a distance for the fits - so the key it has to
    publish is named per relation instead of assumed, and a relation answering with someone else's
    measurement fails here."""
    def check(p):
        got = (p.get("measured") or {}).get(key)
        return _measured(f"{relation} measured {key}",
                         {"relation": p.get("relation"), "measured": p.get("measured"),
                          "passed": p.get("passed")},
                         p.get("relation") == relation and isinstance(p.get("passed"), bool)
                         and isinstance(got, (int, float)) and not isinstance(got, bool))
    return check


def _relation_passes(relation):
    """The same read where the step's own geometry settles the verdict (an entity compared with
    ITSELF is parallel to itself), so the boolean is asserted too."""
    inner = _relation_measured(relation)

    def check(p):
        return inner(p) and _measured(f"{relation} verdict", {"passed": p.get("passed"),
                                                              "note": (p.get("note") or "")[:80]},
                                      p.get("passed") is True)
    return check


def _extent_measured(p):
    """model_inspect (default bbox): the three extents came back as POSITIVE numbers off the
    target's bodies-only AABB - a target with no measurable solid reads null instead."""
    x, y, z = p.get("x"), p.get("y"), p.get("z")
    return _measured("bounding-box extents", {"x": x, "y": y, "z": z, "units": p.get("units")},
                     all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
                         for v in (x, y, z)))


def _drafted(p):
    """model_draft: 'faces_drafted' is feature.faces.count - the count the FEATURE reports, null
    when it could not be read."""
    n = p.get("faces_drafted")
    return _measured("faces the draft feature took",
                     {"faces_drafted": n, "faces_requested": p.get("faces_requested")},
                     isinstance(n, int) and not isinstance(n, bool) and n >= 1)


def _drilled(count):
    """model_hole: 'holes_verified' says the DRILL AXES were counted off the created feature (the
    read that catches a point which cut nothing), and 'holes' is that verified count."""
    def check(p):
        return _measured(f"holes drilled (want {count}, axis-verified)",
                         {"holes": p.get("holes"), "holes_verified": p.get("holes_verified"),
                          "points": p.get("points")},
                         p.get("holes_verified") is True and p.get("holes") == count)
    return check


def _mirrored(p):
    """model_mirror: the feature's own result bodies, plus the effect signal the tool measured -
    a body added to the host census, or a volume that moved."""
    return _measured("mirror effect",
                     {"result_bodies": p.get("result_bodies"),
                      "bodies_added": p.get("bodies_added"),
                      "volume_change_cm3": p.get("volume_change_cm3")},
                     bool(p.get("result_bodies"))
                     and bool(p.get("bodies_added") or p.get("volume_change_cm3")))


def _patterned(key, count):
    """model_pattern_rectangular / _circular: the instance count the tool publishes, which is
    feature.patternElements.count when that read answered."""
    def check(p):
        return _measured(f"pattern {key} (want {count})",
                         {key: p.get(key), "feature": p.get("feature"), "type": p.get("type")},
                         p.get(key) == count and bool(p.get("feature")))
    return check


def _joined(p):
    """model_combine(join): the result body's lump count, read FRESH off the joined body - one lump
    is what fusing two touching solids produces, and the tool flags a disjoint join instead."""
    return _measured("join fused into one lump",
                     {"lump_count": p.get("lump_count"), "disjoint_join": p.get("disjoint_join"),
                      "bodies_remaining": p.get("bodies_remaining")},
                     p.get("lump_count") == 1 and not p.get("disjoint_join"))


def _edge_feature(kind):
    """model_fillet / model_chamfer: 'faces_created' is read from the created feature (corner
    patches count too, so it can exceed the edges requested)."""
    def check(p):
        n = p.get("faces_created")
        return _measured(f"{kind} faces created",
                         {"faces_created": n, "edges_requested": p.get("edges_requested"),
                          "feature": p.get("feature")},
                         isinstance(n, int) and not isinstance(n, bool) and n >= 1
                         and bool(p.get("feature")))
    return check


_filleted = _edge_feature("fillet")
_chamfered = _edge_feature("chamfer")


def _shelled(p):
    """model_shell: the before/after volume difference over the shelled body - a hollowing removes
    material, and the key is published only where both reads answered."""
    v = p.get("volume_removed_cm3")
    return _measured("volume the shell removed",
                     {"volume_removed_cm3": v, "faces_delta": p.get("faces_delta"),
                      "result_bodies": p.get("result_bodies")},
                     isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0)


def _offset_faces(p):
    """model_offset_face: the affected body's volume delta - the tool refuses an unchanged volume,
    so a published non-zero delta is the push/pull that landed."""
    v = p.get("volume_delta_cm3")
    return _measured("volume the face offset moved",
                     {"volume_delta_cm3": v, "bodies": p.get("bodies")},
                     isinstance(v, (int, float)) and not isinstance(v, bool) and v != 0)


def _moved(p):
    """model_move: 'displacement' is measured from the bodies' own points before and after, not
    from the request."""
    d = p.get("displacement")
    return _measured("measured displacement",
                     {"displacement": d, "units": p.get("units"), "mode": p.get("mode")},
                     isinstance(d, (int, float)) and not isinstance(d, bool) and d > 0)


def _split_bodies(p):
    """model_split(body): the piece count - the tool errors below 2, and this reads the number."""
    n = p.get("result_count")
    return _measured("pieces the split produced",
                     {"result_count": n, "result_bodies": p.get("result_bodies")},
                     isinstance(n, int) and not isinstance(n, bool) and n >= 2)


def _unstitched(p):
    """model_unstitch: the surface bodies the explode produced, counted off the feature's own
    result, beside the host census before/after."""
    n = p.get("surface_body_count")
    return _measured("surface bodies the unstitch produced",
                     {"surface_body_count": n, "bodies_before": p.get("bodies_before"),
                      "bodies_after": p.get("bodies_after")},
                     isinstance(n, int) and not isinstance(n, bool) and n >= 2)


def _stitched(p):
    """model_stitch: 'became_solid' is the isSolid read-back over the result bodies - a BOOLEAN
    means it was read (null means it would not answer). Whether two faces close into a solid is
    the geometry's business, so the value is not asserted."""
    return _measured("stitch result read-back",
                     {"became_solid": p.get("became_solid"), "is_solid": p.get("is_solid"),
                      "result_bodies": p.get("result_bodies"),
                      "input_body_count": p.get("input_body_count")},
                     isinstance(p.get("became_solid"), bool) and bool(p.get("result_bodies")))


def _base_feature_open(p):
    """model_base_feature(start): the scope reports itself open and registered."""
    return _measured("base-feature scope opened",
                     {"editing": p.get("editing"), "open_scope_count": p.get("open_scope_count"),
                      "base_feature": p.get("base_feature")},
                     p.get("editing") is True and bool(p.get("base_feature"))
                     and isinstance(p.get("open_scope_count"), int) and p["open_scope_count"] >= 1)


def _base_feature_closed(p):
    """model_base_feature(finish): the scope closed - 'editing' False (null is an UNCONFIRMED
    close) and at least one scope named in 'closed_scopes'."""
    return _measured("base-feature scope closed",
                     {"editing": p.get("editing"), "closed_scopes": p.get("closed_scopes"),
                      "open_scope_count": p.get("open_scope_count")},
                     p.get("editing") is False and bool(p.get("closed_scopes")))


def _arranged(count):
    """model_arrange: the solver's effect read back - which named inputs MOVED, or the occurrences
    it added (it restructures parts under Envelope occurrences and can mint copies)."""
    def check(p):
        return _measured(f"arrange effect (want {count} shapes)",
                         {"arranged_count": p.get("arranged_count"), "moved": p.get("moved"),
                          "new_occurrence_count": p.get("new_occurrence_count")},
                         p.get("arranged_count") == count
                         and bool(p.get("moved") or p.get("new_occurrences")))
    return check


def _holder_computed(p):
    """model_compute_holder: the profile reduced to a stack of bands - the segment count and the
    bands themselves."""
    segs = p.get("segments_mm")
    return _measured("holder profile segments",
                     {"segment_count": p.get("segment_count"), "name": p.get("name")},
                     isinstance(segs, list) and len(segs) >= 1
                     and p.get("segment_count") == len(segs))


def _piped(p):
    """model_pipe: the bodies the pipe produced, plus the section size read back off the feature
    (feature.sectionSize.value), not the request echoed."""
    return _measured("pipe result",
                     {"result_bodies": p.get("result_bodies"),
                      "section_size_measured": p.get("section_size_measured"),
                      "section_size": p.get("section_size")},
                     bool(p.get("result_bodies"))
                     and isinstance(p.get("section_size_measured"), (int, float))
                     and not isinstance(p.get("section_size_measured"), bool))


# --- joint_* / assembly_* / param_* / doc_* value predicates -----------------------------------
# Same rule as the model_* block above: each reads keys the tool PUBLISHES on the call shape its
# step uses, and each reports what it read through _measured so a miss is diagnosable from the
# ledger line alone.

def _num(v):
    """True for a real number - a bool is an int in Python and is never a measurement."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _near(v, want, tol):
    return _num(v) and abs(v - want) < tol


def _mod360(a, b):
    """The smaller of the two ways round between two angles in degrees - a revolute's stored value
    ACCUMULATES full turns, so 720 and 0 are one pose (the same test joint_drive's own no-take gate
    applies before it will call a drive landed)."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _jointed(name):
    """joint_create: 'healthy' is the created joint's own compute state, read off the joint or its
    timeline item - null when NEITHER answered, which is not a 'yes' - and 'joint_name' is what
    apply_rename read BACK off the joint rather than the name requested."""
    def check(p):
        return _measured(f"joint create read-back ('{name}')",
                         {"created": p.get("created"), "healthy": p.get("healthy"),
                          "joint_name": p.get("joint_name"), "joint_type": p.get("joint_type"),
                          "input_one": p.get("input_one"), "input_two": p.get("input_two")},
                         p.get("created") is True and p.get("healthy") is True
                         and p.get("joint_name") == name)
    return check


def _mesh_round_trip(width_mm, height_mm, tol=0.5):
    """model_inspect on a re-imported mesh: it comes back the SIZE of the mesh that was written.

    mesh_export defaults stl_units to mm and mesh_insert defaults units to mm, so a file written and
    re-imported with neither naming a unit agrees end to end. The two defaults are set in separate
    tools, and a mismatch is silent: read at the wrong unit, every coordinate is divided (or
    multiplied) by 25.4 and the mesh arrives a fortieth of its size and a fortieth of its distance
    from the origin, which puts it inside whatever is parked there. Only a size check catches that;
    the import returns ok either way.

    A SIZE, not a position: the layout pass moves the bench, and 25.4 is the only thing being looked
    for. 'is_oriented' and 'volume' ride in the evidence unasserted - the round trip comes back
    un-oriented with zero signed volume, and pinning a value here would pin that rather than
    describe it."""
    def check(p):
        b = p.get("bbox") or {}
        return _measured(f"re-imported mesh measures {width_mm} x {height_mm} mm",
                         {"x": b.get("x"), "y": b.get("y"), "z": b.get("z"),
                          "is_closed": p.get("is_closed"), "is_oriented": p.get("is_oriented"),
                          "volume": p.get("volume")},
                         _near(b.get("x"), width_mm, tol) and _near(b.get("y"), width_mm, tol)
                         and _near(b.get("z"), height_mm, tol))
    return check


def _joint_origin_landed(name, z=10.0):
    """joint_create_origin for a bench station. The X/Y it was asked for travel with the layout, and
    a predicate is not shifted with the step it belongs to - so this asserts the axis the layout
    never touches (Z, the height up the base) plus the name and units read back off the created
    origin. Where it sits ALONG the base is proven by the arms landing apart, not by a literal."""
    def check(p):
        loc = p.get("offset_parameters") or {}
        return _measured(f"joint origin '{name}' landed at z={z} mm",
                         {"created": p.get("created"),
                          "joint_origin_name": p.get("joint_origin_name"),
                          "offset_parameters": loc},
                         p.get("created") is True and p.get("joint_origin_name") == name
                         and loc.get("units") == "mm" and _near(loc.get("z"), z, 1e-4))
    return check


def _joint_origin_at(name, x=0.0, y=0.0, z=0.0):
    """joint_create_origin(anchor='coordinates'): 'offset_parameters' holds the created JO's own
    offsetX/Y/Z VALUES read back off it (the tool deletes the origin and errors when they differ
    from what was asked), so this reads where the frame LANDED, not where it was aimed."""
    def check(p):
        loc = p.get("offset_parameters") or {}
        vals = [loc.get(k) for k in ("x", "y", "z")]
        return _measured(f"joint origin '{name}' offsets (want {[x, y, z]} mm)",
                         {"created": p.get("created"),
                          "joint_origin_name": p.get("joint_origin_name"),
                          "offset_parameters": loc,
                          "joint_origin_count": p.get("joint_origin_count")},
                         p.get("created") is True and p.get("joint_origin_name") == name
                         and loc.get("units") == "mm"
                         and all(_near(v, w, 1e-4) for v, w in zip(vals, (x, y, z))))
    return check


def _joint_origin_computed(name):
    """joint_create_origin(a COMPUTED anchor): 'origin_readback' is the created JO's own
    geometry.origin read AFTER the fact, and 'computed_anchor' the point the tool derived from the
    target's measured box - equal within the tool's own 0.01 mm band is the frame landing on the
    anchor it computed. Both keys absent would mean that verification never ran."""
    def check(p):
        want, landed = p.get("computed_anchor") or {}, p.get("origin_readback") or {}
        pairs = [(want.get(k), landed.get(k)) for k in ("x", "y", "z")]
        return _measured(f"joint origin '{name}' landed on its computed anchor",
                         {"computed_anchor": want, "origin_readback": landed,
                          "frame_axes": p.get("frame_axes")},
                         p.get("created") is True and p.get("joint_origin_name") == name
                         and want.get("units") == landed.get("units")
                         and all(_num(a) and _near(b, a, 0.01) for a, b in pairs))
    return check


def _joint_limits(name, **want):
    """joint_edit(limits): every published limit is the value READ BACK off the live JointLimits in
    the caller's own units - a limit whose read-back could not be taken publishes null and is named
    in 'limits_unverified', so an empty 'limits_unverified' beside real numbers is the write
    confirmed."""
    def check(p):
        good = (p.get("edited") is True and p.get("joint_name") == name
                and p.get("recomputed") is True and not p.get("limits_unverified")
                and all(_near(p.get(k), v, 1e-3) for k, v in want.items()))
        return _measured(f"joint '{name}' limits read back {sorted(want)}",
                         {"edited": p.get("edited"), "joint_name": p.get("joint_name"),
                          "changes": p.get("changes"), "recomputed": p.get("recomputed"),
                          "limits_unverified": p.get("limits_unverified")}, good)
    return check


def _motion_linked(one, two, reversed_):
    """joint_motion_link: 'motion_link' is the name read off the CREATED link, 'ratio_applied' says
    setMotionData answered true (a ratio that would not apply rolls the link back and errors), and
    joint_one/joint_two are read off the two joints the names resolved to."""
    def check(p):
        return _measured(f"motion link '{one}' -> '{two}'",
                         {"linked": p.get("linked"), "motion_link": p.get("motion_link"),
                          "joint_one": p.get("joint_one"), "joint_two": p.get("joint_two"),
                          "ratio": p.get("ratio"), "reversed": p.get("reversed")},
                         p.get("linked") is True and p.get("ratio_applied") is True
                         and bool(p.get("motion_link"))
                         and p.get("joint_one") == one and p.get("joint_two") == two
                         and p.get("reversed") is reversed_)
    return check


def _driven_angle(want_deg):
    """joint_drive: 'value_now.angle_deg' is jointMotion.rotationValue read BACK off the joint after
    the drive, compared the way the tool's own no-take gate compares it (modulo 360). 'moved' names
    the member whose placement changed and is reported, not asserted - which member moves is the
    mechanism's business."""
    def check(p):
        v = (p.get("value_now") or {}).get("angle_deg")
        return _measured(f"drive read-back (want {want_deg} deg)",
                         {"driven": p.get("driven"), "joint": p.get("joint"),
                          "value_now": p.get("value_now"), "moved": p.get("moved"),
                          "motion_link_partner": p.get("motion_link_partner")},
                         p.get("driven") is True and _num(v) and _mod360(v, want_deg) < 0.5)
    return check


def _driven_slide(want_mm):
    """joint_drive (a slider): 'value_now.distance_mm' is jointMotion.slideValue read back off the
    joint in mm - the tool errors when it disagrees with the command, so the number here IS the
    pose the slide took."""
    def check(p):
        v = (p.get("value_now") or {}).get("distance_mm")
        return _measured(f"slide read-back (want {want_mm} mm)",
                         {"driven": p.get("driven"), "joint": p.get("joint"),
                          "value_now": p.get("value_now"), "moved": p.get("moved")},
                         p.get("driven") is True and _near(v, want_mm, 1e-3))
    return check


def _as_built(p):
    """joint_create_as_built: 'joint' is the name read off the CREATED AsBuiltJoint (a rename that
    did not land is refused rather than published), and the two occurrence paths are read off the
    occurrences the inputs resolved to."""
    return _measured("as-built joint read-back",
                     {"created": p.get("created"), "joint": p.get("joint"),
                      "occurrence_one": p.get("occurrence_one"),
                      "occurrence_two": p.get("occurrence_two"),
                      "joint_type": p.get("joint_type"),
                      "anchor_warning": p.get("anchor_warning")},
                     p.get("created") is True and bool(p.get("joint"))
                     and bool(p.get("occurrence_one")) and bool(p.get("occurrence_two")))


def _jointed_at_geometry(p):
    """joint_at_geometry: 'healthy' is the created joint's compute state - the payload's own
    authoritative flag, false when the joint was added but could not solve - beside the key points
    the two handles resolved to."""
    return _measured("joint-at-geometry read-back",
                     {"jointed": p.get("jointed"), "healthy": p.get("healthy"),
                      "health_state": p.get("health_state"), "joint_name": p.get("joint_name"),
                      "geometry_one": p.get("geometry_one"),
                      "geometry_two": p.get("geometry_two")},
                     p.get("jointed") is True and p.get("healthy") is True
                     and bool(p.get("geometry_one")) and bool(p.get("geometry_two")))


def _grounded(p):
    """assembly_ground(ground_to_parent=true): 'isGroundToParent' is the flag READ BACK off the
    occurrence after the set - the tool errors on a flag that will not read or does not match, so a
    true here is the lock in force. 'isGrounded' is the separate UI flag this tool never writes
    (read_flag: null means unread)."""
    return _measured("parent lock read-back",
                     {"occurrence": p.get("occurrence"),
                      "isGroundToParent": p.get("isGroundToParent"),
                      "isGrounded": p.get("isGrounded"),
                      "position_reset": p.get("position_reset")},
                     p.get("isGroundToParent") is True and bool(p.get("occurrence")))


def _moved_occurrence(x_mm=None):
    """assembly_move: 'position' is the occurrence's transform2 translation READ BACK after the
    compose, in the caller's units - the tool errors when the transform reads unchanged or will not
    read at all, so these are the coordinates the part actually holds. x is asserted only where the
    step is the FIRST move of a fresh occurrence, whose transform starts at the identity."""
    def check(p):
        pos = p.get("position") or {}
        vals = [pos.get(k) for k in ("x", "y", "z")]
        good = (p.get("moved") is True and bool(p.get("occurrence"))
                and all(_num(v) for v in vals))
        if good and x_mm is not None:
            good = _near(pos.get("x"), x_mm, 1e-3)
        return _measured("moved occurrence position"
                         + ("" if x_mm is None else f" (want x {x_mm})"),
                         {"moved": p.get("moved"), "occurrence": p.get("occurrence"),
                          "position": pos, "units": p.get("units")}, good)
    return check


def _rigid_grouped(count):
    """assembly_rigid_group: 'member_count' is rigidGroup.occurrences.count read off the CREATED
    group - the tool errors when it comes back short of the occurrences it resolved."""
    def check(p):
        return _measured(f"rigid group members (want {count})",
                         {"assembly_rigid_group": p.get("assembly_rigid_group"),
                          "member_count": p.get("member_count"), "grouped": p.get("grouped")},
                         p.get("member_count") == count
                         and bool(p.get("assembly_rigid_group"))
                         and len(p.get("grouped") or []) == count)
    return check


def _constrained(p):
    """assembly_constrain: 'relationship_count' is the count read off the CREATED constraint (null
    when it would not read), 'constraint' its name read back, and 'moved' is the world-transform
    diff sampled either side of the add - a LIST (empty is a real answer: nothing moved) when the
    parts were sampled on both sides, null when whether anything moved is unknown."""
    n = p.get("relationship_count")
    return _measured("assembly constraint read-back",
                     {"created": p.get("created"), "constraint": p.get("constraint"),
                      "relationship_count": n,
                      "relationships_submitted": p.get("relationships_submitted"),
                      "moved": p.get("moved"), "occurrences": p.get("occurrences")},
                     p.get("created") is True and bool(p.get("constraint"))
                     and _num(n) and n >= 1 and isinstance(p.get("moved"), list))


def _captured(p):
    """assembly_capture_position(capture): 'snapshot_count' is the collection's own re-read (the
    tool errors when it did not advance), 'snapshot' the marker's name, and 'pose_held' is True only
    where a transform was sampled on BOTH sides of the add - a capture that REVERTED the pending
    move instead of recording it is an error, and an unsampled one publishes null."""
    n = p.get("snapshot_count")
    return _measured("captured position read-back",
                     {"captured": p.get("captured"), "snapshot": p.get("snapshot"),
                      "snapshot_count": n, "pose_held": p.get("pose_held")},
                     p.get("captured") is True and bool(p.get("snapshot"))
                     and _num(n) and n >= 1 and p.get("pose_held") is True)


def _joint_is(name, kind):
    """assembly_get: one joint's motion type, read off the DESIGN's own joint walk. joint_edit
    publishes the type it was ASKED for, so a retype that silently did not land reads back correct
    from the writer and wrong from here - which is the whole point of asking someone else."""
    def check(p):
        row = next((j for j in (p.get("joints") or []) if j.get("name") == name), None)
        return _measured(f"{name} reads back as {kind}",
                         {"joint": row and {"name": row.get("name"), "type": row.get("type")}},
                         bool(row) and row.get("type") == kind)
    return check


def _joints_listed(minimum, poses=None):
    """assembly_get (default read): 'joint_count' is the design-wide joint walk's own total, and a
    named joint's 'value_now.angle_deg' is its jointMotion's current driven value read straight off
    the joint - an INDEPENDENT witness to a pose joint_drive reported for itself."""
    poses = poses or {}

    def check(p):
        rows = {j.get("name"): j for j in (p.get("joints") or [])}
        angles = {n: (rows.get(n, {}).get("value_now") or {}).get("angle_deg") for n in poses}
        good = (_num(p.get("joint_count")) and p.get("joint_count") >= minimum
                and isinstance(p.get("is_healthy"), bool)
                and all(_num(angles[n]) and _mod360(angles[n], w) < 0.5
                        for n, w in poses.items()))
        return _measured(f"assembly joints (want >= {minimum}, poses {poses})",
                         {"joint_count": p.get("joint_count"), "is_healthy": p.get("is_healthy"),
                          "value_now": angles, "broken_joints": p.get("broken_joints")}, good)
    return check


def _joint_origins_listed(*names):
    """assembly_get(include=['joint_origins']): an INDEPENDENT read of the frames
    joint_create_origin built - each named JO present in the design-wide JO walk, carrying the
    handle that round-trips back into a joint or a CAM WCS input."""
    def check(p):
        rows = {r.get("name"): r for r in (p.get("joint_origins") or [])}
        missing = [n for n in names if n not in rows]
        return _measured("joint origins listed " + str(list(names)),
                         {"joint_origin_count": p.get("joint_origin_count"),
                          "names": sorted(n for n in rows if n)[:12], "missing": missing},
                         _num(p.get("joint_origin_count"))
                         and p.get("joint_origin_count") >= len(names) and not missing
                         and all(rows[n].get("handle") for n in names))
    return check


def _interference_measured(p):
    """assembly_inspect_interference: the census the check actually ran - how many occurrences and
    root bodies it compared, and the rows it found, with the count agreeing with the list. The
    VERDICT is not asserted: whether parts overlap is the model's business."""
    m = p.get("measured") or {}
    items = m.get("interferences")
    return _measured("interference census",
                     {"relation": p.get("relation"), "passed": p.get("passed"),
                      "interference_count": m.get("interference_count"),
                      "occurrences_checked": m.get("occurrences_checked"),
                      "root_bodies_checked": m.get("root_bodies_checked")},
                     p.get("relation") == "interference_free"
                     and isinstance(p.get("passed"), bool) and isinstance(items, list)
                     and m.get("interference_count") == len(items)
                     and _num(m.get("occurrences_checked")) and m.get("occurrences_checked") >= 1)


# A parameter's value read back through unitsManager.convert lands on the nominal to well within
# this; the band is far too narrow to admit a different unit or a different expression's result.
_PARAM_TOL = 1e-4


def _param_added(name, value, units="mm"):
    """param_add: 'parameter' is the created Parameter re-read - the name it answers to and the
    value Fusion EVALUATED its expression to, in that parameter's own unit ('value_units'), never
    the request echoed. A derived expression's value is therefore the design's own arithmetic."""
    def check(p):
        par = p.get("parameter") or {}
        return _measured(f"param add read-back '{name}' (want {value} '{units}')",
                         {"added": p.get("added"), "name": par.get("name"),
                          "expression": par.get("expression"), "value": par.get("value"),
                          "value_units": par.get("value_units"),
                          "timeline_warnings": p.get("timeline_warnings")},
                         p.get("added") is True and par.get("name") == name
                         and par.get("value_units") == units
                         and _near(par.get("value"), value, _PARAM_TOL))
    return check


def _param_set_to(name, value):
    """param_set: 'before'/'after' are the SAME parameter read either side of the assignment (the
    tool errors when the two are identical while the expression asked for a change), so the moved
    value is the edit landing rather than the expression being accepted."""
    def check(p):
        before, after = p.get("before") or {}, p.get("after") or {}
        return _measured(f"param set read-back '{name}' (want {value})",
                         {"set": p.get("set"), "created": p.get("created"), "name": p.get("name"),
                          "before": {"expression": before.get("expression"),
                                     "value": before.get("value")},
                          "after": {"expression": after.get("expression"),
                                    "value": after.get("value")}},
                         p.get("set") is True and p.get("created") is False
                         and p.get("name") == name
                         and _near(after.get("value"), value, _PARAM_TOL)
                         and before.get("value") != after.get("value"))
    return check


def _param_deleted(name):
    """param_delete: 'deleted' is what Parameter.deleteMe() RETURNED - the tool errors on a false
    and on a delete that introduced a timeline error, so a true here is the parameter gone with the
    timeline still walking clean."""
    def check(p):
        return _measured(f"param delete read-back '{name}'",
                         {"deleted": p.get("deleted"), "name": p.get("name"),
                          "note": (p.get("note") or "")[:60]},
                         p.get("deleted") is True and p.get("name") == name)
    return check


def _params_listed(*names):
    """param_get (collection): the AUTHORED user parameters the design answered with - 'returned'
    counts the rows actually read and 'user_parameter_count' the whole set the design holds, so a
    page can never claim to be the design; every named parameter is present carrying its value."""
    def check(p):
        rows = p.get("user_parameters") or []
        by_name = {r.get("name"): r for r in rows}
        missing = [n for n in names if n not in by_name]
        return _measured(f"user parameters listed (want {list(names)})",
                         {"user_parameter_count": p.get("user_parameter_count"),
                          "returned": p.get("returned"), "rows_read": len(rows),
                          "generated_skipped": p.get("generated_skipped"), "missing": missing,
                          "names": sorted(n for n in by_name if n)[:12]},
                         p.get("returned") == len(rows)
                         and _num(p.get("user_parameter_count"))
                         and p.get("user_parameter_count") >= len(rows) and not missing
                         and all(_num(by_name[n].get("value")) for n in names))
    return check


def _param_read(name, value, units="mm"):
    """param_get (one name): the parameter's evaluated value in its own unit, read off the design
    long after the add - the same number the adding step measured, now on the read side."""
    def check(p):
        par = p.get("parameter") or {}
        return _measured(f"param read '{name}' (want {value} '{units}')",
                         {"name": par.get("name"), "expression": par.get("expression"),
                          "value": par.get("value"), "value_units": par.get("value_units")},
                         par.get("name") == name and par.get("value_units") == units
                         and _near(par.get("value"), value, _PARAM_TOL))
    return check


def _param_favorited(name, favorite=True):
    """param_set_favorite: 'favorite' is Parameter.isFavorite READ BACK after the set (null when the
    read declined), so it is the flag the parameter holds, not the flag requested."""
    def check(p):
        return _measured(f"favorite read-back '{name}' (want {favorite})",
                         {"name": p.get("name"), "favorite": p.get("favorite")},
                         p.get("name") == name and p.get("favorite") is favorite)
    return check


def _new_document(p):
    """doc_new: 'is_active' compares app.activeDocument with the new document BY HANDLE after the
    add, so it says the new document really took the foreground - not that documents.add returned
    something."""
    return _measured("new document read-back",
                     {"created": p.get("created"), "document_name": p.get("document_name"),
                      "is_active": p.get("is_active"), "is_saved": p.get("is_saved")},
                     p.get("created") is True and p.get("is_active") is True
                     and bool(p.get("document_name")))


def _document_read(p):
    """doc_get (default read): the ACTIVE document's own record, and the session list it has to
    appear in. This sweep's document is created by doc_new and never saved, so 'has_data_file'
    false is that state read back off the document."""
    active = p.get("active") or {}
    names = [r.get("name") for r in (p.get("open_documents") or [])]
    return _measured("active document in the session list",
                     {"name": active.get("name"), "has_data_file": active.get("has_data_file"),
                      "is_modified": active.get("is_modified"), "open_count": p.get("open_count"),
                      "open_documents": names[:6]},
                     bool(active.get("name")) and active.get("has_data_file") is False
                     and _num(p.get("open_count")) and p.get("open_count") >= 1
                     and active.get("name") in names)


def _document_closed(p):
    """doc_close: 'closed' names the document Document.close() answered true for, 'acted_on' is the
    identity read BEFORE the close (afterwards neither name nor URN reads back), and 'errors' holds
    the targets that refused."""
    return _measured("document closed",
                     {"closed": p.get("closed"), "closed_count": p.get("closed_count"),
                      "errors": p.get("errors"), "skipped_invalid": p.get("skipped_invalid"),
                      "remaining_open": p.get("remaining_open"), "acted_on": p.get("acted_on")},
                     p.get("closed_count") == 1 and bool(p.get("closed"))
                     and not p.get("errors") and p.get("save_changes") is False
                     and bool((p.get("acted_on") or {}).get("name")))


def _home_document(p):
    """Verify doc_get has one active document with an exact session handle."""
    active = p.get("active") or {}
    rows = [r for r in (p.get("open_documents") or []) if r.get("is_active")]
    return _measured("one active document with an exact session handle",
                     {"name": active.get("name"), "has_data_file": active.get("has_data_file"),
                      "open_count": p.get("open_count"),
                      "active_rows": [(r.get("name"), r.get("document_handle")) for r in rows]},
                     bool(active.get("name"))
                     and _num(p.get("open_count")) and p["open_count"] >= 1
                     and len(rows) == 1
                     and isinstance(rows[0].get("document_handle"), str)
                     and rows[0]["document_handle"].startswith("session:")
                     and len(rows[0]["document_handle"]) > len("session:")
                     and not any(c.isspace() for c in rows[0]["document_handle"])
                     and rows[0]["document_handle"] == active.get("document_handle"))


def _home_address(p):
    """That document's exact session handle off the same active-document read."""
    row = next(r for r in p["open_documents"] if r.get("is_active"))
    handle = row.get("document_handle")
    if (not isinstance(handle, str) or not handle.startswith("session:")
            or len(handle) <= len("session:") or any(c.isspace() for c in handle)
            or handle != (p.get("active") or {}).get("document_handle")):
        raise ValueError("active document has no exact session handle")
    return handle


def _activated(name=None):
    """doc_activate: 'activated' is the VERIFIED state - true where the foreground caught up,
    'pending' where the call was accepted and the async switch has not. Either is the switch taken;
    false is not. A name Fusion mints is reported rather than asserted."""
    def check(p):
        return _measured(f"'{name or p.get('document_name')}' activated",
                         {"activated": p.get("activated"), "is_active": p.get("is_active"),
                          "document_name": p.get("document_name")},
                         p.get("activated") in (True, "pending")
                         and (name is None or p.get("document_name") == name))
    return check


def _imported(p):
    """doc_insert_import (a solid format into a component): 'objects_created' counts what
    importToTarget2 RETURNED, and bodies_added/occurrences_added are the target component's own
    census differenced across the import."""
    n = p.get("objects_created")
    return _measured("import result",
                     {"objects_created": n, "bodies_added": p.get("bodies_added"),
                      "occurrences_added": p.get("occurrences_added"), "into": p.get("into"),
                      "created": (p.get("created") or [])[:2]},
                     p.get("imported") is True and _num(n) and n >= 1
                     and bool(p.get("created")))


def _imported_sketches(p):
    """doc_insert_import, DXF branch: one sketch per DXF layer carrying 2D geometry. The receiving
    component's own sketch census, differenced across the import, is the receipt - importToTarget2
    returns nothing for a DXF."""
    n = p.get("sketches_added")
    return _measured("DXF sketches landed",
                     {"sketches_added": n, "plane": p.get("plane"),
                      "created": (p.get("created") or [])[:2]},
                     p.get("imported") is True and _num(n) and n >= 1)


def _imported_curves(p):
    """doc_insert_import, SVG branch: the curves land in an EXISTING sketch, so the receipt is that
    sketch's own curve count differenced across the import."""
    n = p.get("curves_added")
    return _measured("SVG curves landed",
                     {"curves_added": n, "objects_created": p.get("objects_created"),
                      "into": p.get("into")},
                     p.get("imported") is True and _num(n) and n >= 1)


def _exported_bytes(p):
    """design_export: the file LANDED with content. The API's own success bool is not enough - a
    build missing a format's factory, or an export that writes an empty file, reports success and
    leaves nothing to open, so the size on disk is what the row stands on."""
    n = p.get("size_bytes")
    return _measured("exported file on disk",
                     {"format": p.get("format"), "file_path": p.get("file_path"), "size_bytes": n},
                     p.get("exported") is True and _num(n) and n > 0)


def _box(name, ox=0, oy=0, tint="", shape="box"):
    """Steps building a fresh free component 'name' holding one small solid (offset ox/oy in the
    world grid - every cameo gets its own slot so nothing builds on top of the part or another
    cameo). The reusable free occurrence the joint/assembly steps mate.

    'shape' and 'tint' make the two halves of a jointed PAIR tell apart. Two identical grey 20 mm
    cubes mated together show nothing: which one moved, and which way, is exactly what a viewer is
    trying to read off the joint."""
    if shape == "disc":
        draw = ("sketch_add_geometry",
                {"geometry": [{"kind": "circle", "cx": ox + 10, "cy": oy + 10, "radius": 10}],
                 "sketch_name": name + "S"}, "ok", None)
        height = 16
    elif shape == "bar":
        draw = ("sketch_add_geometry",
                {"geometry": [{"kind": "rectangle", "x1": ox, "y1": oy + 5,
                               "x2": ox + 34, "y2": oy + 15}],
                 "sketch_name": name + "S"}, "ok", None)
        height = 8
    else:
        draw = ("sketch_add_geometry",
                {"geometry": [{"kind": "rectangle", "x1": ox, "y1": oy,
                               "x2": ox + 20, "y2": oy + 20}],
                 "sketch_name": name + "S"}, "ok", None)
        height = 10
    return [
        ("model_create_component", {"name": name, "activate": True}, _made_component, None),
        ("sketch_create", {"plane": "xy", "name": name + "S"}, "ok", None),
        draw,
        ("model_extrude", {"sketch_name": name + "S", "profile_index": 0, "distance": height},
         _extruded, None),
    ] + ([("appearance_set", {"target": name, "color": tint}, "ok", None)] if tint else [])


# The joint bench's stations: (tag, motion, rotation axis, slide axis, drive kwarg, drive value).
# A drive of None is a motion with no single driven parameter - rigid has no freedom at all, and
# ball/planar carry several at once, so joint_drive has nothing to set and the station stands as a
# created joint. Axes are FRAME-relative; every part here is built in world coordinates on a
# world-aligned frame, so they read as world axes too.
# (tag, motion, rotation axis, slide axis, [(drive kwarg, value), ...], pose, tint).
# EVERY motion with a degree of freedom moves. The two ways of using one are different tools, and
# which one applies is a property of the motion, not a preference:
#   drives - joint_drive sets a joint's own driven VALUE, and takes revolute / slider / cylindrical
#            only (measured; it refuses the rest by name). Cylindrical carries BOTH freedoms, so it
#            is driven twice - rotation then slide - and shows them one at a time.
#   pose   - the multi-freedom motions have no single value to set, so they are POSED with
#            assembly_move within the freedom their joint allows: pin_slot slides and turns, ball
#            turns about three axes, planar slides in its plane and spins in it. joint_drive's own
#            refusal points at assembly_move for exactly this.
# Rigid is the one station that must NOT move; it is the control the others are read against.
_JOINT_STATIONS = (
    ("Rev", "revolute",    "z", "",  [("angle_deg", 90.0)],                  None,
     "#E5533C"),
    ("Sld", "slider",      "x", "",  [("distance", 22.0)],                   None,
     "#1E88E5"),
    ("Cyl", "cylindrical", "z", "",  [("angle_deg", 60.0), ("distance", 14.0)], None,
     "#43A047"),
    ("Pin", "pin_slot",    "z", "x", [], {"dx": 18.0, "rotate_z": 35.0},     "#FB8C00"),
    ("Bal", "ball",        "z", "",  [], {"rotate_x": 25.0, "rotate_z": 40.0}, "#8E24AA"),
    ("Pla", "planar",      "z", "",  [], {"dx": 15.0, "dy": 12.0, "rotate_z": 20.0}, "#00ACC1"),
    ("Rig", "rigid",       "z", "",  [], None,                               "#9E9E9E"),
)
# Where each station sits along the base, and where its arm is drawn before the joint carries it
# there. Both are authored; the layout pass moves the whole bench as one welded group.
_STN_X0, _STN_PITCH, _STN_Y, _STN_Z = 890.0, 45.0, 320.0, 10.0
# How long one motion is held before the next starts. Ten of these is the price of the seven motions
# reading as seven rather than as one blur; it is the largest dwell budget in the sweep, and the
# sweep runs against a 600 s ceiling, so it stays under a second.
_JOINT_BEAT = 0.8


def _pose_took(tag):
    """model_inspect on a POSED arm: its world footprint is no longer the 34 x 10 mm bar it was drawn
    as. Every pose on this bench TURNS the arm as well as sliding it, so an axis-aligned box still
    measuring 34 x 10 says the pose did not take. assembly_move's own report cannot say that - it
    describes the move it asked for - and a transform that ticked is not a part that moved.

    The two extents are judged TOGETHER, because a turn does not change them evenly: measured, a
    35 deg pose takes a 34 x 10 bar to 33.59 x 27.69, so the long axis barely moves while the short
    one nearly triples. Either extent alone would call that pose a no-op."""
    def check(p):
        return _measured(f"Ind{tag} is off its 34 x 10 mm rest footprint",
                         {"x": p.get("x"), "y": p.get("y"), "z": p.get("z")},
                         _num(p.get("x")) and _num(p.get("y"))
                         and abs(p["x"] - 34.0) + abs(p["y"] - 10.0) > 5.0)
    return check


def _joint_bench():
    """A station per motion type: a Joint Origin ON the base at that station, an indicator arm drawn
    clear of it, and the joint that carries the arm to the station. Then the drive pass, so the
    motions that HAVE a degree of freedom are seen using it.

    The stations are Joint Origins rather than a snap onto the base's top face because a face snap
    resolves to that face's centre - every arm would mate at the same point and the seven motions
    would end up in one pile, which is the opposite of a demonstration."""
    rows = []
    # EVERY ARM FIRST, in a row clear of the base, and only then the frame that holds the whole
    # bench. The ASSEMBLY is the thing worth watching here - seven arms hopping onto seven stations -
    # and it cannot be watched while the camera is still fitted to a bare base, or while it cuts to
    # each arm as that arm is drawn. Building the parts before the joints is what makes one frame
    # cover the whole of it.
    for i, (tag, _m, _ax, _sl, _dk, _dv, tint) in enumerate(_JOINT_STATIONS):
        rows += _box("Ind" + tag, ox=_STN_X0 + i * _STN_PITCH, oy=_STN_Y + 60.0,
                     tint=tint, shape="bar")
    rows.append(("design_activate_component", {"occurrence": "root"}, "ok", None))
    rows.append(_watch(["JointBase:1"] + ["Ind" + st[0] + ":1" for st in _JOINT_STATIONS]))
    for i, (tag, motion, axis, slide, _dk, _dv, _t) in enumerate(_JOINT_STATIONS):
        x = _STN_X0 + i * _STN_PITCH
        # FLIPPED, and the reason is measured: an arm's bottom face points -Z while the station's
        # frame points +Z, so a flush mate opposes the two normals and rotates the arm 180 degrees -
        # it hangs DOWN from the station and ends up inside the base rather than standing on it
        # (measured: an 8 mm arm landing at z 2..10 inside a base spanning z 0..10, invisible).
        # Flipping seats the arm proud of the base, which is the only place a driven motion reads -
        # measured on a finished run, all seven land at z 10..18 on a base spanning z 0..10.
        joint = {"occurrence_one": "Ind" + tag + ":1:bottom", "occurrence_two": "Stn" + tag,
                 "joint_type": motion, "axis": axis, "flip": True, "name": "J" + tag}
        if slide:
            joint["slide_axis"] = slide
        rows += [
            ("design_activate_component", {"occurrence": "JointBase:1"}, "ok", None),
            ("joint_create_origin", {"anchor": "coordinates", "x": x, "y": _STN_Y, "z": _STN_Z,
                                     "name": "Stn" + tag},
             _joint_origin_landed("Stn" + tag), None),
            ("design_activate_component", {"occurrence": "root"}, "ok", None),
            ("joint_create", joint, _jointed("J" + tag), None),
        ]
    # THE MOTION PASS. Everything that CAN move is moved before anything is put back, so one frame
    # of the bench shows six arms displaced at once against the rigid station that cannot be - which
    # is the only way a motion type reads as a motion rather than a label.
    # Each motion gets its OWN beat. Back to back they read as one blur - a viewer cannot tell which
    # arm moved for which joint, which is the whole point of a bench with one station per type. The
    # beat is a dwell rather than a camera row: the bench is already framed as a whole, and moving
    # the camera per station would cost seven zooms to show seven small displacements.
    for tag, _m, _ax, _sl, drives, _pose, _t in _JOINT_STATIONS:
        for kwarg, value in drives:
            check = _driven_angle(value) if kwarg == "angle_deg" else _driven_slide(value)
            rows.append(("joint_drive", {"joint_name": "J" + tag, kwarg: value}, check, None))
            rows.append(_dwell(_JOINT_BEAT))
    # the multi-freedom motions, POSED - each within what its own joint allows. assembly_move on a
    # jointed occurrence is a TRANSIENT pose and says so in 'jointed_warning'; it is discarded below
    # rather than captured, because a pending uncaptured move makes the next joint create refuse.
    for tag, _m, _ax, _sl, _d, pose, _t in _JOINT_STATIONS:
        if not pose:
            continue
        rows.append(("assembly_move", dict(occurrence="Ind" + tag + ":1", **pose),
                     _moved_occurrence(), None))
        rows.append(("model_inspect", {"target": "Ind" + tag + ":1"}, _pose_took(tag), None))
        rows.append(_dwell(_JOINT_BEAT))
    # THE DISPLACED FRAME, captured before anything is put back: six arms off their rest pose against
    # the one rigid station that cannot leave its. 'current' shoots what the camera already frames -
    # a named view would refit to the whole model and lose the bench.
    rows.append(_dwell(2.5))
    rows.append(("view_screenshot", {"view": "current", "width": 640, "height": 460}, "ok", None))
    # the motions joint_drive will NOT take, each refused by name rather than answered with a value
    # it does not have: rigid has no freedom at all, ball carries three rotations at once, and
    # pin_slot is outside the drivable set even though it has freedom in two. The refusal is what
    # sends a caller to assembly_move, which is what the poses above use.
    for tag in ("Rig", "Bal", "Pin"):
        rows.append(("joint_drive", {"joint_name": "J" + tag, "angle_deg": 30.0}, "refused", None))
    # HOME AGAIN: the transient poses reverted in one call, then every driven value back to zero, so
    # the bench is left as it was built and the next act starts from a known pose.
    rows.append(("assembly_capture_position", {"action": "discard_pending"},
                 lambda p: p.get("discarded") is True and p.get("has_pending") is False, None))
    for tag, _m, _ax, _sl, drives, _pose, _t in _JOINT_STATIONS:
        for kwarg, _value in drives:
            check = _driven_angle(0.0) if kwarg == "angle_deg" else _driven_slide(0.0)
            rows.append(("joint_drive", {"joint_name": "J" + tag, kwarg: 0.0}, check, None))
    return rows


def _group_of(name):
    """The tool-group a cameo or scratch sketch belongs to - its name with any trailing index,
    suffix letter or role word stripped. SlotA..SlotH are one group, EditTrim/EditSplit/EditCorner
    another, PipeRun/PipeHalf/PipeCut another. Grouping is what lets the camera move once per family
    of related operations instead of once per entity, and frame the family together."""
    stem = re.sub(r"(:\d+)$", "", name)
    stem = re.sub(r"(Sketch|Path|Prof|Block|Post|Cameo|Comp|Part|Run|S)$", "", stem) or stem
    stem = re.sub(r"[A-Z]?\d*$", "", stem) or stem
    return stem or name


# {sketch name: the origin plane it was drawn on} - a sketch is shot NORMAL to its own plane, or it
# is edge-on and renders as nothing. Shooting every sketch from the top assumes every sketch is on
# XY; an XZ one photographed from above is a blank frame.
_SKETCH_PLANE = {}
_PLANE_VIEW = {"xy": "top", "xz": "front", "yz": "right"}


def _watch(occurrence):
    """A camera row: orient and FRAME the occurrence, so a viewer watching the run sees the chunk
    about to be exercised fill the viewport instead of sitting as a speck in a corner. The story
    world is metres wide (every cameo gets its own grid slot) while the parts are tens of
    millimetres, so an unframed orient shows nothing legible - one of these opens each chunk that
    works on its own geometry, not each step.

    Sketch-only subjects are shot from the TOP, not iso: a flat XY sketch seen from iso-top-right is
    foreshortened to near nothing, and text on it cannot be read at all. Anything with a body in it
    keeps iso, where a solid reads as a solid."""
    names = occurrence if isinstance(occurrence, list) else [occurrence]
    flat = not any(str(n).endswith(":1") for n in names)
    view = "iso-top-right"
    if flat:
        planes = {_SKETCH_PLANE.get(str(n)) for n in names}
        planes.discard(None)
        # one shared plane: look straight down its normal. Mixed planes have no such view, so iso
        # at least shows all of them at an angle rather than one of them edge-on.
        view = _PLANE_VIEW.get(planes.pop(), "top") if len(planes) == 1 else "iso-top-right"
    return ("view_set", {"action": "orient", "orientation": view, "focus": occurrence}, "ok", None)


def _dwell(seconds):
    """Hold the current view for a beat. The ONLY sleep in the sweep and a deliberate one: a
    showcase step exists to be watched, and a toolpath that flicks on and off inside one frame
    shows nothing. It is not a wait for an async result - those are bounded polls - so it never
    guards a correctness step, and _DWELL is filtered out of STEPS so it cannot pass as a tool."""
    return (_DWELL, {"seconds": seconds}, "ok", None)


def _leaves_no_row(step):
    """True for a step run_steps judges nothing for and appends NO row - today only a _dwell.

    The ONE definition of that class, because two sites depend on it agreeing: run_steps skips on
    it, and judged_steps drops it to keep the by-position pairing honest. A second row-less step
    kind taught to one site alone would silently shift that pairing again."""
    return step[0] == _DWELL


def _watch_all():
    """A camera row with NO focus: fit the whole model. Correct only while the world IS the subject
    - the parametric skeleton, before the first cameo lands in its own grid slot - and wrong once
    the grid spreads the world over a metre, which is what _watch(occurrence) is for."""
    return ("view_set", {"action": "orient", "orientation": "iso-top-right"}, "ok", None)
