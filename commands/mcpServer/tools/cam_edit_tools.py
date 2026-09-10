# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Manage CAM tools across the document / local / cloud / hub tool libraries: list, add, remove, edit
parameters, add/remove a named preset on an existing tool, find where a tool is used, or create a new
shared library. The fusion scope is the shipped sample libraries - readable, never written. Hub
libraries can't be created via the API (importToolLibrary fails there) - create those in the UI."""

import adsk.core
import adsk.cam

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item, Verification
from ..mcp_primitives.registry import register
from ._common import iter_collection, named_with_remainder, ok, error, safe
from ._cam_common import get_cam, expression_error, library_assets, quote_expression
from ._cam_presets import (_apply_preset_values, _persist_preset_change, _persisted_preset_names,
                           _preset_names, _preset_spec_error, _preset_tool, _presets_named)

app = adsk.core.Application.get()

_ACTIONS = ("list", "list_types", "parameters", "add", "remove", "edit", "add_preset",
            "remove_preset", "where_used", "create_library")
_SCOPES = ("document", "local", "cloud", "hub", "fusion")
# friendly scope -> LibraryLocations attr, for every shared scope (document hosts no shared library
# and can host no new one). The ONE table the resolve / list / create paths all read.
_SHARED_LOCATIONS = {"local": "LocalLibraryLocation", "cloud": "CloudLibraryLocation",
                     "hub": "HubLibraryLocation", "fusion": "Fusion360LibraryLocation"}

# The scope holding the libraries the installation ships - the same location _build_type_map clones
# its from_type samples out of. Every write action is refused there: a shipped asset is shared by
# every document on this installation, so it is copied out of, never edited in place.
_READ_ONLY_SCOPE = "fusion"
_WRITE_ACTIONS = ("add", "remove", "edit", "add_preset", "remove_preset", "create_library")
_SCOPE_READ_ONLY = (
    "scope='{scope}' is the libraries this Fusion installation ships, which this tool only READS - "
    "'{action}' was refused. Copy the tool out instead: action='list' at this scope for its "
    "libraries and their tools, then action='add' at scope='document' (or local) with "
    "add_tools=[{{library_url, index}}].")


# ── target abstraction: a uniform view over document-lib vs shared-lib ───────

class _Target:
    """Uniform interface the handler drives over a document or shared library: persist() commits a
    shared library, update_tool() commits a document edit, and refetch() re-reads - from the url
    for a shared library, which returns STORED state, from the live library for the document."""
    def __init__(self, lib, is_document, persist_fn=None, update_tool_fn=None, ops_fn=None,
                 refetch_fn=None):
        self._lib = lib
        self.is_document = is_document
        self._persist_fn = persist_fn
        self._update_tool_fn = update_tool_fn
        self._ops_fn = ops_fn
        self._refetch_fn = refetch_fn
        self._tools = None

    def _drop_tool_cache(self):
        """Forget the held tool list. Every method that writes to the library, commits it, or reads
        it again calls this: a list surviving one of those would hide the change it made."""
        self._tools = None

    def refetch(self):
        """The library read again - the stored library for a shared target, the live one for the
        document target; None when neither reads."""
        self._drop_tool_cache()
        return self._refetch_fn() if self._refetch_fn else None

    def _refetch_tool(self, index):
        lib = self.refetch()
        return safe(lambda: lib.item(index)) if lib is not None else None

    def persisted_count(self):
        """The tool count off the re-read library (None when it cannot be re-read) - for a shared
        target the proof a persist() actually landed, since updateToolLibrary returning true is not."""
        lib = self.refetch()
        return safe(lambda: lib.count) if lib is not None else None

    def reread_param(self, index, name):
        """The expression of one parameter off the re-read library (None when it cannot be re-read) -
        for a shared target the proof an edit() stored, for the document target the proof it is
        present; updateTool/updateToolLibrary returning true is neither."""
        t = self._refetch_tool(index)
        return safe(lambda: t.parameters.itemByName(name).expression) if t is not None else None

    def reread_preset_names(self, index):
        """The preset names of one tool off the re-read library, None when it cannot be re-read -
        the only read that covers a preset change, which moves no tool COUNT."""
        return _persisted_preset_names(self._refetch_tool(index))

    def stored_tool_numbers(self):
        """Every tool_number the STORED library holds, re-read from its url (None when it cannot be
        re-read) - the SET a caller matches an auto-assigned number against, however a persist
        orders the tools."""
        lib = self.refetch()
        if lib is None:
            return None
        return [_read_tool_number(t) for t in iter_collection(lib)]

    @property
    def tools(self):
        # A tool's INDEX is its address, so this stays a positional walk: iter_collection drops an
        # unreadable item, which would slide every later tool onto the wrong index. The list is held
        # until _drop_tool_cache(), which every mutating and re-reading method calls.
        if self._tools is None:
            self._tools = [safe(lambda i=i: self._lib.item(i))
                           for i in range(safe(lambda: self._lib.count, 0) or 0)]
        return self._tools

    def add(self, tool):
        self._drop_tool_cache()
        self._lib.add(tool)

    def remove(self, index):
        self._drop_tool_cache()
        self._lib.remove(index)

    def update_tool(self, tool):
        if self._update_tool_fn:
            self._drop_tool_cache()
            self._update_tool_fn(tool)

    def persist(self):
        if self._persist_fn:
            self._drop_tool_cache()
            self._persist_fn()

    def operations_by_tool(self, tool):
        ops = self._ops_fn(tool) if self._ops_fn else None
        if ops is None:
            return []
        # OperationVector is index/len accessible, not a Python list
        out = []
        try:
            for i in range(len(ops)):
                out.append(safe(lambda i=i: ops[i].name))
        except Exception:
            out.extend(safe(lambda o=o: o.name)
                       for o in iter_collection(ops))
        return out




