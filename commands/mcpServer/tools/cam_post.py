# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Create-or-reuse an NC Program for the chosen scope, then post it to a G-code / NC file on disk.
Success is gated on the OUTPUT FILE landing - postProcess returning true is not proof one was
written - and the post config decides the extension, so files are matched by newness."""

import glob
import os
import tempfile
import time

import adsk.core
import adsk.cam

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import named_with_remainder, ok, error, safe
from . import _assert
from . import _inputs
from . import _outputs
from ._cam_common import (get_cam, library_assets, live_readiness, resolve_cam_node,
                          setups as cam_setups, operations_under, toolpath_present_tally,
                          quote_expression as _quote, unquote_expression as _unquote)
from ._export import verify_written   # the file-landed proof (postProcess() true != a file)

app = adsk.core.Application.get()

RETURNS = [
    _outputs.ReturnsValue("file_path", "an NC/G-code file written to disk", in_list=True),
]

_UNITS_CHOICE = _inputs.Choice("units", options=["document", "inch", "mm"], default="document")

# NC-program output parameters live on the NCProgramInput/NCProgram CAMParameters collection - the
# UI's Name / Comment / Output-folder fields - not on the .cps post OPTIONS in .postParameters.
# Every set below is read back, so the applied value is reported rather than assumed.
_P_NAME = "nc_program_name"
_P_COMMENT = "nc_program_comment"
_P_FOLDER = "nc_program_output_folder"
_P_OPEN_EDITOR = "nc_program_openInEditor"
_P_UNIT = "nc_program_unit"
_MISSING = object()   # sentinel: the parameter is not present on this program


# Shared-library post scope -> the LibraryLocations enum member that names its root. The team
# libraries are network-slow and NESTED in folders (a flat listing is empty), so the walk is bounded
# on both depth and node count; the shipped library holds hundreds of posts, so its cap is its own.
_POST_LOCATIONS = {"cloud": "CloudLibraryLocation", "hub": "HubLibraryLocation",
                   "fusion": "Fusion360LibraryLocation"}
_POST_MAX_DEPTH = 4
_POST_MAX_ASSETS = {"cloud": 400, "hub": 400, "fusion": 900}
_NAMES_LISTED = 30

_POST_SCOPE_CHOICE = _inputs.Choice(
    "post_scope", options=["local", "cloud", "hub", "fusion"], default="local")


def _post_library():
    """The shared PostLibrary - on CAMManager.get().libraryManager.postLibrary (parallel to the tool
    libraries; NOT the document's CAM product). Patched in tests."""
    return safe(lambda: adsk.cam.CAMManager.get().libraryManager.postLibrary)


def _resolve_local_cps(cam, post):
    """Resolve a LOCAL 'post' to a full .cps path that exists on disk. Accepts a full path to a .cps
    file, or a bare name resolved against the personal then the installed (generic) post folder.
    Returns (path, None) or (None, error)."""
    post = post.replace("\\", "/")
    stem = post if post.lower().endswith(".cps") else post + ".cps"
    personal = safe(lambda: cam.personalPostFolder)
    generic = safe(lambda: cam.genericPostFolder)
    candidates = []
    if os.path.isabs(post) or "/" in post:
        candidates.append(stem)                       # treat as a path as given
    base = os.path.basename(stem)
    for folder in (personal, generic):
        if folder:
            candidates.append(folder.replace("\\", "/").rstrip("/") + "/" + base)
    for c in candidates:
        if safe(lambda c=c: os.path.isfile(c), False):
            return c, None
    return None, (f"Post config not found for '{post}'. Tried: {', '.join(candidates) or '(none)'}. "
                  f"Provide a full .cps path, or a post name in the personal ({personal}) or installed "
                  f"({generic}) post folder. post_scope=fusion resolves the same name against the posts "
                  "this installation ships; cloud/hub against a deployed team post.")


def _norm_post_name(name):
    """Case-fold a post name and drop a trailing .cps so 'Generic Fanuc' matches 'generic fanuc.cps'."""
    low = (name or "").strip().lower()
    return low[:-4] if low.endswith(".cps") else low


def _resolve_library_post(post, post_scope):
    """Resolve a CLOUD/HUB/FUSION 'post' to a PostConfiguration by matching the post NAME against
    the asset URLs' leafName under the scope root (case-insensitive and EXACT), then loading it via
    postConfigurationAtURL. Refuses an ambiguous name with the candidate urls. Returns
    (post_config, label, None) or (None, None, error). Patched in tests."""
    lib = _post_library()
    if not lib:
        return None, None, "Post library unavailable (CAMManager.get().libraryManager.postLibrary)."
    loc = getattr(adsk.cam.LibraryLocations, _POST_LOCATIONS[post_scope], None)
    root = safe(lambda: lib.urlByLocation(loc)) if loc is not None else None
    if root is None:
        return None, None, f"Could not resolve the {post_scope} post-library root URL."
    # The shared bounded library walk. childAssetURLs yields the post URLs (each exposing .leafName =
    # the post name and .toString() = the full url); childPostConfigurations would load the heavy
    # PostConfiguration objects, which carry no name/url, so the asset URLs are what we match on.
    assets, truncated = library_assets(lib, root, max_depth=_POST_MAX_DEPTH,
                                       max_assets=_POST_MAX_ASSETS[post_scope])
    want = _norm_post_name(post)

    def leaf(a):
        return safe(lambda a=a: a.leafName) or ""

    matches = [a for a in assets if _norm_post_name(leaf(a)) == want]
    if not matches:
        # The shipped library answers with hundreds of names, so what the listing cannot carry is
        # COUNTED by the shared renderer rather than dropped.
        names = sorted({leaf(a) for a in assets if leaf(a)})
        hint = (f" Available posts: {named_with_remainder(names, cap=_NAMES_LISTED)}." if names
                else " No posts were found in this library.")
        trunc = " (search was capped - more posts may exist deeper)" if truncated else ""
        return None, None, f"No {post_scope} post named '{post}'.{hint}{trunc}"
    if len(matches) > 1:
        urls = [safe(lambda a=a: a.toString()) or leaf(a) for a in matches]
        return None, None, (f"Ambiguous {post_scope} post '{post}' - {len(matches)} match: "
                            f"{', '.join(str(u) for u in urls)}. Pass a more specific name or the url.")
    url = matches[0]
    pc = safe(lambda: lib.postConfigurationAtURL(url))
    if pc is None:
        return None, None, (f"The {post_scope} post '{post}' matched a url but postConfigurationAtURL "
                            f"returned null ({safe(lambda: url.toString())}).")
    label = safe(lambda: url.toString()) or leaf(url)
    return pc, label, None


def _resolve_post_config(cam, post, post_scope):
    """(post_config, label, None) or (None, None, error) - 'post' resolved for the chosen scope: a
    .cps path or name in the local post folders, or a post NAME in a shared/shipped library."""
    post = (post or "").strip()
    if not post:
        return None, None, ("Provide 'post' - the post processor: for post_scope=local a full .cps path "
                            "or a name in the personal/installed post folder; for cloud/hub/fusion the "
                            "NAME of a post in that library.")
    if post_scope in _POST_LOCATIONS:
        return _resolve_library_post(post, post_scope)
    path, perr = _resolve_local_cps(cam, post)
    if perr:
        return None, None, perr
    pc, lerr = _load_post_configuration(path)
    if lerr:
        return None, None, lerr
    return pc, path, None


def _load_post_configuration(cps_path):
    """Build a PostConfiguration from a .cps file's content. Returns (post_config, None) or
    (None, error). createFromContent takes the file CONTENT string, not a path."""
    try:
        with open(cps_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return None, f"Could not read the post config '{cps_path}': {e}"
    try:
        pc = adsk.cam.PostConfiguration.createFromContent(content)
    except Exception as e:
        return None, f"Could not load the post config '{cps_path}': {e}"
    if pc is None:
        return None, f"The post config '{cps_path}' did not load (createFromContent returned null)."
    return pc, None


def _dir_snapshot(folder):
    """{file_path: mtime} for the regular files currently in folder (empty if it does not exist yet)."""
    snap = {}
    if safe(lambda: os.path.isdir(folder), False):
        for n in safe(lambda: os.listdir(folder), []) or []:
            p = os.path.join(folder, n)
            if safe(lambda p=p: os.path.isfile(p), False):
                snap[p] = safe(lambda p=p: os.path.getmtime(p), 0.0)
    return snap


def _new_or_changed(folder, before):
    """Sorted paths in folder that are new since 'before' or whose mtime advanced - the files this post
    actually wrote."""
    out = []
    if not safe(lambda: os.path.isdir(folder), False):
        return out
    for n in sorted(safe(lambda: os.listdir(folder), []) or []):
        p = os.path.join(folder, n)
        if not safe(lambda p=p: os.path.isfile(p), False):
            continue
        m = safe(lambda p=p: os.path.getmtime(p), 0.0)
        if p not in before or m > before[p]:
            out.append(p)
    return out


def _set_str_param(params, name, value):
    """Set a string CAMParameter by name and read it back. Returns the read-back value, or _MISSING if
    the parameter is not present on this program."""
    p = safe(lambda: params.itemByName(name))
    if p is None:
        return _MISSING
    quoted = _quote(value)
    try:
        p.expression = quoted
    except Exception as e:
        return f"<error: {e}>"
    return _unquote(safe(lambda: p.expression))


def _set_value_param(params, name, value):
    """Set a value-backed CAMParameter (bool/choice) by name and read it back. Returns the read-back
    value, or _MISSING if the parameter is not present on this program."""
    p = safe(lambda: params.itemByName(name))
    if p is None:
        return _MISSING
    try:
        p.value.value = value
    except Exception as e:
        return f"<error: {e}>"
    return safe(lambda: p.value.value)


def _set_unit_param(params, units_key):
    """(applied_value, note) - set the NC-program output-unit ChoiceParameter to `units_key` and
    read it back; a ChoiceParameterValue rejects a bare int index and exposes its legal values only
    through getChoices(), so the value is picked by the unit word in its NAME."""
    p = safe(lambda: params.itemByName(_P_UNIT))
    if p is None:
        return _MISSING, None
    cval = safe(lambda: p.value)
    got = safe(lambda: cval.getChoices()) if cval is not None else None
    try:
        names, values = (list(got[1]), list(got[2])) if got and got[0] else ([], [])
        token = {"inch": "inch", "mm": "milli", "document": "document"}[units_key]
        hits = [v for n, v in zip(names, values) if token in str(n).lower()]
        if len(hits) != 1:
            raise ValueError(
                f"no unique '{token}' entry in the program's unit choices {names or '(none exposed)'}")
        expected = _unquote(hits[0])
        cval.value = expected
    except Exception as e:
        note = (f"Output units could not be set to '{units_key}' ({e}); posting was refused. "
                "Set units in the post config / Fusion UI, or omit 'units'.")
        return f"<error: {e}>", note
    applied = safe(lambda: cval.value, _MISSING)
    if applied is _MISSING or applied != expected:
        note = (f"Output units did not read back as '{units_key}' after the setter "
                f"(got {applied!r}); posting was refused.")
        return f"<error: output units read back as {applied!r}>", note
    return applied, None


def _stored_str_param(params, name):
    """Read-only companion to _set_str_param: the CURRENT expression of a string CAMParameter, or None
    when absent - lets as-is posting target the program's STORED output folder without writing
    anything (as-is skips every set_*_param call)."""
    p = safe(lambda: params.itemByName(name))
    if p is None:
        return None
    return _unquote(safe(lambda: p.expression))


def _apply_output_params(params, program_name, out_dir, comment, units_key):
    """Apply the NC-program output parameters onto a CAMParameters collection (an NCProgramInput's or an
    existing NCProgram's). Returns (applied, unresolved, unit_note): applied is {param: read_back_value},
    unresolved is the list of expected params this program did not expose, unit_note is a human string
    when the requested output units did not apply (else None)."""
    applied = {}
    unresolved = []

    def record(name, result):
        if result is _MISSING:
            unresolved.append(name)
        else:
            applied[name] = result

    record(_P_NAME, _set_str_param(params, _P_NAME, str(program_name).strip()))
    # Point the program at the folder the file-landed gate watches (forward slashes: robust in the
    # Fusion expression parser on every platform).
    record(_P_FOLDER, _set_str_param(params, _P_FOLDER, out_dir.replace("\\", "/")))
    if (comment or "").strip():
        record(_P_COMMENT, _set_str_param(params, _P_COMMENT, comment.strip()))
    record(_P_OPEN_EDITOR, _set_value_param(params, _P_OPEN_EDITOR, False))   # headless: never pop the editor
    unit_note = None
    if units_key != "document":
        uval, unit_note = _set_unit_param(params, units_key)
        record(_P_UNIT, uval)
    return applied, unresolved, unit_note


def _cam_log_root(temp_dir):
    """The Fusion360CAM tree post logs land in: temp_dir's own 'Fusion360CAM' ancestor when the process
    temp dir already sits inside one, else <temp_dir>/Fusion360CAM."""
    path = os.path.normpath(temp_dir)
    while True:
        if os.path.basename(path).lower() == "fusion360cam":
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return os.path.join(os.path.normpath(temp_dir), "Fusion360CAM")
        path = parent


# Fusion writes the DETAILED post error to <TEMP>/Fusion360CAM/<session>/<n>/<program>.log - NOT the
# output folder (there it leaves only a '.failed' stub that says "See log for details"). Inside the
# Fusion process tempfile.gettempdir() is <TEMP>/Fusion360CAM/<session> on 2705.1.11 (measured live).
_CAM_LOG_ROOT = _cam_log_root(tempfile.gettempdir())


def _is_failure_marker(path):
    """A '.failed' file (e.g. '1001.nc.failed') is a POST-FAILED stub, not a deliverable NC file."""
    return path.lower().endswith(".failed")


def _post_log_errors(program_name, since):
    """Best-effort Error/Warning lines from THIS post's log, so a failure is actionable, not a guess.
    Finds the newest <program>.log under the Fusion CAM temp tree touched since the post started."""
    base = os.path.basename(str(program_name).strip())
    if not base:
        return []
    try:
        matches = glob.glob(os.path.join(_CAM_LOG_ROOT, "**", base + ".log"), recursive=True)
    except Exception:
        return []
    recent = [(m, p) for (m, p) in
              ((safe(lambda p=p: os.path.getmtime(p), 0.0), p) for p in matches) if m >= since - 2.0]
    if not recent:
        return []
    _, newest = max(recent)
    lines = []
    try:
        with open(newest, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                s = raw.strip()
                if s.lower().startswith(("error", "warning")) and s not in lines:
                    lines.append(s)
                    if len(lines) >= 15:
                        break
    except Exception:
        return []
    return lines


# What a post's own log line calls the value it could not read a number out of. Nothing here decides
# which posts need one - only a post that SAID so earns the clause below.
_PROGRAM_NUMBER_MARK = "program number"


def _program_number_clause(log_errors, prog_name):
    """The clause a post-log line naming the PROGRAM NUMBER earns when the name passed was not a
    number; '' otherwise."""
    if prog_name.isdigit():
        return ""
    if not any(_PROGRAM_NUMBER_MARK in (line or "").lower() for line in log_errors):
        return ""
    return (f" The post's message names the PROGRAM NUMBER, and program_name '{prog_name}' is not a "
            "number - retry with a numeric program_name such as '1001'.")


def _operations_collection(cam, targets):
    """The list of things to post - the resolved target(s), or every setup. NCProgram.operations is
    a vector<OperationBase> setter: it takes a plain LIST, not an ObjectCollection, and expands
    children."""
    return list(targets) if targets else cam_setups(cam)


def _setup_list(setups):
    """(names, error) - the 'setups' request as a list of setup names, from a JSON array or an
    'A, B' string; blank entries are dropped."""
    if isinstance(setups, str):
        raw = setups.split(",")
    elif isinstance(setups, (list, tuple)):
        raw = list(setups)
    else:
        return None, "Provide 'setups' as an array of setup names, or a 'SetupA, SetupB' string."
    return [s for s in (str(x).strip() for x in raw) if s], None


def _resolve_setups(cam, names):
    """([Setup objects], error) for an explicit setups list, each resolved through the shared exact
    resolver; a spelling listed twice is refused."""
    seen, out = set(), []
    for want in names:
        key = want.lower()
        if key in seen:
            return None, f"'{want}' is listed twice in 'setups' - name each setup once."
        seen.add(key)
        node, err = resolve_cam_node(cam, want, kinds=("setup",), label="setup")
        if err:
            return None, err
        out.append(node.obj)
    return out, None


def _expand_ops(items):
    """Terminal Operation objects for any item NCProgram.operations may hold, so a STORED scope and
    a REQUESTED one compare on the same flattened shape."""
    out = []
    for it in (items or []):
        has_children = (safe(lambda it=it: it.operations) is not None
                        or safe(lambda it=it: it.allOperations) is not None)
        if has_children:
            out.extend(operations_under(it))
        else:
            out.append(it)
    return out


# The count semantics BOTH post arms publish - one sentence, since both arms publish the same keys
# off _program_counts.
_COUNTS_NOTE = ("program_operation_count: unsuppressed operations in scope; "
                "posted_operations: hasToolpath True. Suppressed operations are excluded "
                "from both counts and NC output.")


# MEASURED: a program scoped over a list holding a suppressed operation omits it - filteredOperations
# excludes it, postProcess answers True, and the file carries none of its moves. Suppressing a
# generated operation flips hasToolpath False, so posted_operations counts 0 for it.
def _program_counts(program) -> dict:
    """{program_operation_count, program_item_count, posted_operations, toolpath_unread?} - the
    operations the program HOLDS, the setups/folders it stores, and how many held rows read
    hasToolpath True; a key is absent where its collection did not read."""
    out = {}
    held = safe(lambda: list(program.filteredOperations))
    if held is not None:
        out["program_operation_count"] = len(held)
        posted, unread = toolpath_present_tally(held)
        out["posted_operations"] = posted
        if unread:
            out["toolpath_unread"] = unread
    items = safe(lambda: list(program.operations))
    if items is not None:
        out["program_item_count"] = len(items)
    return out


def _membership_facts(program, prog_name, requested_ops):
    """(facts, error) - the program's OWN operations read back after the scope was assigned, against
    what that scope resolved to. An EMPTY read-back beside a non-empty request is the one shape
    reported as an error; every other disagreement is disclosed as counts."""
    stored = safe(lambda: list(program.operations))
    if stored is None:
        return dict(_program_counts(program), membership_verified=None,
                    membership_note=("the program's own operations did not read back, so its "
                                     "membership was not compared with the scope.")), None
    stored_ops = _expand_ops(stored)
    if requested_ops and not stored_ops:
        return None, (f"NC Program '{prog_name}' re-read ZERO operations after the requested scope "
                      f"was assigned, while that scope resolves to {len(requested_ops)} - the "
                      "assignment did not take, so nothing was posted.")
    stored_ids, stored_unreadable = _op_id_set(stored_ops)
    wanted_ids, wanted_unreadable = _op_id_set(requested_ops)
    # The flatten above serves the id comparison only - counting it would publish more operations
    # than the program holds.
    facts = _program_counts(program)
    if stored_unreadable or wanted_unreadable:
        facts["membership_verified"] = None
        facts["membership_note"] = (
            f"{stored_unreadable} stored and {wanted_unreadable} requested operation(s) have no "
            "readable operationId, so the program's membership was not compared with the scope.")
    elif stored_ids == wanted_ids:
        facts["membership_verified"] = True
    else:
        facts["membership_verified"] = False
        # The two sides of the disagreement ride as counts, so the wire clause states the fact and
        # points at the read that lists what the program holds.
        facts["membership_missing"] = len(wanted_ids - stored_ids)
        facts["membership_extra"] = len(stored_ids - wanted_ids)
        facts["membership_note"] = (
            f"the program holds {len(stored_ids)}, not the {len(wanted_ids)} the scope resolves "
            "to - cam_get(include=['nc_programs']).")
    return facts, None


def _op_id_set(ops):
    """(ids, unreadable) - the operationId identity for a stored-vs-requested comparison; an
    unreadable id is COUNTED, since two fetches of one operation are different Python objects."""
    ids, unreadable = set(), 0
    for o in (ops or []):
        oid = safe(lambda o=o: o.operationId)
        if oid is None:
            unreadable += 1
        else:
            ids.add(oid)
    return ids, unreadable


def _rollback_program(cam, program, prog_name, reused):
    """The rollback sentence for the program a failed post would leave behind - empty when the
    program pre-existed (it is the caller's), else deleteMe()'s answer CONFIRMED by a re-read of
    ncPrograms: removed, or still there with the way to remove it."""
    if reused:
        return ""
    try:
        removed = program.deleteMe()
    except Exception as e:
        return (f" The just-created NC Program '{prog_name}' was NOT removed - deleteMe raised "
                f"({e}), so it remains in the document; remove it with cam_delete.")
    if not removed:
        return (f" The just-created NC Program '{prog_name}' was NOT removed - deleteMe returned "
                "false, so it remains in the document; remove it with cam_delete.")
    # cam_delete's shape: a true bool is not the effect, so the name is looked up again.
    if safe(lambda: cam.ncPrograms.itemByName(prog_name)) is not None:
        return (f" deleteMe returned true for the just-created NC Program '{prog_name}' but it "
                "still resolves in ncPrograms - remove it with cam_delete.")
    return f" The just-created NC Program '{prog_name}' was removed."


def handler(scope: str = "", post: str = "", post_scope: str = "local", output_folder: str = "",
            program_name: str = "", units: str = "document", program_comment: str = "",
            overwrite: bool = False, setups=None) -> dict:
    """See TOOL_DESCRIPTION."""
    cam, cerr = get_cam()
    if cerr:
        return error(cerr)

    if not (program_name or "").strip():
        return error("Provide 'program_name' - the NC Program name or number (some posts require a number).")
    prog_name = str(program_name).strip()

    # Parsed before the reuse check: a 'setups' list IS a scope, so it decides as-is mode alongside
    # 'scope'/'post'/'output_folder'.
    setup_names, slerr = _setup_list(setups) if setups else ([], None)
    if slerr:
        return error(slerr)

    # Reuse-or-create. Looked up FIRST (before the configuration guards below) so as-is mode can skip
    # them entirely for an EXISTING program called with no configuration knobs.
    existing = safe(lambda: cam.ncPrograms.itemByName(prog_name))
    reused = existing is not None
    as_is = (reused and not (scope or "").strip() and not (post or "").strip()
            and not (output_folder or "").strip() and not setup_names)

    units_key, uerr = _UNITS_CHOICE.resolve(units)
    if uerr:
        return error(uerr)
    if as_is and units_key != "document":
        return error("An as-is post uses the program's stored output units; omit 'units' or "
                     "pass units='document' before posting it unchanged.")

    post_scope_key, pserr = _POST_SCOPE_CHOICE.resolve(post_scope)
    if pserr:
        return error(pserr)

    if not as_is and not (output_folder or "").strip():
        return error("Provide 'output_folder' - the directory where the NC file(s) will be written.")

    # Resolve the scope up front so a bad name fails before we touch the NC Program. Stays None/
    # "document" for as-is (scope and setups are guaranteed blank by the as_is check above).
    targets, kind = (None, "document")
    want = (scope or "").strip()
    if setup_names and want:
        return error("Pass 'scope' or 'setups', not both - 'scope' names ONE setup/folder/operation "
                     "and 'setups' names the several setups one program holds.")
    if setup_names:
        objs, sserr = _resolve_setups(cam, setup_names)
        if sserr:
            return error(sserr + " Omit both to post the whole document.")
        targets, kind = objs, "setups"
    elif want and want.lower() not in ("all", "document", "*"):
        node, serr = resolve_cam_node(cam, want, kinds=("setup", "folder", "operation"),
                                      label="setup/folder/operation")
        if serr:
            return error(serr + " Omit 'scope' to post the whole document.")
        targets, kind = [node.obj], node.kind

    # Up-front refusal: nothing valid to post. live_readiness is the one CAM health signal; the post
    # omits invalid/empty operations, so zero valid ops -> no file. Applies to as-is too - a stale
    # program still writes wrong G-code.
    live, lerr = live_readiness()
    if lerr:
        return error(lerr)
    live = live or {}
    if not live.get("valid"):
        return error("No valid toolpaths to post - every operation is out-of-date, errored, or "
                     "ungenerated. Run cam_generate (in the Manufacture workspace) first. "
                     f"({live.get('readiness', '')})")

    # OVERWRITE GUARD: reconfiguring an EXISTING program whose STORED operations differ from the
    # REQUESTED scope would clobber a program that may be machinist-curated. Each refusal names
    # every input that has to be omitted to reach as-is mode.
    if reused and not as_is and not overwrite:
        stored_ops = _expand_ops(safe(lambda: list(existing.operations)) or [])
        stored_ids, stored_unreadable = _op_id_set(stored_ops)
        requested_ids, requested_unreadable = _op_id_set(_expand_ops(_operations_collection(cam, targets)))
        if stored_ops and (stored_unreadable or requested_unreadable):
            # No identity, no comparison: whether this reconfigure clobbers curated work is unknown,
            # and an unknown answer must not be published as "they match".
            return error(
                f"NC Program '{prog_name}' already exists, but its stored operations cannot be "
                f"compared with what 'scope' resolves to: {stored_unreadable} stored and "
                f"{requested_unreadable} requested operation(s) have no readable operationId, so "
                "whether reconfiguring would overwrite a machinist-curated program is unknown. Omit "
                "'scope', 'setups', 'post', and 'output_folder' to post it exactly as stored, or "
                "pass overwrite=true to reconfigure it anyway.")
        if stored_ids and stored_ids != requested_ids:
            return error(
                f"NC Program '{prog_name}' already exists and its stored operations differ from what "
                "'scope' resolves to - reconfiguring would overwrite a program that may be machinist-"
                "curated. Omit 'scope', 'setups', 'post', and 'output_folder' to post it exactly as "
                "stored, or pass overwrite=true to reconfigure it.")

    if as_is:
        # Post the program EXACTLY as stored: no operations/postConfiguration/parameter writes - just
        # postProcess() against its own configuration.
        program = existing
        out_dir = _stored_str_param(program.parameters, _P_FOLDER)
        if not out_dir:
            return error(f"NC Program '{prog_name}' has no stored '{_P_FOLDER}' output folder to post "
                         "as-is against - configure it once with 'output_folder' and 'post'.")
        post_label = (safe(lambda: program.postConfiguration.description)
                     if safe(lambda: program.postConfiguration) else None)
        applied, unresolved, unit_note, membership = {}, [], None, {}
    else:
        # Resolve the post AFTER the cheap guards - a cloud/hub lookup is network-slow, so a bad
        # output_folder/program_name/scope or an empty document fails fast without touching the network.
        post_config, post_label, perr = _resolve_post_config(cam, post, post_scope_key)
        if perr:
            return error(perr)

        out_dir = os.path.abspath(output_folder.strip())

        # UPDATE IN PLACE rather than delete+recreate: operations, postConfiguration and the output
        # parameters are all settable, so the program keeps its identity. ONE resolve of the scope
        # serves both the assignment and the membership read-back below.
        scope_items = _operations_collection(cam, targets)
        requested_ops = _expand_ops(scope_items)
        try:
            if reused:
                program = existing
                program.operations = scope_items
                program.postConfiguration = post_config
                applied, unresolved, unit_note = _apply_output_params(program.parameters, prog_name,
                                                                      out_dir, program_comment, units_key)
            else:
                nc_input = cam.ncPrograms.createInput()
                nc_input.displayName = prog_name
                nc_input.operations = scope_items
                applied, unresolved, unit_note = _apply_output_params(nc_input.parameters, prog_name,
                                                                      out_dir, program_comment, units_key)
                program = cam.ncPrograms.add(nc_input)
                program.postConfiguration = post_config
        except Exception as e:
            return error(f"Could not {'update' if reused else 'create'} the NC Program '{prog_name}': {e}. "
                         f"(Post config: {post_label}.)")

        if program is None:
            return error(f"NC Program '{prog_name}' was not {'updated' if reused else 'created'} "
                         "(the API returned null).")

        # What the program's OWN operations read back as - the assignment is not taken on trust.
        membership, merr = _membership_facts(program, prog_name, requested_ops)
        if merr:
            return error(merr + _rollback_program(cam, program, prog_name, reused))

        if units_key != "document" and (_P_UNIT in unresolved or unit_note):
            reason = (unit_note or f"The NC Program has no '{_P_UNIT}' parameter, so output units could not be set.")
            retained = (" Existing-program configuration edits remain; inspect the program before retrying."
                        if reused else "")
            return error(reason + retained + _rollback_program(cam, program, prog_name, reused))

        if unresolved and _P_FOLDER in unresolved:
            # Without the output-folder parameter the file lands at the program default, not out_dir, and
            # the file-landed gate below can't see it - fail loudly and name the missing parameter.
            return error(f"The NC Program has no '{_P_FOLDER}' parameter, so the output folder could not be "
                         f"set to '{out_dir}'. Unresolved parameters: {', '.join(unresolved)}."
                         + _rollback_program(cam, program, prog_name, reused))

    before = _dir_snapshot(out_dir)
    started = time.time()

    try:
        # postProcess(None) RAISES "Options must not be null" (live-verified, Fusion 2704.1.39) - the
        # API docstring's "Can be null" does not hold in this build; always pass a real options object.
        options = adsk.cam.NCProgramPostProcessOptions.create()
        posted = program.postProcess(options)
    except Exception as e:
        # Do not leave a just-created program behind a failed post - and say which way that went.
        return error(f"Post processing raised: {e}. (Post config: {post_label}; check the post matches "
                     "the machine/operations.)"
                     + _rollback_program(cam, program, prog_name, reused))

    # Honesty gate: postProcess returning true is NOT proof a file landed - diff the folder. A '.failed'
    # stub is a failure marker, NOT a deliverable - keep the two apart so a failed post never lists a
    # stub as if it were G-code.
    written = _new_or_changed(out_dir, before)
    files, failed_markers = [], []
    for p in written:
        if _is_failure_marker(p):
            failed_markers.append(p)
            continue
        size, note = verify_written(p)
        if not note:
            files.append({"file_path": p, "size_bytes": size})

    if not files:
        # No deliverable NC file - a created program that produced nothing is an orphan, so roll it back
        # (a reused program pre-existed, leave it). Surface the post LOG's error lines - the real,
        # actionable reason (a '.failed' stub in the folder only says "see log").
        rolled = _rollback_program(cam, program, prog_name, reused)
        log_errors = _post_log_errors(prog_name, started)
        msg = (f"NC Program '{prog_name}' did not post a usable NC file to '{out_dir}' "
               f"(postProcess={bool(posted)}, failed_stub={bool(failed_markers)})."
               + rolled + " ")
        if log_errors:
            msg += "Post log: " + " | ".join(log_errors) + _program_number_clause(log_errors, prog_name)
        else:
            msg += "Check the output folder path, the program number/name, and that the post matches the ops."
        return error(msg)

    # A file appeared, but a faulted program (NCProgram.hasError, the same flag live_readiness counts as
    # programs_errored) OR a '.failed' stub alongside is a BLOCKER - the G-code may be incomplete/wrong.
    program_error = bool(safe(lambda: program.hasError, False))
    clean = bool(posted) and not program_error and not failed_markers

    program_verb = "reused" if reused else "created"
    result = {
        "posted": bool(posted),
        "mode": "as_is" if as_is else "configured",
        "scope": "as_is" if as_is else kind,
        "program_name": prog_name,
        "program_reused": reused,
        "nc_program": program_verb,
        "post_config": post_label,
        "output_folder": out_dir,
        "file_count": len(files),
        "files": files,
        "elapsed_seconds": round(time.time() - started, 1),
        # live_readiness's own sentence, which embeds an operation name and its warning text - a key
        # of its own, since a note that inlined it could not be bounded.
        "readiness": live.get("readiness", ""),
    }
    # The held/posted counts come off ONE builder on both arms, so what they mean cannot depend on
    # which arm posted; as-is compared no scope, so it carries counts and no membership verdict.
    if as_is:
        result.update(_program_counts(program))
    else:
        result.update({
            "post_scope": post_scope_key,
            "units": units_key,
            "params_applied": applied,
        })
        result.update(membership)
        if kind == "setups":
            # The names the multi-setup scope was asked for, beside what the program read back.
            result["scope_setups"] = setup_names
        if unresolved:
            # Non-fatal (the file landed); name what the program did not expose so a caller can confirm.
            result["params_unresolved"] = unresolved
    if not clean:
        # A file appeared but the post flagged failure or the program faulted - surface both facts AND
        # the post log's error lines so the caller sees the real reason, not just "review the output".
        result["partial"] = True
        result["program_error"] = safe(lambda: program.error) if program_error else None
        log_errors = _post_log_errors(prog_name, started)
        if log_errors:
            result["post_log"] = log_errors
        if failed_markers:
            result["failed_stubs"] = failed_markers
        reason = (" ".join(log_errors) if log_errors
                  else f"postProcess={bool(posted)}, program_error={program_error}")
        result["note"] = (f"Post did not report clean success - review before running. {reason}")
        return ok(result)

    if as_is:
        result["note"] = (f"Program '{prog_name}' posted AS-IS from its stored configuration - "
                          f"{len(files)} file(s), nothing reconfigured. 'readiness' carries its "
                          "health. " + _COUNTS_NOTE)
    else:
        result["note"] = (f"Program '{prog_name}' {program_verb}, posted {len(files)} file(s). "
                          "'readiness' carries its health. " + _COUNTS_NOTE)
        if membership.get("membership_note"):
            # The program's own membership read disagreed with the scope, or could not be compared -
            # stated beside the file rather than left in a key the note never mentions.
            result["note"] += " Membership: " + membership["membership_note"]
    return ok(result)


TOOL_DESCRIPTION = (
    "Create (or reuse) an NC Program and post it to a G-code / NC file on disk - the final CAM "
    "step. 'scope' or 'setups' picks the toolpaths; only VALID ones post - run cam_generate "
    "first.\n"
    + _outputs.produces_block(RETURNS)
)

tool = (
    Tool.create_simple(name="cam_post", description=TOOL_DESCRIPTION)
    .add_input_property("scope", {"type": "string"})
    .add_input_property("post", {"type": "string",
            "description": "post_scope=local: a .cps path or a name in the post folder. Otherwise a post NAME in that library."})
    .add_input_property(_POST_SCOPE_CHOICE.name, _POST_SCOPE_CHOICE.schema())
    .add_input_property("output_folder", {"type": "string"})
    .add_input_property("program_name", {"type": "string",
            "description": "An existing program with this name is reused."})
    .add_input_property(_UNITS_CHOICE.name, _UNITS_CHOICE.schema())
    .add_input_property("program_comment", {"type": "string"})
    .add_input_property("overwrite", {"type": "boolean"})
    .add_input_property("setups", {"type": "array", "items": {"type": "string"}})
    .strict_schema()
)

# DeliverablesExist is a REDUNDANT gate here: the handler's snapshot-diff verification stays (it
# CONSTRUCTS files[] and drives the partial/rollback logic); the kernel re-stats every claimed
# deliverable so a claim that drifted from disk reality can never ship as success.
item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             postconditions=[_assert.DeliverablesExist()])


def register_tool():
    register(item)