def _tool_libraries():
    """The shared ToolLibraries - on CAMManager.get().libraryManager (NOT the document's CAM product,
    which has no libraryManager). Works without an open CAM job."""
    return safe(lambda: adsk.cam.CAMManager.get().libraryManager.toolLibraries)


def _shared_libraries(scope):
    """List (name, url) of the libraries at a shared scope, recursing folders (Hub/Cloud nest).
    Returns (entries, truncated, None) or (None, False, error). Patched in tests."""
    libs = _tool_libraries()
    if not libs:
        return None, False, "Tool libraries unavailable."
    loc = getattr(adsk.cam.LibraryLocations, _SHARED_LOCATIONS[scope])
    root = safe(lambda: libs.urlByLocation(loc))
    found, truncated = library_assets(libs, root)
    return [{"name": safe(lambda a=a: a.leafName), "url": safe(lambda a=a: a.toString())}
            for a in found], truncated, None


def _resolve_target(scope, library):
    """Return (_Target, None) for the scope, or (None, error). Patched in tests."""
    if scope == "document":
        cam, cerr = get_cam()        # document scope needs an open CAM document
        if cerr:
            return None, cerr
        dtl = safe(lambda: cam.documentToolLibrary)
        if dtl is None:
            return None, "No document tool library."
        return _Target(dtl, is_document=True,
                       update_tool_fn=lambda t: dtl.updateTool(t),
                       ops_fn=lambda t: safe(lambda: dtl.operationsByTool(t)),
                       # the document library re-read off the CAM product - the document's own state
                       # (doc_save is what stores it), so a read-back here proves presence, not storage
                       refetch_fn=lambda: safe(lambda: cam.documentToolLibrary)), None
    # shared library - no open document needed
    libs = _tool_libraries()
    if not libs:
        return None, "Tool libraries unavailable."
    loc = getattr(adsk.cam.LibraryLocations, _SHARED_LOCATIONS[scope])
    root = safe(lambda: libs.urlByLocation(loc))
    # collect libraries (recurse folders for Hub/Cloud) via the shared walk
    found, truncated = library_assets(libs, root)
    # A capped walk means an unlisted library may simply be beyond the bound - the refusal says
    # so instead of asserting absence.
    capped = " (folder walk hit its bound - a deeper library may exist unlisted)" if truncated else ""
    target = (library or "").strip()
    if not target:
        return None, f"Provide 'library' (name or url) for {scope} scope. Available: " \
                     f"{', '.join(safe(lambda a=a: a.leafName) for a in found)}.{capped}"
    lib_url = next((a for a in found if safe(lambda a=a: a.toString()) == target), None) \
        or next((a for a in found if safe(lambda a=a: a.leafName) == target), None)
    if lib_url is None:
        avail = [safe(lambda a=a: a.leafName) for a in found]
        return None, (f"No {scope} library '{target}'. Available: "
                      f"{', '.join(str(a) for a in avail)}.{capped}")
    lib = safe(lambda: libs.toolLibraryAtURL(lib_url))
    if not lib:
        return None, f"Could not load {scope} library '{target}'."
    # A write to this library makes any cached copy of it stale, so persist drops that entry.
    cache_key = safe(lambda: lib_url.toString())

    def _persist():
        _invalidate_library(cache_key)
        return libs.updateToolLibrary(lib_url, lib)

    return _Target(lib, is_document=False,
                   persist_fn=_persist,
                   refetch_fn=lambda: safe(lambda: libs.toolLibraryAtURL(lib_url))), None


# library url string -> the ToolLibrary already fetched for it. Loading one is a cloud round-trip
# costing seconds, and a type lookup reads the same library twice. The entries are LIVE objects held
# for the process life, so the dict is bounded and a persist DROPS the library it wrote.
_LIBRARY_CACHE_MAX = 8
_library_cache = {}


def _cache_library(key, lib):
    """Keep a fetched library under `key`, evicting the oldest entry past _LIBRARY_CACHE_MAX."""
    if not key or lib is None:
        return
    _library_cache[key] = lib
    while len(_library_cache) > _LIBRARY_CACHE_MAX:
        _library_cache.pop(next(iter(_library_cache)))


def _invalidate_library(key):
    """Drop the cached copy of a library that has just been written to."""
    if key:
        _library_cache.pop(key, None)


def _source_tool(library_url, index):
    """Fetch a Tool from a (library_url, index) reference. Patched in tests."""
    libs = safe(lambda: adsk.cam.CAMManager.get().libraryManager.toolLibraries)
    if not libs:
        return None, "Tool libraries unavailable."
    lib = _library_cache.get(library_url)
    if lib is None:
        url = safe(lambda: adsk.core.URL.create(library_url))
        lib = safe(lambda: libs.toolLibraryAtURL(url)) if url else None
        _cache_library(library_url, lib)
    if not lib:
        return None, f"Could not load source library '{library_url}'."
    n = safe(lambda: lib.count, 0) or 0
    if not (0 <= index < n):
        return None, f"tool_index {index} out of range for '{library_url}' ({n} tools)."
    t = safe(lambda: lib.item(index))
    return (t, None) if t is not None else (None, f"No tool at index {index}.")


# ── tool creation: clone a sample of a geometry type, build via JSON ─────────

import json as _json

_json_loads = _json.loads
_json_dumps = _json.dumps

# Fusion sample libraries that, together, hold one of every common geometry type. 'center drill'
# appears in no Metric sample library, which is why the one Inch library is here; 'Probes' is where
# the probe type lives and no cutting library carries it. Metric first: the first library wins.
_SAMPLE_LIBS = ("Milling Tools (Metric)", "Hole Making Tools (Metric)", "Cutting Tools (Metric)",
                "Turning Tools (Metric)", "Hole Making Tools (Inch)", "Probes")
_HOLDERS_LIB = "Holders (Metric)"
# ({tool_type: (library_url, index)}, {sample libraries already fetched}) - built INCREMENTALLY,
# because each library is a cloud round-trip (see _build_type_map).
_type_map_cache = None


def _tool_from_json(json_str):
    return adsk.cam.Tool.createFromJson(json_str)


def _quote(text):
    """The shared CAM expression codec, plus a doubled backslash for a value that holds one."""
    s = str(text)
    if "\\" not in s:
        return quote_expression(s)
    # Measured: the tool-parameter store spells a backslash DOUBLED. Sent through the shared codec
    # as written, the expression is accepted and the parameter's value reads '<UNSPECIFIED>'.
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _fusion360_child(leaf_substr):
    """A Fusion360 sample-library URL whose leaf contains leaf_substr, or None."""
    libs = safe(lambda: adsk.cam.CAMManager.get().libraryManager.toolLibraries)
    if not libs:
        return None, None
    root = safe(lambda: libs.urlByLocation(adsk.cam.LibraryLocations.Fusion360LibraryLocation))
    for a in (safe(lambda: list(libs.childAssetURLs(root)), []) or []):
        if leaf_substr in (safe(lambda a=a: a.leafName) or ""):
            return libs, a
    return libs, None


def _build_type_map(want=None):
    """{tool_type -> (library_url, index)} from the sample libraries, built incrementally: each one
    is a cloud fetch of several seconds, `want` stops at the type it names, and only a library that
    resolved AND was walked counts as scanned, so an empty transient fetch is retried."""
    global _type_map_cache
    if _type_map_cache is None:
        _type_map_cache = ({}, set())
    out, scanned = _type_map_cache
    if want and want in out:
        return out
    if len(scanned) == len(_SAMPLE_LIBS):
        return out
    libs = safe(lambda: adsk.cam.CAMManager.get().libraryManager.toolLibraries)
    if libs:
        root = safe(lambda: libs.urlByLocation(adsk.cam.LibraryLocations.Fusion360LibraryLocation))
        children = safe(lambda: list(libs.childAssetURLs(root)), []) or []
        for ln in _SAMPLE_LIBS:
            if ln in scanned:
                continue
            u = next((a for a in children if ln in (safe(lambda a=a: a.leafName) or "")), None)
            if not u:
                continue
            key = safe(lambda u=u: u.toString())
            lib = _library_cache.get(key) or safe(lambda: libs.toolLibraryAtURL(u))
            if lib is None:
                continue                      # the fetch failed - retry it on the next call
            _cache_library(key, lib)          # _source_tool reads the SAME library moments later
            # The index is the tool's ADDRESS in this library (_source_tool takes it), so the walk
            # keeps its own positions rather than the present-item positions iter_collection yields.
            for i in range(safe(lambda: lib.count, 0) or 0):
                ty = safe(lambda lib=lib, i=i: lib.item(i).parameters.itemByName("tool_type").value.value)
                if ty and ty not in out:
                    out[ty] = (key, i)
            scanned.add(ln)
            if want and want in out:
                break
    return out


def _sample_for_type(tool_type):
    """A sample Tool of the given geometry type (e.g. 'drill', 'ball end mill'), or (None, error)."""
    want = (tool_type or "").strip()
    tmap = _build_type_map(want)
    ref = tmap.get(want)
    if not ref:
        # A miss must list the FULL vocabulary, so fall back to the complete walk before refusing.
        tmap = _build_type_map()
        ref = tmap.get(want)
    if not ref:
        return None, (f"No sample tool of type '{tool_type}'. Available types: "
                      f"{', '.join(sorted(tmap.keys()))}.")
    return _source_tool(ref[0], ref[1])


def _holder_json(ref):
    """The holder JSON sub-dict for a {library_url, index} holder reference, or (None, error).
    A Holders-library item IS a holder doc (type='holder', has 'segments'); use it directly."""
    if not isinstance(ref, dict):
        return None, f"'holder' must be {{library_url, index}}; got {ref!r}."
    url, idx = ref.get("library_url"), ref.get("index")
    if url is None or idx is None:
        # convenience: no url given -> use the default Holders sample library
        if idx is None:
            return None, "'holder' needs an 'index' (and optionally a 'library_url')."
        libs, hu = _fusion360_child(_HOLDERS_LIB)
        if not hu:
            return None, "Default holders library not found; give an explicit 'library_url'."
        url = safe(lambda: hu.toString())
    htool, herr = _source_tool(url, idx)
    if herr:
        return None, herr
    hd = safe(lambda: _json_loads(htool.toJson()))
    if not isinstance(hd, dict):
        return None, "Could not read holder JSON."
    return (hd["holder"] if "holder" in hd else hd), None


# ── tool number: auto-assigned on add so multiple adds don't collide ──────────
# A cloned sample keeps the sample's tool_number, so two adds land at the same number and cam_post
# refuses. tool_number is an expression-settable integer parameter, read back via .value.value.
_P_TOOL_NUMBER = "tool_number"


def _read_tool_number(tool):
    """The tool's assigned tool_number as an int, or None if the parameter is absent/unreadable."""
    p = safe(lambda: tool.parameters.itemByName(_P_TOOL_NUMBER))
    if p is None:
        return None
    v = safe(lambda: p.value.value)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _set_tool_number(tool, number):
    """Set a tool's tool_number and read it back. (assigned_int, None) on success, (None, error) if
    the parameter is missing or the value did not land."""
    p = safe(lambda: tool.parameters.itemByName(_P_TOOL_NUMBER))
    if p is None:
        return None, "The tool has no 'tool_number' parameter - a free number could not be assigned."
    try:
        p.expression = str(int(number))
    except Exception as e:
        return None, f"Could not set tool_number to {number}: {e}."
    landed = _read_tool_number(tool)
    if landed != int(number):
        return None, (f"Set tool_number to {number} but it read back {landed!r} - "
                      "the number did not land.")
    return int(number), None


# ── per-tool summary ─────────────────────────────────────────────────────────

def _tp(tool, name, default=None):
    p = safe(lambda: tool.parameters.itemByName(name))
    return safe(lambda: p.value.value, default) if p else default


def _json_scalar(v):
    """A parameter's evaluated value coerced to a JSON-safe scalar; a non-scalar value type is
    str()'d so it is still reported, never dropped."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return safe(lambda: str(v))


def _tool_summary(tool, index):
    from ._cam_common import tool_holder
    dia = _tp(tool, "tool_diameter")
    summ = {
        "index": index,
        "number": _read_tool_number(tool),   # the tool NUMBER (auto-assigned on add; cam_post keys on it)
        "type": _tp(tool, "tool_type"),
        "diameter_mm": round(dia * 10.0, 4) if isinstance(dia, (int, float)) else None,
        "flutes": _tp(tool, "tool_numberOfFlutes"),
        "description": _tp(tool, "tool_description"),
    }
    # The CUTTING tool's OWN product identity (tool_productId/tool_vendor) - distinct from the holder's
    # product_id/vendor below. The add path sets and verifies these, so this list read confirms them.
    product_id = _tp(tool, "tool_productId")
    vendor = _tp(tool, "tool_vendor")
    if product_id:
        summ["tool_product_id"] = product_id
    if vendor:
        summ["tool_vendor"] = vendor
    holder = tool_holder(tool)   # assigned holder identity - shown only when the tool carries one
    if holder:
        summ["holder"] = holder
    return summ


# ── actions ──────────────────────────────────────────────────────────────────

def _do_list(target, tool_type=""):
    tfilter = (tool_type or "").strip().lower()
    tools = []
    for i, t in enumerate(target.tools):
        summ = _tool_summary(t, i)
        if tfilter and tfilter not in str(summ.get("type") or "").lower():
            continue
        tools.append(summ)
    out = {"tool_count": len(tools), "tools": tools,
           "note": "Summary rows only (diameter/flutes/type/description/number/product identity). "
                   "For a tool's FULL parameter list - every dimension by name/expression/value - "
                   "call action='parameters' with that tool's index."}
    if tfilter:
        out["filtered_by_type"] = tool_type
    return ok(out)


def _do_list_libraries(scope):
    entries, truncated, lerr = _shared_libraries(scope)
    if lerr:
        return error(lerr)
    out = {"scope": scope, "library_count": len(entries), "libraries": entries,
           "note": "Pass 'library' = one of these (name or url) to list/manage its tools. Tool "
                   "references are (library_url, index)."}
    if truncated:
        out["truncated"] = True
        out["note"] += (" The folder walk hit its bound - a deeper library may exist unlisted.")
    return ok(out)


def _do_list_types():
    """The tool-type vocabulary _build_type_map() discovers by walking the bundled sample libraries -
    the same map action='add' resolves 'from_type' against. No document/CAM product/scope/library
    needed (replaces harvesting the vocabulary off a deliberately-failing add probe)."""
    tmap = _build_type_map()
    if not tmap:
        return error("Could not read the sample tool libraries - tool types are unavailable.")
    return ok({"type_count": len(tmap), "types": sorted(tmap.keys()),
               "note": "Pass one of these as add_tools[].from_type to clone a sample of that type."})


def _build_entry(ref):
    """(tool, None) or (None, error) for one add entry - {from_type} clones a sample of that
    geometry type, {library_url, index} copies an existing tool, and description / diameter /
    product_id / vendor / holder / presets are applied over it."""
    if not isinstance(ref, dict):
        return None, f"Each add_tools entry must be an object; got {ref!r}."

    # 1) get the SOURCE tool (by type-clone or by reference)
    if ref.get("from_type"):
        src, serr = _sample_for_type(ref["from_type"])
        if serr:
            return None, serr
    elif ref.get("library_url") is not None and ref.get("index") is not None:
        src, serr = _source_tool(ref.get("library_url"), ref.get("index"))
        if serr:
            return None, serr
    else:
        return None, (f"Entry {ref!r} needs 'from_type' (clone a sample of that type) or "
                      "'library_url'+'index' (copy an existing tool).")

    # 2) optional holder to ASSIGN (resolve before mutating). PRESENCE gates, not truthiness: an
    # empty {} holder ref is a malformed request, which a truthy gate ships as the sample's holder.
    holder_json = None
    if ref.get("holder") is not None:
        hd, herr = _holder_json(ref["holder"])
        if herr:
            return None, herr
        holder_json = hd

    # 3) build via JSON: clone source, apply overrides + holder
    d = safe(lambda: _json_loads(src.toJson()))
    if not isinstance(d, dict):
        return None, "Could not read the source tool's JSON."
    if ref.get("description"):
        d["description"] = str(ref["description"])
    if holder_json is not None:
        d["holder"] = holder_json
    tool = safe(lambda: _tool_from_json(_json_dumps(d)))
    if tool is None:
        return None, "Could not create the tool from JSON."
    # diameter override (after creation, on the param). A missing parameter means the requested
    # override cannot apply - error instead of adding the tool without it.
    if ref.get("diameter") is not None:
        p = safe(lambda: tool.parameters.itemByName("tool_diameter"))
        if p is None:
            return None, "The tool has no 'tool_diameter' parameter - the requested diameter override cannot apply."
        p.expression = str(ref["diameter"])

    # 3b) product_id / vendor are real tool parameters but NOT part of createFromJson's schema,
    # which drops those keys silently - so they are applied as quoted-string expressions after
    # creation and read back.
    for field, pname in (("product_id", "tool_productId"), ("vendor", "tool_vendor")):
        val = ref.get(field)
        if val is None:
            continue
        val = str(val)
        p = safe(lambda pname=pname: tool.parameters.itemByName(pname))
        if p is None:
            return None, f"The tool has no '{pname}' parameter - the requested {field} cannot apply."
        try:
            p.expression = _quote(val)
        except Exception as e:
            return None, f"Could not set {pname} = {val!r}: {e}."
        eerr, _ = expression_error(p)
        if eerr:
            return None, f"Set {pname} but it failed to evaluate: {eerr}."
        landed = safe(lambda p=p: p.value.value)
        if landed != val:
            return None, (f"Set {pname}'s expression but it read back {landed!r} instead of "
                          f"{val!r} - the {field} did not land.")

    # 4) presets - same rule: a preset that cannot be created or populated is an error, not a skip
    for ps in (ref.get("presets") or []):
        serr = _preset_spec_error(ps)
        if serr:
            return None, serr
        preset = safe(lambda: tool.presets.add())
        if preset is None:
            return None, "Could not add a preset to the tool - the requested presets were not applied."
        perr = _apply_preset_values(preset, ps)
        if perr:
            return None, perr
    return tool, None


def _do_add(target, add_tools):
    if not add_tools:
        return error("Provide 'add_tools' - entries to add. Each: {from_type:'drill'} (create from a "
                     "sample of that type) or {library_url, index} (copy an existing tool); optional "
                     "'description'/'diameter'/'product_id'/'vendor' overrides, "
                     "'holder':{library_url,index}, 'presets':[...].")
    # build ALL entries before adding any (no partial write on an error)
    built = []
    for ref in add_tools:
        t, terr = _build_entry(ref)
        if terr:
            return error(terr)
        built.append(t)
    # Auto-assign a FREE tool_number to each new tool. A cloned sample keeps the sample's number, so
    # two adds would collide and cam_post refuses duplicate tool numbers; hand out the next free one
    # (skipping every number already in the library, and each one just assigned in this call).
    used = {n for n in (_read_tool_number(t) for t in target.tools) if n is not None}
    assigned = []
    nxt = 1
    for t in built:
        while nxt in used:
            nxt += 1
        num, nerr = _set_tool_number(t, nxt)
        if nerr:
            return error(nerr)
        used.add(num)
        assigned.append(num)
    for t in built:
        target.add(t)
    if not target.is_document:
        target.persist()
        got = target.persisted_count()
        if got is not None and got != len(target.tools):
            return error(f"updateToolLibrary reported success but the library re-read from its url "
                         f"holds {got} tool(s), not {len(target.tools)} - the persist did not land.")
    # Honesty read-back, first rung: each IN-MEMORY tool object's number after the add/persist -
    # this catches an assignment that did not stick on the object.
    landed = [_read_tool_number(t) for t in built]
    if landed != assigned:
        return error(f"Auto-assigned tool numbers {assigned} but after the add they read back "
                     f"{landed} - the tool-number assignment did not persist.")
    # Second rung, SHARED targets only: the numbers the STORED library holds, the only read that
    # shows a persist-side renumber. The document library has no url to re-read.
    in_memory_only = True
    if not target.is_document:
        stored_numbers = target.stored_tool_numbers()
        in_memory_only = stored_numbers is None
        if not in_memory_only:
            missing = [n for n in assigned if n not in stored_numbers]
            if missing:
                return error(f"Auto-assigned tool number(s) {missing} but the library re-read from "
                             f"its url holds numbers {stored_numbers} - the assignment did not "
                             "reach the stored library.")
    return ok({"added": len(built), "tool_count": len(target.tools),
               "assigned_tool_numbers": assigned,
               # the honest basis of the numbers above: the stored library agreed, or nothing but the
               # in-memory tools was checked (always the case for a document target)
               "verified_in_memory_only": in_memory_only,
               "note": (_persist_note(target, "Tools added", in_memory_only)
                        + f" Auto-assigned free tool number(s) {assigned} (next free per tool, so "
                        "multiple adds do not collide - cam_post refuses duplicate tool numbers).")})


def _do_remove(target, indices):
    if not indices:
        return error("Provide 'remove_indices' - the tool indices to remove.")
    n = len(target.tools)
    bad = [i for i in indices if not (0 <= i < n)]
    if bad:
        return error(f"Index/indices out of range (library has {n} tools): {', '.join(map(str, bad))}.")
    # remove high-to-low so earlier indices stay valid
    for i in sorted(set(indices), reverse=True):
        target.remove(i)
    if not target.is_document:
        target.persist()
        got = target.persisted_count()
        if got is not None and got != len(target.tools):
            return error(f"updateToolLibrary reported success but the library re-read from its url "
                         f"holds {got} tool(s), not {len(target.tools)} - the persist did not land.")
    return ok({"removed": len(set(indices)), "tool_count": len(target.tools)})


def _formula_source(params, name, before_expr):
    """The parameter `before_expr` tracks when it is exactly another parameter's NAME on this tool -
    a formula-derived value an edit overwrites silently - else None."""
    ref = (before_expr or "").strip()
    if not ref or ref == name:
        return None
    return ref if safe(lambda: params.itemByName(ref)) is not None else None


def _do_edit(target, tool_index, parameters):
    tools = target.tools
    if tool_index is None or not (0 <= tool_index < len(tools)):
        return error(f"Provide a valid 'tool' index (0..{len(tools) - 1}).")
    if not parameters:
        return error("Provide 'parameters' {name: expression} to set on the tool.")
    tool = tools[tool_index]
    params = safe(lambda: tool.parameters)
    # validate ALL parameter names before applying any
    resolved = {}
    missing = []
    for name in parameters:
        p = safe(lambda name=name: params.itemByName(name)) if params else None
        (resolved.__setitem__(name, p) if p is not None else missing.append(name))
    if missing:
        return error(f"Tool has no parameter(s): {', '.join(missing)}. (Read the tool's parameters first.)")
    changed = []
    warnings = []
    eval_failures = []
    for name, expr in parameters.items():
        p = resolved[name]
        before = safe(lambda p=p: p.expression)
        src = _formula_source(params, name, before)
        if src:
            warnings.append(f"'{name}' was formula-derived (expression was '{src}', tracking that "
                            f"parameter) - this edit overwrites that internal relationship; the tool "
                            f"no longer keeps '{name}' equal to '{src}'.")
        try:
            p.expression = str(expr)
        except Exception as e:
            return error(f"Could not set '{name}' = '{expr}': {e}. "
                         f"(Applied: {', '.join(c['name'] for c in changed) or 'none'}.)")
        # A tool parameter STORES an expression it cannot evaluate and echoes it back verbatim, so
        # only .error reveals it - read here on the in-memory tool, before the persist below.
        eval_err, eval_warn = expression_error(p)
        rec = {"name": name, "before": before, "after": safe(lambda p=p: p.expression)}
        if eval_warn:
            rec["warning"] = eval_warn
        changed.append(rec)
        if eval_err:
            eval_failures.append((name, str(expr), eval_err))
    # An expression the platform stored but could not EVALUATE is a swallowed no-op it reports as
    # success. Restore every parameter this call touched and return before the persist below, so the
    # library keeps what it held; a restore that will not read back is named rather than assumed.
    if eval_failures:
        unrestored = []
        for rec in changed:
            p = resolved[rec["name"]]
            safe(lambda p=p, rec=rec: setattr(p, "expression", rec["before"]))
            if safe(lambda p=p: p.expression) != rec["before"]:
                unrestored.append(rec["name"])
        detail = "; ".join(f"'{n}' = '{e}' ({why})" for n, e, why in eval_failures)
        left = (f" ROLLBACK INCOMPLETE - {', '.join(unrestored)} did NOT read back its prior "
                "expression; re-read the tool.") if unrestored else ""
        return error(f"Tool {tool_index}: expression did not evaluate - {detail}. Rolled back all "
                     f"{len(changed)} parameter(s) and did not commit, so the library keeps what it "
                     f"held.{left} (A tool expression must reference parameters this tool carries "
                     "and resolve to a value - check names and units.)")
    # persist
    if target.is_document:
        target.update_tool(tool)
    else:
        target.persist()
    # updateTool/updateToolLibrary returning is NOT proof the edit stored, and an edit moves no tool
    # COUNT, so the tool is re-fetched and ONE edited expression is confirmed.
    check = changed[0]
    stored = target.reread_param(tool_index, check["name"])
    if stored is not None and str(stored) != str(check["after"]):
        return error(f"Edited '{check['name']}' to '{check['after']}' but the tool re-read from the "
                     f"library holds '{stored}' - the edit did not persist.")
    # verified_in_memory_only = nothing proved the edit reached STORAGE: the library did not re-read,
    # or this is the document library, whose read-back shows presence only.
    in_memory_only = target.is_document or stored is None
    note = _persist_note(target, "Tool edited", stored is None)
    if target.is_document:
        note += " " + _OP_TOOL_COPY_NOTE
    out = {"edited": len(changed), "tool": tool_index, "changed": changed,
           "verified_in_memory_only": in_memory_only,
           "note": note}
    if warnings:
        out["warnings"] = warnings
    return ok(out)


_OP_TOOL_COPY_NOTE = ("Operations already created keep their own copy of this tool - "
                      "cam_edit_operation(tool_scope, tool_index) re-assigns one.")


def _persist_note(target, act, in_memory_only):
    """The note for a completed write - what the read-back PROVED: nothing where no library read
    happened, storage for a shared library re-read from its url, presence for the document one."""
    if in_memory_only:
        return (f"{act}, but the library was not read back - confirmed on the in-memory tool only."
                + (" doc_save stores the document." if target.is_document else ""))
    if target.is_document:
        return (f"{act}, and read back from the document tool library - a document read-back shows "
                "the change is present, not that it was stored; doc_save stores the document.")
    return f"{act} and persisted (re-read from the library url)."


def _preset_note(target, done, in_memory_only=False):
    """The payload note for a completed preset change ('added to' / 'removed from')."""
    return (_persist_note(target, f"Preset {done} the tool", in_memory_only)
            + " 'presets' lists this tool's preset names.")


def _do_add_preset(target, tool_index, spec):
    """Add ONE named preset (its own cutting data) to a tool already in the library."""
    tool, presets, name, gerr = _preset_tool(target, tool_index, spec)
    if gerr:
        return error(gerr)
    if _presets_named(presets, name):
        return error(f"The tool already has a preset named '{name}' - remove it first "
                     "(action='remove_preset') or pick another name.")
    at = safe(lambda: presets.count, 0) or 0   # add() appends, so the new preset lands at this index
    preset = safe(lambda: presets.add())
    if preset is None:
        return error(f"Could not add a preset to the tool at index {tool_index} - none was created.")
    try:
        verr = _apply_preset_values(preset, dict(spec, name=name))
    except Exception as e:
        verr = f"Could not populate the new preset '{name}': {e}."
    if verr:
        # add() has already appended the preset, so a value that will not apply would leave a
        # half-populated preset on the tool - drop it again and report why nothing was added.
        if safe(lambda: presets.remove(at), False) is False:
            verr += f" The preset add() created at index {at} could not be removed again."
        return error(verr)
    matches = _presets_named(presets, name)
    if len(matches) != 1:
        return error(f"Added a preset named '{name}' but the tool's presets read back as "
                     f"{_preset_names(presets)} - the add did not take.")
    stored, perr = _persist_preset_change(target, tool, tool_index, name, expect_present=True)
    if perr:
        return error(perr)
    return ok({"tool": tool_index, "preset": name, "preset_index": matches[0][0],
               "preset_count": safe(lambda: presets.count, 0),
               "presets": stored if stored is not None else _preset_names(presets),
               "verified_in_memory_only": target.is_document or stored is None,
               "note": _preset_note(target, "added to", stored is None)})


def _do_remove_preset(target, tool_index, spec):
    """Remove ONE named preset from a tool already in the library."""
    tool, presets, name, gerr = _preset_tool(target, tool_index, spec)
    if gerr:
        return error(gerr)
    matches = _presets_named(presets, name)
    if not matches:
        avail = [n for n in _preset_names(presets) if n]
        return error(f"The tool has no preset named '{name}'. Presets on this tool: "
                     f"{named_with_remainder(avail) if avail else '(none)'}.")
    if len(matches) > 1:
        return error(f"'{name}' names {len(matches)} presets on this tool (indices "
                     f"{', '.join(str(i) for i, _ in matches)}) - the removal is refused rather "
                     "than picking one of them.")
    index = matches[0][0]
    removed = presets.remove(index)
    if removed is False:
        return error(f"ToolPresets.remove({index}) reported failure - preset '{name}' was not removed.")
    survivors = _presets_named(presets, name)
    if survivors:
        return error(f"Removed preset '{name}' at index {index} but {len(survivors)} preset(s) of "
                     "that name survive on the tool - the removal did not take.")
    stored, perr = _persist_preset_change(target, tool, tool_index, name, expect_present=False)
    if perr:
        return error(perr)
    return ok({"tool": tool_index, "preset": name, "removed_index": index,
               "preset_count": safe(lambda: presets.count, 0),
               "presets": stored if stored is not None else _preset_names(presets),
               "verified_in_memory_only": target.is_document or stored is None,
               "note": _preset_note(target, "removed from", stored is None)})


def _do_where_used(target, tool_index):
    if not target.is_document:
        return error("'where_used' is only available for the document library (scope='document') - a "
                     "shared library has no operations.")
    tools = target.tools
    if tool_index is None or not (0 <= tool_index < len(tools)):
        return error(f"Provide a valid 'tool' index (0..{len(tools) - 1}).")
    tool = tools[tool_index]
    ops = target.operations_by_tool(tool)
    return ok({"tool": tool_index, "description": _tp(tool, "tool_description"),
               "operation_count": len(ops), "operations": ops,
               "note": "Operations that use this tool." if ops else "This tool is not used by any operation."})


def _do_parameters(target, tool_index):
    """The full parameter list of ONE tool - the deeper read the list summary points to. Reports each
    parameter's name, expression, and evaluated value where readable; only what the API exposes, no
    guessed names. 'formula_source' flags a parameter whose expression IS another parameter's name
    (it tracks that parameter rather than holding a literal; an edit overwrites that relationship)."""
    tools = target.tools
    if tool_index is None or not (0 <= tool_index < len(tools)):
        return error(f"Provide a valid 'tool' index (0..{len(tools) - 1}).")
    tool = tools[tool_index]
    params = safe(lambda: tool.parameters)
    rows = []
    for p in iter_collection(params):
        name = safe(lambda p=p: p.name)
        expr = safe(lambda p=p: p.expression)
        row = {"name": name, "expression": expr,
               "value": _json_scalar(safe(lambda p=p: p.value.value))}
        src = _formula_source(params, name, expr)
        if src:
            row["formula_source"] = src
        rows.append(row)
    return ok({"tool": tool_index, "description": _tp(tool, "tool_description"),
               "parameter_count": len(rows), "parameters": rows,
               "note": "Every parameter's name/expression/value (value is null where unreadable). "
                       "'formula_source' marks a parameter tracking another (editing it overwrites "
                       "that relationship). Set one with action='edit'."})


def _empty_library():
    return adsk.cam.ToolLibrary.createEmpty()


def _do_create_library(scope, name, seed_tools):
    """Create + persist a NEW tool library at a shared scope (Local/Cloud/Hub). Fusion360 is read-only;
    document scope can't host a new library. Seeds validated before the persistent write."""
    if scope == "document":
        return error("Cannot create a library in the document scope. Use scope=local/cloud/hub.")
    name = (name or "").strip()
    if not name:
        return error("Provide 'library' as the new library's name (create_library).")
    libs = _tool_libraries()
    if not libs:
        return error("Tool libraries unavailable.")
    root = safe(lambda: libs.urlByLocation(getattr(adsk.cam.LibraryLocations, _SHARED_LOCATIONS[scope])))
    if not root:
        return error(f"Could not resolve the '{scope}' library root.")
    # Hub can't import at the bare hub:// root - descend to its team folder.
    if scope == "hub":
        child = safe(lambda: list(libs.childFolderURLs(root)), []) or []
        if not child:
            return error("No hub folder to create the library in.")
        root = child[0]
    # resolve seed tools BEFORE the persistent write
    resolved = []
    for ref in (seed_tools or []):
        if not isinstance(ref, dict):
            return error(f"Each seed entry must be {{library_url, index}}; got {ref!r}.")
        t, terr = _source_tool(ref.get("library_url"), ref.get("index"))
        if terr:
            return error(terr)
        resolved.append(t)
    lib = safe(lambda: _empty_library())
    if not lib:
        return error("Could not create an empty tool library.")
    for t in resolved:
        safe(lambda t=t: lib.add(t))
    try:
        new_url = libs.importToolLibrary(lib, root, name)
    except Exception as e:
        hint = (" Hub team libraries use a different write path importToolLibrary doesn't satisfy - "
                "create Hub libraries in the UI." if scope == "hub" else "")
        return error(f"Creating library '{name}' at {scope} failed: {e}.{hint}")
    if not new_url:
        return error(f"Creating library '{name}' at {scope} returned no URL.")
    if safe(lambda: libs.toolLibraryAtURL(new_url)) is None:
        return error(f"importToolLibrary returned a URL but no library loads back from it - the "
                     "create did not land.")
    return ok({"created_library": name, "scope": scope, "url": safe(lambda: new_url.toString()),
               "tool_count": safe(lambda: lib.count, len(resolved)),
               "note": "Library created and persisted. List it with action='list'. (Local=disk, "
                       "Cloud/Hub=your Autodesk account; a duplicate name gets a numeric suffix.)"})


def read_library(scope: str = "document", library: str = "", tool_type: str = "") -> dict:
    """The READ-ONLY library listing, shared with cam_get(include=['library']). Lists the tools in the
    target library (or, for a shared scope with no 'library', the libraries at that location). The write
    actions (add/remove/edit) stay on the cam_edit_tools tool - this is just the read half."""
    scope = (scope or "document").strip().lower()
    if scope not in _SCOPES:
        return error(f"Unknown scope '{scope}'. Use one of: {', '.join(_SCOPES)}.")
    if scope != "document" and not (library or "").strip():
        return _do_list_libraries(scope)
    target, terr = _resolve_target(scope, library)
    if terr:
        return error(terr)
    return _do_list(target, tool_type)


def handler(action: str = "list", scope: str = "document", library: str = "",
            add_tools=None, remove_indices=None, tool=None, parameters=None,
            tool_type: str = "", preset=None) -> dict:
    """See TOOL_DESCRIPTION."""
    action = (action or "list").strip().lower()
    if action not in _ACTIONS:
        return error(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.")
    scope = (scope or "document").strip().lower()
    if scope not in _SCOPES:
        return error(f"Unknown scope '{scope}'. Use one of: {', '.join(_SCOPES)}.")
    if scope == _READ_ONLY_SCOPE and action in _WRITE_ACTIONS:
        return error(_SCOPE_READ_ONLY.format(scope=scope, action=action))

    # create_library: the target doesn't exist yet - 'library' is the NEW name. Dispatch before resolve.
    if action == "create_library":
        return _do_create_library(scope, library, add_tools)

    # list is the READ half - one implementation, also surfaced as cam_get(include=['library']).
    if action == "list":
        return read_library(scope, library, tool_type)
    # list_types needs no scope/library/document - _build_type_map reads the bundled sample libraries
    # directly (dispatched here, before _resolve_target, same as create_library above).
    if action == "list_types":
        return _do_list_types()

    target, terr = _resolve_target(scope, library)
    if terr:
        return error(terr)

    if action == "add":
        return _do_add(target, add_tools or [])
    if action == "remove":
        return _do_remove(target, remove_indices or [])
    if action == "edit":
        return _do_edit(target, tool, parameters)
    if action == "add_preset":
        return _do_add_preset(target, tool, preset)
    if action == "remove_preset":
        return _do_remove_preset(target, tool, preset)
    if action == "where_used":
        return _do_where_used(target, tool)
    if action == "parameters":
        return _do_parameters(target, tool)
    return error(f"Unhandled action '{action}'.")


TOOL_DESCRIPTION = (
    "Read and manage CAM TOOL LIBRARIES and their tools - list, add, remove or edit tools, "
    "manage presets, or create a library. Writes persist."
)

tool = (
    Tool.create_simple(name="cam_edit_tools", description=TOOL_DESCRIPTION)
    .add_input_property("action", {"type": "string", "enum": list(_ACTIONS)})
    .add_input_property("scope", {"type": "string", "enum": list(_SCOPES)})
    .add_input_property("library", {"type": "string"})
    .add_input_property("add_tools", {"type": "array",
            "items": {"type": "object", "properties": {
                "from_type": {"type": "string"}, "library_url": {"type": "string"}, "index": {"type": "integer"},
                "description": {"type": "string"}, "diameter": {"type": "string"},
                "product_id": {"type": "string"}, "vendor": {"type": "string"},
                "holder": {"type": "object", "properties": {"library_url": {"type": "string"}, "index": {"type": "integer"}}},
                "presets": {"type": "array", "items": {"type": "object", "properties": {
                    "name": {"type": "string"},
                    "spindle_speed": {"type": ["number", "string"]},
                    "feed": {"type": ["number", "string"]}}}}}},
            "description": "{from_type:'drill'} clones a sample; {library_url,index} copies one."})
    .add_input_property("remove_indices", {"type": "array", "items": {"type": "integer"}})
    .add_input_property("tool", {"type": "integer",
            "description": "Tool index, for the per-tool actions."})
    .add_input_property("parameters", {"type": "object"})
    .add_input_property("tool_type", {"type": "string",
            "description": "list: tool-type substring, e.g. 'ball'."})
    .add_input_property("preset", {"type": "object", "properties": {
                "name": {"type": "string"}, "spindle_speed": {"type": ["number", "string"]},
                "feed": {"type": ["number", "string"]}},
            "description": "A bare number is rpm / mm-per-min in the document's units; a string carries its own ('35in/min')."})
    .strict_schema()
)
item = Item.create_tool_item(
    tool=tool, write="write", handler=handler, run_on_main_thread=True,
    # Every write arm re-reads through Target.refetch and errors on a disagreement; a target whose
    # refetch cannot answer publishes verified_in_memory_only rather than claiming storage.
    verification=Verification(
        kind="inline",
        evidence_test="tests/unit/test_cam_edit_tools.py::TestEdit"
                      "::test_persist_readback_mismatch_bites",
        rung="value"))


def register_tool():
    register(item)
