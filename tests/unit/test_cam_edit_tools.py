"""Unit tests for ``cam_edit_tools`` — read & manage CAM tool libraries + their tools.

The adsk.cam API is mocked; what we pin is the tool's OWN logic: the action dispatch (list / add /
remove / edit / where_used), resolving the target library by scope, and the per-action behaviour —
adding multiple tools by (library_url, index) reference, removing multiple by index (high-to-low so
indices stay valid), editing named tool parameters then persisting, and where_used (document scope
only). Plus the guards (unknown action/scope, library not found, where_used outside document, bad
tool reference, out-of-range remove index) and that every WRITE persists (document: updateTool /
shared: a persist callback).

The tool exposes seams so the test supplies a target without the real adsk plumbing:
  _resolve_target(scope, library) -> (target, error)  where target is a small object the handler drives.
"""

import json
import re
from types import SimpleNamespace

import adsk.cam
import pytest

from conftest import (FakeCAMParameter, FakeCAMParameters, FakeOperation, FakeTool,
                      _NamedCollection, load_tool)

_LOCATIONS = adsk.cam.LibraryLocations

ct = load_tool("cam_edit_tools")
cp = load_tool("_cam_presets")          # the preset helpers cam_edit_tools' preset paths run through
cc = load_tool("_cam_common")           # the shared expression codec _quote delegates to


# ── fakes ────────────────────────────────────────────────────────────────────

def _val(v):
    """A CAMParameter's `value` - whose own .value is the payload, the second hop a value read makes."""
    return SimpleNamespace(value=v)


class _Param(FakeCAMParameter):
    """Setting the expression to a quoted-string literal ("'text'") updates .value to the unquoted
    text - mirroring live ModelParameter behavior for a string parameter set that way
    (tool_productId/tool_vendor). A plain numeric/unquoted expression leaves .value untouched, same as
    every other existing test in this file expects."""
    def __init__(self, name, expr):
        super().__init__(name, expr, value=expr)

    @FakeCAMParameter.expression.setter
    def expression(self, v):
        FakeCAMParameter.expression.fset(self, v)
        if isinstance(v, str) and len(v) >= 2 and v[0] == v[-1] == "'":
            self.value = _val(v[1:-1])
        elif isinstance(v, str) and v.lstrip("-").isdigit():
            # a bare integer expression evaluates into the value (mirrors a ModelParameter set that
            # way - e.g. tool_number, which cam_edit_tools sets via .expression and reads via .value)
            self.value = _val(int(v))
        elif isinstance(v, str):
            evaluated = _evaluate(v)
            if evaluated is not None:
                self.value = _val(evaluated)


_IN_PER_MIN = re.compile(r"^([+-]?[\d.]+)\s*in/min$")


def _evaluate(expr):
    """A CAM parameter's evaluated value for the expression forms these tests use, or None when the
    expression evaluates to nothing (leaving .value where it was, which is what a preset parameter
    given an unevaluable expression does live). A feed carrying inch units converts to mm/min."""
    try:
        return float(expr.strip())
    except ValueError:
        pass
    m = _IN_PER_MIN.match(expr.strip())
    return float(m.group(1)) * 25.4 if m else None


def _Params(d):
    """A tool's or preset's parameters from {name: expression}."""
    return FakeCAMParameters([_Param(k, v) for k, v in d.items()])


def _replace(parameters, param):
    """Swap `param` in for the one of that name - how a test gives one parameter a scenario shape."""
    items = parameters._coll._items
    for i, existing in enumerate(items):
        if existing.name == param.name:
            items[i] = param
            return param
    items.append(param)
    return param


def _drop(parameters, name):
    """Take one parameter out entirely - the parameter a tool does not carry at all."""
    items = parameters._coll._items
    items[:] = [p for p in items if p.name != name]


# A preset's parameter set is the owning tool's cutting data, and that set varies by tool CLASS: a
# mill turns at a spindle speed, a turning tool that cuts at constant surface speed carries
# tool_surfaceSpeed and no tool_spindleSpeed at all.
_MILL_CUTTING_DATA = {"tool_spindleSpeed": "0", "tool_feedCutting": "0"}
_TURNING_CUTTING_DATA = {"tool_surfaceSpeed": "0", "tool_feedCutting": "0"}


class _Preset:
    def __init__(self, name="", params=None):
        self.name = name
        self.parameters = _Params(dict(params if params is not None else _MILL_CUTTING_DATA))


class _Presets(_NamedCollection):
    """ToolPresets: add() appends a preset carrying the owning tool's cutting-data parameters,
    remove(index) deletes by index and reports whether it deleted."""
    def __init__(self, names=(), owner=None):
        self._owner_params = dict(owner.preset_params) if owner is not None else dict(_MILL_CUTTING_DATA)
        super().__init__([_Preset(n, self._owner_params) for n in names])
    def add(self):
        p = _Preset("", self._owner_params)
        self._items.append(p)
        return p
    def remove(self, index):
        del self._items[index]
        return True


def _preset_names(tool):
    return [tool.presets.item(i).name for i in range(tool.presets.count)]


class _Tool(FakeTool):
    """A library tool: the parameters every read here goes through (tool_description carries the
    description), the presets carrying this tool's own cutting data, and toJson - the text a copy is
    rebuilt from. `desc` is this file's shorthand for the description it was built under."""

    def __init__(self, desc, preset_params=None, **params):
        params.setdefault("tool_description", desc)
        params.setdefault("tool_diameter", params.get("tool_diameter", "1.0"))
        params.setdefault("tool_productId", "")
        params.setdefault("tool_vendor", "")
        params.setdefault("tool_number", params.get("tool_number", "0"))
        super().__init__(description=desc, parameters=_Params(params))
        # the cutting data this tool's presets carry (mill unless the test says otherwise)
        self.preset_params = dict(preset_params if preset_params is not None else _MILL_CUTTING_DATA)
        self.presets = _Presets(owner=self)
        self.holder = None      # set when a holder JSON is assigned (build via json)

    @property
    def desc(self):
        return self.description

    def toJson(self):
        return json.dumps({"description": self.desc, "type": "x",
                           "holder": self.holder or {"description": "stock holder", "segments": []}})


class _SrcLib(_NamedCollection):
    """A source library to copy seed tools from (referenced by url+index)."""


def _refetched(tool):
    """One tool as a library re-read from its url hands it back: a DIFFERENT object carrying the same
    stored values. Every persist read-back must go through one of these - a read-back satisfied by the
    object the write just touched proves nothing. (Preset PARAMETER values are not copied; the
    re-read only ever reports preset NAMES.)"""
    clone = _Tool(tool.desc)
    clone.parameters = _Params({tool.parameters.item(i).name: tool.parameters.item(i).expression
                                for i in range(tool.parameters.count)})
    clone.preset_params = dict(tool.preset_params)
    clone.presets = _Presets(_preset_names(tool), owner=clone)
    clone.holder = tool.holder
    return clone


# A 'target' the handler drives. Models the union of document-lib + shared-lib behaviour the tool needs:
#   .tools (list), .add(tool), .remove(index), .update_tool(tool), .persist(), .operations_by_tool(tool),
#   .is_document (where_used only valid here), and the refetch read-backs off _fresh()
class _Target:
    def __init__(self, tools=(), is_document=False, ops_by_desc=None, persisted_count_value="mirror"):
        self.tools = list(tools)
        self.is_document = is_document
        self.persisted = 0
        self.updated = []
        self._ops_by_desc = ops_by_desc or {}
        # 'mirror' = the url re-read agrees with the in-memory tools (a landing persist);
        # a NUMBER simulates a persist whose url re-read disagrees (the platform lie).
        self._persisted_count_value = persisted_count_value

    def _fresh(self):
        """The library re-read from its url - FRESH clones of what it stored, never the held objects.
        A test sets `tgt._fresh = lambda: None` to simulate a library that cannot be re-read (every
        read-back then reports None and the payload must fall back to the in-memory basis)."""
        return [_refetched(t) for t in self.tools]

    def persisted_count(self):
        if self._persisted_count_value != "mirror":
            return self._persisted_count_value
        fresh = self._fresh()
        return len(fresh) if fresh is not None else None
    def reread_param(self, index, name):
        # read off the FRESH clone, so a landing edit reads back its own 'after'; a test overrides
        # this to simulate a library that stored something else.
        fresh = self._fresh()
        if fresh is None:
            return None
        p = fresh[index].parameters.itemByName(name)
        return p.expression if p is not None else None
    def reread_preset_names(self, index):
        # same fresh re-read for presets; a test overrides it to simulate a persist that the
        # library did not store.
        fresh = self._fresh()
        return _preset_names(fresh[index]) if fresh is not None else None
    def stored_tool_numbers(self):
        # the numbers the STORED library holds; a test overrides it to simulate a persist-side
        # renumber, which no in-memory read could catch.
        fresh = self._fresh()
        return None if fresh is None else [ct._read_tool_number(t) for t in fresh]
    def add(self, tool):
        self.tools.append(tool)
    def remove(self, index):
        del self.tools[index]
    def update_tool(self, tool):
        self.updated.append(tool); return True
    def persist(self):
        self.persisted += 1
    def operations_by_tool(self, tool):
        return list(self._ops_by_desc.get(tool.desc, []))


_SRC_URL = "systemlibraryroot://Samples/Milling Tools (Metric)"


def _install(monkeypatch, target=None, src=None):
    if target is None:
        target = _Target(tools=[_Tool("12mm Flat", tool_numberOfFlutes="3"),
                                _Tool("6mm Ball", tool_numberOfFlutes="2")])
    src = src if src is not None else _SrcLib([_Tool("A"), _Tool("B"), _Tool("C")])
    monkeypatch.setattr(ct, "_resolve_target", lambda scope, library: (target, None))
    monkeypatch.setattr(ct, "_source_tool", lambda url, idx: (src.item(idx), None) if 0 <= idx < src.count
                        else (None, "tool_index %d out of range" % idx))
    # creation seams (rich add): build via JSON -> a fresh _Tool carrying description + holder
    def _from_json(js):
        d = json.loads(js)
        t = _Tool(d.get("description", "built"))
        t.holder = d.get("holder")
        return t
    monkeypatch.setattr(ct, "_tool_from_json", _from_json)
    monkeypatch.setattr(ct, "_sample_for_type", lambda ty: (_Tool("sample-" + ty, tool_type=ty), None)
                        if ty in ("drill", "ball end mill", "flat end mill")
                        else (None, "no sample of type '%s'" % ty))
    monkeypatch.setattr(ct, "_holder_json", lambda ref: ({"description": "CT40 Holder", "segments": [1, 2]}, None)
                        if isinstance(ref, dict) and ref.get("index") is not None
                        else (None, "bad holder ref"))
    return target


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_unknown_action(self, monkeypatch):
        _install(monkeypatch)
        res = ct.handler(action="explode", scope="document")
        assert res["isError"] is True and "action" in res["message"].lower()

    def test_unknown_scope(self, monkeypatch):
        _install(monkeypatch)
        res = ct.handler(action="list", scope="moon")
        assert res["isError"] is True and "scope" in res["message"].lower()

    def test_target_not_found(self, monkeypatch):
        monkeypatch.setattr(ct, "_resolve_target", lambda scope, library: (None, "no library 'X'"))
        res = ct.handler(action="list", scope="local", library="X")
        assert res["isError"] is True and "library" in res["message"].lower()

    def test_where_used_requires_document(self, monkeypatch):
        _install(monkeypatch, _Target(tools=[_Tool("T")], is_document=False))
        res = ct.handler(action="where_used", scope="local", tool=0)
        assert res["isError"] is True and "document" in res["message"].lower()


# ── list ─────────────────────────────────────────────────────────────────────

class TestList:
    def test_lists_tools(self, monkeypatch):
        _install(monkeypatch)
        out = _payload(ct.handler(action="list", scope="document"))
        assert out["tool_count"] == 2
        assert out["tools"][0]["description"] == "12mm Flat" and out["tools"][0]["index"] == 0

    def test_list_shows_tool_product_id_vendor_and_number(self, monkeypatch):
        # the CUTTING tool's own product identity + its tool number ride the list row so a read can
        # confirm what the add path set (no list read could confirm them before).
        _install(monkeypatch, _Target(tools=[_Tool("Drill", tool_productId="HAM-123",
                                                    tool_vendor="Hoffmann", tool_number="7")]))
        out = _payload(ct.handler(action="list", scope="document"))
        row = out["tools"][0]
        assert row["tool_product_id"] == "HAM-123" and row["tool_vendor"] == "Hoffmann"
        assert row["number"] == 7

    def test_list_omits_empty_product_identity(self, monkeypatch):
        # empty product_id/vendor are dropped (present-only), not shown as blank fields.
        _install(monkeypatch, _Target(tools=[_Tool("Plain")]))
        out = _payload(ct.handler(action="list", scope="document"))
        row = out["tools"][0]
        assert "tool_product_id" not in row and "tool_vendor" not in row

    def test_list_filters_by_tool_type(self, monkeypatch):
        _install(monkeypatch, _Target(tools=[_Tool("Flat", tool_type="flat end mill"),
                                _Tool("Ball", tool_type="ball end mill")]))
        out = _payload(ct.handler(action="list", scope="document", tool_type="ball"))
        assert out["tool_count"] == 1 and out["tools"][0]["type"] == "ball end mill"

    def test_list_libraries_when_no_library_given(self, monkeypatch):
        # list, shared scope, no 'library' -> the libraries at that scope
        _install(monkeypatch)
        monkeypatch.setattr(ct, "_shared_libraries",
                            lambda scope: ([{"name": "Milling Tools (Metric)", "url": "u1"},
                                           {"name": "Team Mill.hub", "url": "u2"}], False, None))
        out = _payload(ct.handler(action="list", scope="hub"))
        assert out["library_count"] == 2
        assert "truncated" not in out
        assert "Team Mill.hub" in [l["name"] for l in out["libraries"]]


# ── list_types (the from_type vocabulary, no document/scope/library needed) ─────────────────────────

class TestListTypes:
    def test_lists_the_type_map_no_target_resolution(self, monkeypatch):
        # No _resolve_target seam installed at all - list_types must not need one.
        monkeypatch.setattr(ct, "_build_type_map", lambda: {"drill": ("u", 0), "ball end mill": ("u", 1)})
        out = _payload(ct.handler(action="list_types"))
        assert out["type_count"] == 2
        assert out["types"] == ["ball end mill", "drill"]      # sorted

    def test_empty_type_map_errors(self, monkeypatch):
        monkeypatch.setattr(ct, "_build_type_map", lambda: {})
        res = ct.handler(action="list_types")
        assert res["isError"] is True
        assert "sample tool libraries" in res["message"]


# ── add (multiple by reference) ─────────────────────────────────────────────

class TestAdd:
    def test_add_multiple(self, monkeypatch):
        tgt = _install(monkeypatch)
        out = _payload(ct.handler(action="add", scope="cloud", library="MyLib",
                                  add_tools=[{"library_url": _SRC_URL, "index": 0},
                                             {"library_url": _SRC_URL, "index": 2}]))
        assert len(tgt.tools) == 4 and out["added"] == 2
        assert tgt.persisted == 1          # shared scope persists once after the batch

    def test_persist_whose_url_reread_disagrees_bites(self, monkeypatch):
        # updateToolLibrary reports success but the library re-read fresh from its url holds the
        # wrong tool count (updateToolLibrary returning True is NOT proof) -> error, not ok
        tgt = _Target(tools=[_Tool("12mm Flat")], persisted_count_value=1)
        _install(monkeypatch, target=tgt)
        res = ct.handler(action="add", scope="cloud", library="MyLib",
                         add_tools=[{"library_url": _SRC_URL, "index": 0}])
        assert res["isError"] is True
        assert "did not land" in res["message"]

    def test_add_validates_all_refs_before_adding(self, monkeypatch):
        tgt = _install(monkeypatch)
        res = ct.handler(action="add", scope="cloud", library="MyLib",
                         add_tools=[{"library_url": _SRC_URL, "index": 0},
                                    {"library_url": _SRC_URL, "index": 99}])
        assert res["isError"] is True and "99" in res["message"]
        assert len(tgt.tools) == 2 and tgt.persisted == 0   # nothing added/persisted on a bad ref

    def test_add_requires_refs(self, monkeypatch):
        _install(monkeypatch)
        res = ct.handler(action="add", scope="cloud", library="MyLib")
        assert res["isError"] is True
        assert "Provide 'add_tools'" in res["message"]


# ── rich add: create-by-type + holder + presets (the demo, via tool calls) ──

class TestAddRich:
    def test_create_from_type(self, monkeypatch):
        tgt = _install(monkeypatch)
        out = _payload(ct.handler(action="add", scope="cloud", library="L",
                                  add_tools=[{"from_type": "drill"},
                                             {"from_type": "ball end mill"}]))
        assert out["added"] == 2 and len(tgt.tools) == 4
        # the built tools carry the sample's description (from the cloned JSON)
        descs = [t.desc for t in tgt.tools[-2:]]
        assert "sample-drill" in descs and "sample-ball end mill" in descs

    def test_create_with_description_override_and_holder(self, monkeypatch):
        tgt = _install(monkeypatch)
        _payload(ct.handler(action="add", scope="cloud", library="L",
                            add_tools=[{"from_type": "drill", "description": "MCP Demo - drill",
                                        "holder": {"library_url": "h", "index": 0}}]))
        built = tgt.tools[-1]
        assert built.desc == "MCP Demo - drill"
        assert built.holder == {"description": "CT40 Holder", "segments": [1, 2]}

    def test_an_empty_holder_ref_is_refused_not_silently_dropped(self, monkeypatch):
        # PRESENCE gates the holder resolve, not truthiness: {} is a malformed ref, and dropping
        # it shipped the sample's holder as a success (measured).
        tgt = _install(monkeypatch)
        before = len(tgt.tools)
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill", "holder": {}}])
        assert res["isError"] is True and "bad holder ref" in res["message"]
        assert len(tgt.tools) == before             # nothing was added on the refusal

    def test_create_with_presets(self, monkeypatch):
        tgt = _install(monkeypatch)
        _payload(ct.handler(action="add", scope="cloud", library="L",
                            add_tools=[{"from_type": "drill",
                                        "presets": [{"spindle_speed": 10000, "feed": 500},
                                                    {"spindle_speed": 6000}]}]))
        built = tgt.tools[-1]
        assert built.presets.count == 2
        assert built.presets.item(0).parameters.itemByName("tool_spindleSpeed").expression == "10000"
        assert built.presets.item(0).parameters.itemByName("tool_feedCutting").expression == "500"

    def test_preset_add_failure_errors_not_silent_skip(self, monkeypatch):
        # A preset that cannot be created must abort the add - skipping it would report "added"
        # while silently dropping the requested preset.
        tgt = _install(monkeypatch)

        class _NoPresets(_Presets):
            def add(self):
                raise RuntimeError("presets locked")

        def _from_json(js):
            t = _Tool(json.loads(js).get("description", "built"))
            t.presets = _NoPresets()
            return t

        monkeypatch.setattr(ct, "_tool_from_json", _from_json)
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill", "presets": [{"spindle_speed": 10000}]}])
        assert res["isError"] is True and "preset" in res["message"].lower()
        assert len(tgt.tools) == 2          # nothing was added

    def test_missing_diameter_param_errors_not_silent_drop(self, monkeypatch):
        # A tool without a tool_diameter parameter cannot take the requested override - that is
        # an error, not a tool silently added at its default diameter.
        tgt = _install(monkeypatch)

        def _from_json(js):
            t = _Tool(json.loads(js).get("description", "built"))
            t.parameters = _Params({})      # no tool_diameter parameter
            return t

        monkeypatch.setattr(ct, "_tool_from_json", _from_json)
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill", "diameter": 8}])
        assert res["isError"] is True and "tool_diameter" in res["message"]
        assert len(tgt.tools) == 2          # nothing was added

    def test_diameter_setter_raise_propagates(self, monkeypatch):
        # A diameter-override setter failure must propagate out of the handler - swallowing it
        # would add the tool while silently dropping the requested override.
        import pytest

        class _LockedParam(_Param):
            """A parameter whose expression setter REFUSES the write outright."""

            @FakeCAMParameter.expression.setter
            def expression(self, v):
                raise AttributeError("expression is locked")

        class _LockedParams(FakeCAMParameters):
            """The diameter reads as a locked parameter; every other name reads normally."""

            def itemByName(self, name):
                p = super().itemByName(name)
                if p is not None and name == "tool_diameter":
                    return _LockedParam(p.name, p.expression)
                return p

        class _LockedTool(_Tool):
            def __init__(self, desc, **params):
                super().__init__(desc, **params)
                self.parameters = _LockedParams(
                    [_Param("tool_diameter", "1.0"), _Param("tool_description", desc)])

        _install(monkeypatch)
        monkeypatch.setattr(ct, "_tool_from_json", lambda js: _LockedTool("sample-drill"))
        with pytest.raises(AttributeError, match="expression is locked"):
            ct.handler(action="add", scope="cloud", library="L",
                       add_tools=[{"from_type": "drill", "diameter": "6 mm"}])

    def test_unknown_from_type_errors_before_adding(self, monkeypatch):
        tgt = _install(monkeypatch)
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill"}, {"from_type": "banana mill"}])
        assert res["isError"] is True and "banana mill" in res["message"]
        assert len(tgt.tools) == 2 and tgt.persisted == 0   # validate-all-before-add

    def test_entry_needs_type_or_ref(self, monkeypatch):
        _install(monkeypatch)
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"description": "no source"}])
        assert res["isError"] is True
        assert "needs 'from_type'" in res["message"]


class TestExpressionQuoting:
    """product_id/vendor are written as expressions through the shared CAM codec. The ONE difference
    is a backslash: what the tool-parameter store spells it as is unmeasured, so the doubling stays."""

    def test_a_value_holding_no_backslash_is_the_shared_codec(self):
        for text in ("HAM-123", "Hoffmann Group", "O'Brien", "", "12"):
            assert ct._quote(text) == cc.quote_expression(text)

    def test_a_backslash_is_doubled_where_the_shared_codec_passes_it_through(self):
        assert cc.quote_expression("A\\B") == "'A\\B'"
        assert ct._quote("A\\B") == "'A\\\\B'"


# ── add: product_id / vendor (tool_productId/tool_vendor, applied as expressions AFTER creation - ──
# ── createFromJson's JSON schema silently drops these keys, verified live) ──────────────────────────

class TestAddProductVendor:
    def test_product_id_and_vendor_applied_and_read_back(self, monkeypatch):
        tgt = _install(monkeypatch)
        out = _payload(ct.handler(action="add", scope="cloud", library="L",
                                  add_tools=[{"from_type": "drill", "product_id": "HAM-123",
                                              "vendor": "Hoffmann Group"}]))
        built = tgt.tools[-1]
        assert built.parameters.itemByName("tool_productId").value.value == "HAM-123"
        assert built.parameters.itemByName("tool_vendor").value.value == "Hoffmann Group"
        assert out["added"] == 1

    def test_missing_product_id_param_errors_not_silent_drop(self, monkeypatch):
        # A tool without a tool_productId parameter cannot take the requested override.
        tgt = _install(monkeypatch)

        def _from_json(js):
            t = _Tool(json.loads(js).get("description", "built"))
            t.parameters = _Params({"tool_description": t.desc, "tool_diameter": "1.0"})
            return t

        monkeypatch.setattr(ct, "_tool_from_json", _from_json)
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill", "product_id": "HAM-123"}])
        assert res["isError"] is True and "tool_productId" in res["message"]
        assert len(tgt.tools) == 2          # nothing was added

    def test_product_id_readback_mismatch_errors(self, monkeypatch):
        # The expression sets without raising, but the parameter's evaluated .value never moves - the
        # THIRD failure mode (looks identical to success from the setter alone) that the original
        # add_tools product_id/vendor defect actually was; the read-back must catch it.
        tgt = _install(monkeypatch)

        class _StaleParam(_Param):
            @property
            def expression(self):
                return self._expression
            @expression.setter
            def expression(self, v):
                self._expression = v          # accepted - but .value deliberately stays put

        def _from_json(js):
            t = _Tool(json.loads(js).get("description", "built"))
            _replace(t.parameters, _StaleParam("tool_productId", ""))
            return t

        monkeypatch.setattr(ct, "_tool_from_json", _from_json)
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill", "product_id": "HAM-123"}])
        assert res["isError"] is True and "did not land" in res["message"]
        assert len(tgt.tools) == 2          # nothing was added


# ── add: auto-assign a FREE tool_number so multiple adds don't collide (cam_post refuses dupes) ──

class TestAddToolNumbers:
    def test_assigns_next_free_numbers_from_zero(self, monkeypatch):
        # the two existing tools both sit at tool_number 0 (unassigned); the two new tools must get
        # the next FREE numbers (1, 2), never keep the cloned sample's number.
        tgt = _install(monkeypatch)
        out = _payload(ct.handler(action="add", scope="cloud", library="L",
                                  add_tools=[{"from_type": "drill"}, {"from_type": "ball end mill"}]))
        assert out["assigned_tool_numbers"] == [1, 2]
        added = tgt.tools[-2:]
        assert [ct._read_tool_number(t) for t in added] == [1, 2]

    def test_skips_numbers_already_in_use(self, monkeypatch):
        # existing tools already hold 1 and 3 - a new tool gets 2 (the first free), not a collision.
        tgt = _install(monkeypatch, _Target(tools=[_Tool("A", tool_number="1"),
                                                   _Tool("B", tool_number="3")]))
        out = _payload(ct.handler(action="add", scope="cloud", library="L",
                                  add_tools=[{"from_type": "drill"}]))
        assert out["assigned_tool_numbers"] == [2]
        assert ct._read_tool_number(tgt.tools[-1]) == 2

    def test_the_library_refetch_is_a_different_object(self):
        # The fake's own contract: a read-back reads a FRESH clone, so no production read-back can
        # pass by handing back the very object it just wrote.
        tgt = _Target(tools=[_Tool("EM", tool_number="5")])
        fresh = tgt._fresh()
        assert fresh[0] is not tgt.tools[0]
        assert ct._read_tool_number(fresh[0]) == 5

    def test_numbers_are_confirmed_against_the_fresh_library(self, monkeypatch):
        _install(monkeypatch)
        out = _payload(ct.handler(action="add", scope="cloud", library="L",
                                  add_tools=[{"from_type": "drill"}]))
        assert out["verified_in_memory_only"] is False
        assert "persisted" in out["note"] and "library url" in out["note"]

    def test_document_scope_add_never_claims_the_stored_library(self, monkeypatch):
        # The document library has NO url to re-read, and doc_save is what stores the document. So a
        # document add proves the numbers are present, never that they were stored - the flag stays
        # weak and the note says so.
        tgt = _install(monkeypatch, _Target(tools=[_Tool("EM")], is_document=True))
        out = _payload(ct.handler(action="add", scope="document",
                                  add_tools=[{"from_type": "drill"}]))
        assert out["assigned_tool_numbers"] == [1] and len(tgt.tools) == 2
        assert out["verified_in_memory_only"] is True
        # the add skips the document read-back, so the note must claim NO read-back - only the
        # in-memory tools - and point at what does the storing
        assert "in-memory" in out["note"] and "doc_save" in out["note"]
        assert "persist" not in out["note"].lower() and "url" not in out["note"]

    def test_document_scope_add_does_not_reach_for_a_stored_library(self, monkeypatch):
        # ... and it does not even ask: the stored-number gate is shared-target-only, so a document
        # add makes no refetch-for-storage call it would then have to explain.
        tgt = _install(monkeypatch, _Target(tools=[_Tool("EM")], is_document=True))

        def _boom():
            raise AssertionError("a document target must not read a stored library")
        tgt.stored_tool_numbers = _boom
        out = _payload(ct.handler(action="add", scope="document",
                                  add_tools=[{"from_type": "drill"}]))
        assert out["added"] == 1

    def test_persist_side_renumber_bites(self, monkeypatch):
        # The in-memory tools hold the assigned numbers, and the tool COUNT is right - only the
        # library re-read from its url shows the stored number is a different one.
        tgt = _install(monkeypatch)
        tgt.stored_tool_numbers = lambda: [0, 0, 99]     # the new tool was stored as 99, not 1
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill"}])
        assert res["isError"] is True
        assert "[1]" in res["message"] and "99" in res["message"]
        assert "did not reach the stored library" in res["message"]

    def test_unreadable_refetch_names_the_weaker_basis(self, monkeypatch):
        # The library cannot be re-read: the in-memory verdict stands, but the payload must SAY the
        # numbers were only confirmed there - never claim the stored library agreed.
        tgt = _install(monkeypatch)
        tgt._fresh = lambda: None
        out = _payload(ct.handler(action="add", scope="cloud", library="L",
                                  add_tools=[{"from_type": "drill"}]))
        assert out["assigned_tool_numbers"] == [1]
        assert out["verified_in_memory_only"] is True
        assert "in-memory" in out["note"]

    def test_missing_tool_number_param_errors_not_silent(self, monkeypatch):
        # a tool with no tool_number parameter can't take a free number - error, add nothing.
        tgt = _install(monkeypatch)

        def _from_json(js):
            t = _Tool(json.loads(js).get("description", "built"))
            _drop(t.parameters, "tool_number")
            return t

        monkeypatch.setattr(ct, "_tool_from_json", _from_json)
        res = ct.handler(action="add", scope="cloud", library="L", add_tools=[{"from_type": "drill"}])
        assert res["isError"] is True and "tool_number" in res["message"]
        assert len(tgt.tools) == 2          # nothing was added


# ── remove (multiple) ────────────────────────────────────────────────────────

class TestRemove:
    def test_remove_multiple_high_to_low(self, monkeypatch):
        # removing indices 0 and 2 must delete the RIGHT tools (remove high-to-low so indices stay valid)
        tgt = _install(monkeypatch, _Target(tools=[_Tool("zero"), _Tool("one"), _Tool("two")]))
        out = _payload(ct.handler(action="remove", scope="local", library="L", remove_indices=[0, 2]))
        remaining = [t.desc for t in tgt.tools]
        assert remaining == ["one"] and out["removed"] == 2
        assert tgt.persisted == 1

    def test_remove_out_of_range(self, monkeypatch):
        tgt = _install(monkeypatch, _Target(tools=[_Tool("only")]))
        res = ct.handler(action="remove", scope="local", library="L", remove_indices=[5])
        assert res["isError"] is True and "range" in res["message"].lower()
        assert len(tgt.tools) == 1 and tgt.persisted == 0


# ── edit tool data ───────────────────────────────────────────────────────────

class TestEdit:
    def test_edit_parameters_and_persist_document(self, monkeypatch):
        tgt = _install(monkeypatch, _Target(tools=[_Tool("EM", tool_numberOfFlutes="3")], is_document=True))
        out = _payload(ct.handler(action="edit", scope="document", tool=0,
                                  parameters={"tool_numberOfFlutes": "4"}))
        assert tgt.tools[0].parameters.itemByName("tool_numberOfFlutes").expression == "4"
        assert out["edited"] == 1
        # document scope persists via update_tool (not the shared persist())
        assert tgt.updated and tgt.persisted == 0

    def test_edit_unknown_parameter_before_applying(self, monkeypatch):
        tgt = _install(monkeypatch, _Target(tools=[_Tool("EM", tool_numberOfFlutes="3")], is_document=True))
        res = ct.handler(action="edit", scope="document", tool=0,
                         parameters={"tool_numberOfFlutes": "4", "ghost": "9"})
        assert res["isError"] is True and "ghost" in res["message"]
        # the valid one was NOT applied (validate all first)
        assert tgt.tools[0].parameters.itemByName("tool_numberOfFlutes").expression == "3"

    def test_warns_when_overwriting_a_formula_derived_parameter(self, monkeypatch):
        # tool_shoulderLength's expression is the literal string "tool_fluteLength" - it tracks that
        # OTHER parameter rather than holding an independent literal (verified live). The edit still
        # goes through (the warning is the teaching, not a refusal).
        tgt = _install(monkeypatch, _Target(tools=[_Tool("EM", tool_shoulderLength="tool_fluteLength",
                                                          tool_fluteLength="16")], is_document=True))
        out = _payload(ct.handler(action="edit", scope="document", tool=0,
                                  parameters={"tool_shoulderLength": "21 mm"}))
        assert out["warnings"] and "tool_fluteLength" in out["warnings"][0]
        assert tgt.tools[0].parameters.itemByName("tool_shoulderLength").expression == "21 mm"

    def test_no_warning_when_overwriting_a_literal_parameter(self, monkeypatch):
        tgt = _install(monkeypatch, _Target(tools=[_Tool("EM", tool_numberOfFlutes="3")], is_document=True))
        out = _payload(ct.handler(action="edit", scope="document", tool=0,
                                  parameters={"tool_numberOfFlutes": "4"}))
        assert "warnings" not in out

    def test_setter_raise_mid_edit_names_property_and_applied(self, monkeypatch):
        # the SECOND property's setter raises -> isError naming the FAILING property AND what already
        # landed (partial success surfaced explicitly), and the partial edit is never persisted.
        class _Boom(_Param):
            @property
            def expression(self):
                return self._expression
            @expression.setter
            def expression(self, v):
                raise RuntimeError("locked by Fusion")

        tool = _Tool("EM", tool_numberOfFlutes="3")
        _replace(tool.parameters, _Boom("tool_coolant", "flood"))
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="edit", scope="document", tool=0,
                         parameters={"tool_numberOfFlutes": "4", "tool_coolant": "mist"})
        assert res["isError"] is True
        assert "tool_coolant" in res["message"] and "locked by Fusion" in res["message"]
        assert "tool_numberOfFlutes" in res["message"]        # what DID land is named, not hidden
        assert tgt.updated == [] and tgt.persisted == 0       # the partial edit was not persisted

    def test_setter_raise_with_nothing_applied_says_none(self, monkeypatch):
        class _Boom(_Param):
            @property
            def expression(self):
                return self._expression
            @expression.setter
            def expression(self, v):
                raise RuntimeError("locked")

        tool = _Tool("EM")
        _replace(tool.parameters, _Boom("tool_coolant", "flood"))
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="edit", scope="document", tool=0,
                         parameters={"tool_coolant": "mist"})
        assert res["isError"] is True and "tool_coolant" in res["message"]
        assert "none" in res["message"]                       # the Applied: list is honest about zero
        assert tgt.updated == [] and tgt.persisted == 0

    def test_edit_reports_before_and_read_back_after(self, monkeypatch):
        # 'after' is read BACK off the parameter (the platform can normalize what it stores) -
        # never an echo of the requested expression.
        class _Norm(_Param):
            @property
            def expression(self):
                return self._expression
            @expression.setter
            def expression(self, v):
                self._expression = "4.000 mm"      # platform normalizes the stored expression

        tool = _Tool("EM")
        _replace(tool.parameters, _Norm("tool_diameter", "1.0"))
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        out = _payload(ct.handler(action="edit", scope="document", tool=0,
                                  parameters={"tool_diameter": "4 mm"}))
        assert out["changed"][0] == {"name": "tool_diameter", "before": "1.0", "after": "4.000 mm"}

    def test_persist_readback_mismatch_bites(self, monkeypatch):
        # updateTool/updateToolLibrary returning is not proof the edit stored - the tool re-read from
        # the library holds a DIFFERENT value than what we set -> error, never a false 'edited'.
        tgt = _install(monkeypatch, _Target(tools=[_Tool("EM", tool_numberOfFlutes="3")],
                                            is_document=True))
        tgt.reread_param = lambda index, name: "3"      # library reports a value the edit did not store
        res = ct.handler(action="edit", scope="document", tool=0,
                         parameters={"tool_numberOfFlutes": "4"})
        assert res["isError"] is True and "did not persist" in res["message"]

    def test_document_scope_edit_never_claims_storage_though_the_refetch_reads(self, monkeypatch):
        # The document refetch READS fine and the edit is confirmed present - but the document
        # library has no url, so nothing here proves storage. The flag must stay weak on the
        # readable path too, and the note must name the document rung.
        _install(monkeypatch, _Target(tools=[_Tool("EM", tool_numberOfFlutes="3")],
                                      is_document=True))
        out = _payload(ct.handler(action="edit", scope="document", tool=0,
                                  parameters={"tool_numberOfFlutes": "4"}))
        assert out["edited"] == 1                       # the effect still reports
        assert out["verified_in_memory_only"] is True
        assert "present, not that it was stored" in out["note"] and "doc_save" in out["note"]
        assert "persist" not in out["note"].lower()

    def test_a_document_tool_edit_says_existing_operations_keep_their_copies(self, monkeypatch):
        # MEASURED: a document tool edited 25 -> 40 mm flute reaches an op created AFTER the edit,
        # while an op created before keeps the copy it was made with. Without this clause the
        # caller reads 'Tool edited' and expects its existing operations to cut at the new geometry.
        _install(monkeypatch, _Target(tools=[_Tool("EM", tool_numberOfFlutes="3")],
                                      is_document=True))
        out = _payload(ct.handler(action="edit", scope="document", tool=0,
                                  parameters={"tool_numberOfFlutes": "4"}))
        assert "keep their own copy" in out["note"]
        assert "cam_edit_operation(tool_scope, tool_index)" in out["note"]
        assert len(out["note"]) <= 400, len(out["note"])   # test_prose_budget.NOTE_BUDGET_CHARS

    def test_edit_confirmed_against_the_fresh_library_says_so(self, monkeypatch):
        _install(monkeypatch, _Target(tools=[_Tool("EM", tool_numberOfFlutes="3")]))
        out = _payload(ct.handler(action="edit", scope="cloud", library="L", tool=0,
                                  parameters={"tool_numberOfFlutes": "4"}))
        assert out["verified_in_memory_only"] is False
        assert "persisted" in out["note"] and "library url" in out["note"]
        # a SHARED library holds no operations, so the operation-copy clause does not ride there
        assert "keep their own copy" not in out["note"]

    def test_an_expression_that_does_not_evaluate_errors_and_rolls_back(self, monkeypatch):
        # A tool parameter STORES an unresolvable expression verbatim and reads it back, so the
        # existing echo check cannot see it - only .error can (measured live: tool_diameter set to
        # 'NoSuchParamXyz * 2' returned edited:1 while Fusion held 0.0 with a parameter error).
        tool = _Tool("EM")
        _replace(tool.parameters, _BrokenParam("tool_diameter", "8."))
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="edit", scope="document", tool=0,
                         parameters={"tool_diameter": "NoSuchParamXyz * 2"})
        assert res["isError"] is True
        assert "did not evaluate" in res["message"] and "NoSuchParamXyz * 2" in res["message"]
        assert "Failed to evaluate expression." in res["message"]
        assert tool.parameters.itemByName("tool_diameter").expression == "8."   # rolled back
        assert tgt.updated == [] and tgt.persisted == 0                         # never committed

    def test_one_bad_expression_rolls_the_GOOD_ones_back_too(self, monkeypatch):
        # partial application is the trap: the good parameter landed before the bad one was judged,
        # so the refusal has to restore every parameter this call touched.
        tool = _Tool("EM", tool_numberOfFlutes="3")
        _replace(tool.parameters, _BrokenParam("tool_diameter", "8."))
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="edit", scope="document", tool=0,
                         parameters={"tool_numberOfFlutes": "4", "tool_diameter": "Nope * 2"})
        assert res["isError"] is True and "Rolled back all 2 parameter(s)" in res["message"]
        assert tool.parameters.itemByName("tool_numberOfFlutes").expression == "3"
        assert tool.parameters.itemByName("tool_diameter").expression == "8."

    def test_a_rollback_that_will_not_read_back_is_named_not_assumed(self, monkeypatch):
        class _Sticky(_BrokenParam):
            @property
            def expression(self):
                return self._expression
            @expression.setter
            def expression(self, v):
                self._expression = "NoSuchParamXyz * 2"      # refuses to go back to its prior expression

        tool = _Tool("EM")
        _replace(tool.parameters, _Sticky("tool_diameter", "8."))
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="edit", scope="document", tool=0,
                         parameters={"tool_diameter": "NoSuchParamXyz * 2"})
        assert res["isError"] is True
        assert "ROLLBACK INCOMPLETE" in res["message"] and "tool_diameter" in res["message"]

    def test_a_parameter_warning_alone_never_blocks_the_edit(self, monkeypatch):
        # .warning fires on VALID expressions too, so it is carried on the row, never a refusal.
        class _Warned(_Param):
            """A parameter whose .warning fires on a valid expression."""

            def __init__(self, name, expr):
                super().__init__(name, expr)
                self.warning = "stock is less than the model width"

        tool = _Tool("EM")
        _replace(tool.parameters, _Warned("tool_diameter", "8."))
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        out = _payload(ct.handler(action="edit", scope="document", tool=0,
                                  parameters={"tool_diameter": "10 mm"}))
        assert out["edited"] == 1
        assert out["changed"][0]["warning"] == "stock is less than the model width"

    def test_edit_with_an_unreadable_refetch_names_the_weaker_basis(self, monkeypatch):
        # A library that cannot be re-read proves nothing about storage - the edit still stands on
        # the in-memory tool, and the payload says exactly that instead of claiming persistence.
        tgt = _install(monkeypatch, _Target(tools=[_Tool("EM", tool_numberOfFlutes="3")]))
        tgt._fresh = lambda: None
        out = _payload(ct.handler(action="edit", scope="cloud", library="L", tool=0,
                                  parameters={"tool_numberOfFlutes": "4"}))
        assert out["edited"] == 1
        assert out["verified_in_memory_only"] is True
        assert "persisted" not in out["note"] and "in-memory" in out["note"]


# ── where_used (document scope) ─────────────────────────────────────────────

class TestWhereUsed:
    def test_where_used_lists_operations(self, monkeypatch):
        tgt = _Target(tools=[_Tool("EM")], is_document=True,
                      ops_by_desc={"EM": ["Face1", "Adaptive1"]})
        _install(monkeypatch, tgt)
        out = _payload(ct.handler(action="where_used", scope="document", tool=0))
        assert out["operations"] == ["Face1", "Adaptive1"] and out["operation_count"] == 2


# ── parameters (the FULL per-tool parameter read the list summary points to) ──
# NEEDS-LIVE-VERIFY: against a real document with a CAM product and >=1 tool in the document library,
#   cam_edit_tools(action='parameters', scope='document', tool=0)
# expected shape: {"tool":0, "description":<str>, "parameter_count":>0, "parameters":[
#   {"name":"tool_diameter","expression":<str>,"value":<number>}, ...,
#   {"name":"tool_shoulderLength","expression":"tool_fluteLength","value":<number>,
#    "formula_source":"tool_fluteLength"}, ...]}
# Confirms live: adsk.cam.Tool.parameters exposes .count/.item(i), each parameter exposes
# .name/.expression/.value.value, and a formula-derived parameter (shoulderLength tracking
# fluteLength) reads its source name back so _formula_source flags it.

class TestParameters:
    def test_lists_every_parameter_with_expression_and_value(self, monkeypatch):
        _install(monkeypatch, _Target(tools=[_Tool("EM", tool_numberOfFlutes="3",
                                                    tool_diameter="6 mm")]))
        out = _payload(ct.handler(action="parameters", scope="cloud", library="L", tool=0))
        assert out["parameter_count"] == len(out["parameters"])
        rows = {r["name"]: r for r in out["parameters"]}
        assert rows["tool_diameter"]["expression"] == "6 mm"
        assert rows["tool_numberOfFlutes"]["expression"] == "3"
        # every row carries name/expression/value
        assert all({"name", "expression", "value"} <= set(r) for r in out["parameters"])

    def test_flags_formula_derived_parameter(self, monkeypatch):
        # tool_shoulderLength's expression is literally another parameter's NAME - it tracks that
        # parameter rather than holding a literal, so the read flags it; a literal carries no flag.
        _install(monkeypatch, _Target(tools=[_Tool("EM", tool_shoulderLength="tool_fluteLength",
                                                    tool_fluteLength="16")]))
        out = _payload(ct.handler(action="parameters", scope="cloud", library="L", tool=0))
        rows = {r["name"]: r for r in out["parameters"]}
        assert rows["tool_shoulderLength"]["formula_source"] == "tool_fluteLength"
        assert "formula_source" not in rows["tool_fluteLength"]

    def test_value_is_read_back_off_the_parameter(self, monkeypatch):
        _install(monkeypatch, _Target(tools=[_Tool("EM", tool_numberOfFlutes="4")]))
        out = _payload(ct.handler(action="parameters", scope="cloud", library="L", tool=0))
        row = next(r for r in out["parameters"] if r["name"] == "tool_numberOfFlutes")
        assert row["value"] == "4"

    def test_bad_tool_index_errors(self, monkeypatch):
        _install(monkeypatch, _Target(tools=[_Tool("only")]))
        res = ct.handler(action="parameters", scope="cloud", library="L", tool=7)
        assert res["isError"] is True and "0..0" in res["message"]

    def test_works_on_document_scope(self, monkeypatch):
        _install(monkeypatch, _Target(tools=[_Tool("EM")], is_document=True))
        out = _payload(ct.handler(action="parameters", scope="document", tool=0))
        assert out["tool"] == 0 and out["parameter_count"] >= 1


# ── the REAL _Target's tool list: an index IS the address ───────────────────

class TestTargetToolIndexAlignment:
    """Every action addresses a tool by its INDEX in the library, so the real _Target.tools must be
    a positional walk. A walk that skips an unreadable tool slides every later tool onto the wrong
    index (the caller edits a tool it never named) AND shortens the list below the library's own
    count, which the add/remove persist gate reads as a persist that did not land."""

    def _library_with_an_unreadable_tool(self):
        class _OneToolUnreadable(_SrcLib):
            def item(self, i):
                if i == 1:
                    raise RuntimeError("tool read failed")
                return self._items[i]

        return _OneToolUnreadable([_Tool("First"), _Tool("Ghost"),
                                   _Tool("Third", tool_numberOfFlutes="5")])

    def test_a_later_tool_keeps_its_index_when_an_earlier_one_cannot_be_read(self, monkeypatch):
        lib = self._library_with_an_unreadable_tool()
        target = ct._Target(lib, is_document=True)
        monkeypatch.setattr(ct, "_resolve_target", lambda scope, library: (target, None))
        out = _payload(ct.handler(action="parameters", scope="document", tool=2))
        rows = {r["name"]: r for r in out["parameters"]}
        assert out["tool"] == 2                                    # 'Third' is still index 2...
        assert rows["tool_numberOfFlutes"]["expression"] == "5"    # ...and it IS 'Third'
        assert target.tools[1] is None                             # the unreadable one holds its slot

    def test_the_tool_list_stays_as_long_as_the_library_count(self, monkeypatch):
        # the persist gate compares the re-read library's count with len(target.tools); a short
        # list turns a landed persist into "the persist did not land".
        lib = self._library_with_an_unreadable_tool()
        target = ct._Target(lib, is_document=True)
        assert len(target.tools) == lib.count == 3


class _MutableLib(_SrcLib):
    """A library a target writes through, counting the item() calls one .tools walk costs."""

    def __init__(self, tools):
        super().__init__(list(tools))
        self.walks = 0

    def item(self, i):
        self.walks += 1
        return self._items[i]

    def add(self, t):
        self._items.append(t)

    def remove(self, i):
        del self._items[i]

    def replace(self, i, t):
        self._items[i] = t


# ── the REAL _Target's tool list: held, and dropped by anything that changes it ──

class TestTargetToolListCache:
    """One action reads .tools several times (_do_add reads it 3x, _do_remove 2x) and each read is a
    walk of the whole library - seconds on a 266-tool cloud one. So the list is held between reads,
    and every method that writes the library, commits it, or re-reads it must drop the held copy:
    a surviving list reports the library as it was and hides the very change the action just made."""

    def _target(self, descs=("A", "B"), **kw):
        lib = _MutableLib([_Tool(d) for d in descs])
        return lib, ct._Target(lib, **kw)

    def test_repeat_reads_walk_the_library_once(self):
        lib, target = self._target(is_document=True)
        assert [t.desc for t in target.tools] == ["A", "B"]
        assert [t.desc for t in target.tools] == ["A", "B"]
        assert lib.walks == 2          # two tools, ONE walk - not four

    def test_an_add_is_visible_to_the_next_read(self):
        lib, target = self._target(is_document=True)
        assert len(target.tools) == 2                      # the read that fills the held list
        target.add(_Tool("C"))
        assert [t.desc for t in target.tools] == ["A", "B", "C"]

    def test_a_remove_is_visible_to_the_next_read(self):
        lib, target = self._target(is_document=True)
        assert len(target.tools) == 2
        target.remove(0)
        assert [t.desc for t in target.tools] == ["B"]

    def test_update_tool_is_visible_to_the_next_read(self):
        # updateTool commits an edited tool into the library, which may hand the next read a
        # different object for that index - so the held list cannot survive it either.
        lib = _MutableLib([_Tool("A"), _Tool("B")])
        replacement = _Tool("A edited")
        target = ct._Target(lib, is_document=True,
                            update_tool_fn=lambda t: lib.replace(0, t))
        assert [t.desc for t in target.tools] == ["A", "B"]
        target.update_tool(replacement)
        assert target.tools[0] is replacement

    def test_a_persist_drops_the_held_list(self):
        lib = _MutableLib([_Tool("A"), _Tool("B")])
        target = ct._Target(lib, is_document=False, persist_fn=lambda: lib.add(_Tool("C")))
        assert len(target.tools) == 2
        target.persist()
        assert [t.desc for t in target.tools] == ["A", "B", "C"]

    def test_a_refetch_drops_the_held_list(self):
        # refetch is the read-back seam every persist gate goes through; the library it re-reads is
        # the same one .tools walks, so the held copy is stale from that point on.
        lib = _MutableLib([_Tool("A"), _Tool("B")])
        target = ct._Target(lib, is_document=True, refetch_fn=lambda: lib)
        assert len(target.tools) == 2
        lib.add(_Tool("C"))
        assert target.refetch() is lib
        assert [t.desc for t in target.tools] == ["A", "B", "C"]

    def test_the_persist_gate_reads_the_library_after_the_add(self, monkeypatch):
        # the whole point, end to end: _do_add compares the re-read count against len(target.tools),
        # so a held list would make a landed 3-tool persist read as "2, not 3".
        lib = _MutableLib([_Tool("A"), _Tool("B")])
        target = ct._Target(lib, is_document=False, persist_fn=lambda: None,
                            refetch_fn=lambda: lib)
        monkeypatch.setattr(ct, "_resolve_target", lambda scope, library: (target, None))
        monkeypatch.setattr(ct, "_sample_for_type", lambda ty: (_Tool("sample-" + ty, tool_type=ty), None))
        monkeypatch.setattr(ct, "_tool_from_json", lambda js: _Tool(json.loads(js).get("description", "built")))
        out = _payload(ct.handler(action="add", scope="cloud", library="L",
                                  add_tools=[{"from_type": "drill"}]))
        assert out["added"] == 1 and out["tool_count"] == 3
        assert len(target.tools) == lib.count == 3


# ── create_library (folded from cam_create_tool_library) ────────────────────

class _NewLib:
    def __init__(self):
        self.tools = []
    def add(self, t):
        self.tools.append(t)
    @property
    def count(self):
        return len(self.tools)


class _CreateLibs:
    """Stand-in for ToolLibraries' create path."""
    def __init__(self, loads_back=True):
        self.imported = []
        self._loads_back = loads_back     # False = the created url re-reads to nothing (the lie)
    def urlByLocation(self, loc):
        return _URL_C({_LOCATIONS.LocalLibraryLocation: "toollibraryroot://Local",
                       _LOCATIONS.CloudLibraryLocation: "cloud://",
                       _LOCATIONS.HubLibraryLocation: "hub://"}[loc])
    def childFolderURLs(self, url):
        return [_URL_C("hub://Team")] if url.toString() == "hub://" else []
    def importToolLibrary(self, lib, dest, name):
        if dest.toString().startswith("systemlibraryroot://"):
            raise RuntimeError("read-only")
        self.imported.append((lib, dest, name))
        return _URL_C(dest.toString().rstrip("/") + "/" + name)
    def toolLibraryAtURL(self, url):
        return self.imported[-1][0] if (self._loads_back and self.imported) else None


class _URL_C:
    def __init__(self, s):
        self._s = s
    def toString(self):
        return self._s


def _install_create(monkeypatch, src_count=3):
    libs = _CreateLibs()
    monkeypatch.setattr(ct, "_tool_libraries", lambda: libs)
    monkeypatch.setattr(ct, "_empty_library", _NewLib)
    src = _SrcLib([_Tool("A"), _Tool("B"), _Tool("C")][:src_count])
    monkeypatch.setattr(ct, "_source_tool", lambda url, idx: (src.item(idx), None) if 0 <= idx < src.count
                        else (None, "tool_index %d out of range" % idx))
    return libs


class TestCreateLibrary:
    def test_create_empty_local(self, monkeypatch):
        libs = _install_create(monkeypatch)
        out = _payload(ct.handler(action="create_library", scope="local", library="MCP Test Local"))
        assert len(libs.imported) == 1
        _, dest, name = libs.imported[0]
        assert name == "MCP Test Local" and dest.toString() == "toollibraryroot://Local"
        assert out["created_library"] == "MCP Test Local" and out["tool_count"] == 0

    def test_create_with_seeds(self, monkeypatch):
        libs = _install_create(monkeypatch)
        out = _payload(ct.handler(action="create_library", scope="cloud", library="MCP Test Cloud",
                                  add_tools=[{"library_url": "u", "index": 0},
                                             {"library_url": "u", "index": 1}]))
        lib, _, _ = libs.imported[0]
        assert lib.count == 2 and out["tool_count"] == 2

    def test_created_library_that_does_not_load_back_bites(self, monkeypatch):
        # importToolLibrary returned a URL but nothing loads back from it -> error, not created
        libs = _install_create(monkeypatch)
        libs._loads_back = False
        res = ct.handler(action="create_library", scope="local", library="Ghost Lib")
        assert res["isError"] is True
        assert "did not land" in res["message"]

    def test_hub_descends_to_team_folder(self, monkeypatch):
        libs = _install_create(monkeypatch)
        ct.handler(action="create_library", scope="hub", library="MCP Test Hub")
        _, dest, _ = libs.imported[0]
        assert dest.toString() == "hub://Team"      # not the bare hub:// root

    def test_refuses_document_scope(self, monkeypatch):
        _install_create(monkeypatch)
        res = ct.handler(action="create_library", scope="document", library="X")
        assert res["isError"] is True
        assert "Cannot create a library in the document scope" in res["message"]

    def test_requires_name(self, monkeypatch):
        _install_create(monkeypatch)
        res = ct.handler(action="create_library", scope="local", library="")
        assert res["isError"] is True and "name" in res["message"].lower()

    def test_bad_seed_before_import(self, monkeypatch):
        libs = _install_create(monkeypatch)
        res = ct.handler(action="create_library", scope="local", library="X",
                         add_tools=[{"library_url": "u", "index": 99}])
        assert res["isError"] is True and "99" in res["message"]
        assert len(libs.imported) == 0


# ── _preset_param_of: a preset value maps per tool CLASS (a drill preset has no 'tool_feedCutting', ─
# ── a constant-surface-speed turning preset has no 'tool_spindleSpeed'); the resolver falls through ─
# ── the candidates, and names what exists when none match instead of asserting one class's name. ────

def _PParams(names):
    """A preset's parameters, `names` each holding "0"."""
    return _Params({n: "0" for n in names})


def _preset_with(names):
    """A ToolPreset whose parameters collection carries exactly `names` (no bespoke fake class)."""
    from types import SimpleNamespace
    return SimpleNamespace(parameters=_PParams(names))


def _feed_param(preset):
    return cp._preset_param_of(preset, cp._FEED_PARAM_CANDIDATES, "feed")


def _speed_param(preset):
    return cp._preset_param_of(preset, cp._SPEED_PARAM_CANDIDATES, "speed")


# The feed candidates are a priority ORDER. The expected order is pinned HERE rather than read off
# _FEED_PARAM_CANDIDATES, so a reorder of the tuple cannot reorder its own expectation: every pair
# below is driven through the resolver, which makes any transposition fail at that pair.
_FEED_PRIORITY = ("tool_feedCutting", "tool_feedPlunge", "tool_feedRamp",
                  "tool_feedRetract", "tool_feedEntry", "tool_feedTransition")
_FEED_PAIRS = [(earlier, later)
               for i, earlier in enumerate(_FEED_PRIORITY)
               for later in _FEED_PRIORITY[i + 1:]]


class TestPresetParamOf:
    def test_mill_uses_tool_feed_cutting(self):
        p, avail = _feed_param(_preset_with(["tool_spindleSpeed", "tool_feedCutting"]))
        assert p is not None and p.name == "tool_feedCutting" and avail is None

    def test_drill_falls_back_to_plunge_feed(self):
        # a drill preset carries NO tool_feedCutting - the {feed} value goes to its plunge feed
        p, avail = _feed_param(_preset_with(["tool_spindleSpeed", "tool_feedPlunge"]))
        assert p is not None and p.name == "tool_feedPlunge"

    def test_a_preset_carrying_two_feeds_takes_the_cutting_one(self):
        # the candidates are a priority ORDER, not a set: with more than one of them present the
        # {feed} value drives the CUTTING feed, never whichever feed the preset lists first
        p, avail = _feed_param(_preset_with(["tool_feedPlunge", "tool_feedCutting"]))
        assert p is not None and p.name == "tool_feedCutting" and avail is None

    def test_every_feed_candidate_has_a_pinned_priority(self):
        # a candidate added to (or dropped from) the tuple without a place in _FEED_PRIORITY has no
        # pinned priority at all - the pair sweep below never reaches it
        assert sorted(cp._FEED_PARAM_CANDIDATES) == sorted(_FEED_PRIORITY)

    @pytest.mark.parametrize("earlier,later", _FEED_PAIRS)
    def test_the_earlier_feed_candidate_beats_the_later_one(self, earlier, later):
        # the preset lists the LATER candidate first, so what decides is candidate order and not
        # the preset's own collection order
        p, avail = _feed_param(_preset_with([later, earlier]))
        assert p is not None and p.name == earlier and avail is None

    def test_no_known_feed_names_what_exists(self):
        # nothing from the candidate list -> refuse, naming the feed-ish params actually present
        p, avail = _feed_param(_preset_with(["tool_spindleSpeed", "tool_feedGizmo"]))
        assert p is None and avail == ["tool_feedGizmo"]

    def test_no_feed_params_at_all(self):
        p, avail = _feed_param(_preset_with(["tool_spindleSpeed"]))
        assert p is None and avail == []

    def test_mill_uses_tool_spindle_speed(self):
        p, avail = _speed_param(_preset_with(["tool_spindleSpeed", "tool_feedCutting"]))
        assert p is not None and p.name == "tool_spindleSpeed" and avail is None

    def test_surface_speed_preset_has_no_spindle_speed_and_names_what_it_has(self):
        # a turning preset cutting at constant surface speed - the spindle_speed value has nowhere
        # to go, and the refusal must name tool_surfaceSpeed rather than a mill-only parameter
        p, avail = _speed_param(_preset_with(["tool_surfaceSpeed", "tool_feedCutting"]))
        assert p is None and avail == ["tool_surfaceSpeed"]


# ── the from_type vocabulary comes from the bundled Fusion360 sample libraries ──────────────────

class _AssetURL:
    def __init__(self, leaf):
        self.leafName = leaf
    def toString(self):
        return "systemlibraryroot://Samples/" + self.leafName


class _SampleAssets:
    """ToolLibraries over the bundled Fusion360 sample assets: leaf name -> the tool types it holds.
    Each toolLibraryAtURL is a CLOUD FETCH live (~1.4-6.7s each, measured), so `fetched` records
    them - a test can then pin that a lookup stopped early instead of walking all five."""
    def __init__(self, by_leaf):
        self._by_leaf = by_leaf
        self.fetched = []
    def urlByLocation(self, loc):
        return _AssetURL("Fusion360")
    def childAssetURLs(self, url):
        return [_AssetURL(leaf) for leaf in self._by_leaf]
    def toolLibraryAtURL(self, url):
        self.fetched.append(url.leafName)
        return _SrcLib([_Tool(ty, tool_type=ty) for ty in self._by_leaf.get(url.leafName, [])])


def _install_samples(monkeypatch, by_leaf):
    from types import SimpleNamespace
    import adsk.cam as _c
    import adsk.core as _core
    assets = _SampleAssets(by_leaf)
    mgr = SimpleNamespace(libraryManager=SimpleNamespace(toolLibraries=assets))
    monkeypatch.setattr(_c.CAMManager, "get", lambda: mgr)
    # a type-map entry stores the library's url STRING; _source_tool turns it back into a URL
    monkeypatch.setattr(_core.URL, "create", lambda s: _AssetURL(s.rsplit("/", 1)[-1]))
    monkeypatch.setattr(ct, "_type_map_cache", None)      # the map is cached across calls
    monkeypatch.setattr(ct, "_library_cache", {})         # as are the fetched libraries
    return assets


# A miniature stand-in for the bundled Fusion360 sample assets - a handful of leaf names and the
# tool types behind them, shaped like the real set (an Inch library repeating its Metric twin, and
# a Hole Making Tools (Inch) that adds 'center drill') so the walk can be pinned without them.
_SAMPLE_ASSETS = {
    "Milling Tools (Metric)": ["flat end mill", "ball end mill"],
    "Milling Tools (Inch)": ["flat end mill"],
    "Hole Making Tools (Metric)": ["drill"],
    "Hole Making Tools (Inch)": ["drill", "center drill"],
    "Cutting Tools (Metric)": ["laser cutter"],
    "Turning Tools (Metric)": ["turning general", "turning threading"],
    "Turning Tools (Inch)": ["turning general"],
    "Probes": ["probe"],
}


class TestSampleLibraryFetchCost:
    """Each sample library is a cloud round-trip (~13s for all five, measured live) against a 30s
    handler budget, so a lookup must read only as far as it needs."""

    def test_a_type_in_the_first_library_reads_only_that_library(self, monkeypatch):
        # ONE fetch, not one per read: the type walk and _source_tool share the cached library.
        assets = _install_samples(monkeypatch, _SAMPLE_ASSETS)
        tool, err = ct._sample_for_type("flat end mill")
        assert err is None and tool is not None
        assert assets.fetched == ["Milling Tools (Metric)"]

    def test_a_later_type_stops_at_the_library_holding_it(self, monkeypatch):
        assets = _install_samples(monkeypatch, _SAMPLE_ASSETS)
        _tool, err = ct._sample_for_type("drill")
        assert err is None
        assert "Turning Tools (Metric)" not in assets.fetched   # never reached

    def test_a_second_lookup_scans_no_further_libraries(self, monkeypatch):
        # The type MAP is cached, so a repeat lookup must not widen the walk - it still pays
        # _source_tool's own read of the one library it already knows about.
        assets = _install_samples(monkeypatch, _SAMPLE_ASSETS)
        ct._sample_for_type("flat end mill")
        before = set(assets.fetched)
        ct._sample_for_type("flat end mill")
        assert set(assets.fetched) == before

    def test_an_unknown_type_still_lists_the_whole_vocabulary(self, monkeypatch):
        # the early stop must not shrink the refusal's "Available types" to what happened to be read
        assets = _install_samples(monkeypatch, _SAMPLE_ASSETS)
        tool, err = ct._sample_for_type("no such tool")
        assert tool is None
        for expected in ("flat end mill", "drill", "turning general", "center drill"):
            assert expected in err


class TestSampleTypeMap:
    def test_turning_types_resolve_to_the_turning_sample_library(self, monkeypatch):
        # without the Turning sample library in the walk, add_tools[].from_type cannot clone a
        # turning tool at all - no turning type is in the vocabulary.
        _install_samples(monkeypatch, _SAMPLE_ASSETS)
        tmap = ct._build_type_map()
        assert "turning general" in tmap and "turning threading" in tmap
        url, index = tmap["turning general"]
        assert url.endswith("Turning Tools (Metric)") and index == 0

    def test_center_drill_comes_from_the_inch_hole_making_library(self, monkeypatch):
        # the one type no Metric sample library holds - without that Inch library in the walk it is
        # not cloneable at all.
        _install_samples(monkeypatch, _SAMPLE_ASSETS)
        url, index = ct._build_type_map()["center drill"]
        assert url.endswith("Hole Making Tools (Inch)") and index == 1

    def test_a_shared_type_keeps_its_metric_library(self, monkeypatch):
        # 'drill' is in both hole-making libraries; the Metric one is walked first and wins the key,
        # so adding the Inch library never re-points an existing type at an inch tool.
        _install_samples(monkeypatch, _SAMPLE_ASSETS)
        url, _ = ct._build_type_map()["drill"]
        assert url.endswith("Hole Making Tools (Metric)")

    def test_map_walks_only_the_named_libraries(self, monkeypatch):
        # Milling/Turning Inch add nothing their Metric twins lack, so neither is walked. Probes
        # IS: it is the one shipped library holding the 'probe' type, which a probing operation
        # needs and no cutting-tool library carries.
        _install_samples(monkeypatch, _SAMPLE_ASSETS)
        assert set(ct._build_type_map()) == {"flat end mill", "ball end mill", "drill",
                                             "center drill", "laser cutter", "turning general",
                                             "turning threading", "probe"}

    def test_the_probe_type_is_offered_and_clones_from_the_shipped_probes_library(self, monkeypatch):
        # THE BITE: with 'Probes' out of the sample list the vocabulary carries no probe at all,
        # so a probing operation has no tool to be created with.
        assets = _install_samples(monkeypatch, _SAMPLE_ASSETS)
        out = _payload(ct.handler(action="list_types"))
        assert "probe" in out["types"]
        src, serr = ct._sample_for_type("probe")
        assert serr is None and src.desc == "probe"
        assert "Probes" in assets.fetched

    def test_a_turning_type_clones_through_the_add_path(self, monkeypatch):
        # _sample_for_type resolves 'turning general' through the same map to a real source tool.
        _install_samples(monkeypatch, _SAMPLE_ASSETS)
        src, serr = ct._sample_for_type("turning general")
        assert serr is None and src.desc == "turning general"

    def test_unknown_type_lists_the_turning_types_too(self, monkeypatch):
        _install_samples(monkeypatch, _SAMPLE_ASSETS)
        src, serr = ct._sample_for_type("banana mill")
        assert src is None and "turning general" in serr


class TestLibraryCache:
    """The cache holds LIVE ToolLibrary objects for the life of the process, so it is bounded, and a
    library that has just been written to is dropped from it - handing the next call a pre-write copy
    would answer out of a library that no longer matches storage."""

    def test_the_cache_is_bounded_and_evicts_the_oldest(self, monkeypatch):
        monkeypatch.setattr(ct, "_library_cache", {})
        for i in range(ct._LIBRARY_CACHE_MAX + 3):
            ct._cache_library(f"lib{i}", object())
        assert len(ct._library_cache) == ct._LIBRARY_CACHE_MAX
        assert "lib0" not in ct._library_cache          # the oldest went first
        assert f"lib{ct._LIBRARY_CACHE_MAX + 2}" in ct._library_cache

    def test_a_persist_drops_the_written_library_from_the_cache(self, monkeypatch):
        # _resolve_target's persist writes THIS library; any cached copy of it is now stale.
        from types import SimpleNamespace
        monkeypatch.setattr(ct, "_library_cache", {})
        asset = _AssetURL("L")
        key = asset.toString()
        ct._cache_library(key, object())            # a copy fetched BEFORE the write
        updated = []
        libs = SimpleNamespace(
            urlByLocation=lambda loc: _AssetURL("root"),
            childAssetURLs=lambda url: [asset],
            childFolderURLs=lambda url: [],
            toolLibraryAtURL=lambda url: _SrcLib([_Tool("A")]),
            updateToolLibrary=lambda url, lib: updated.append(url.toString()))
        monkeypatch.setattr(ct, "_tool_libraries", lambda: libs)
        target, err = ct._resolve_target("cloud", "L")
        assert err is None
        target.persist()
        assert updated == [key]                     # the write happened...
        assert key not in ct._library_cache         # ...and the pre-write copy is gone

    def test_a_cached_library_is_not_re_fetched(self, monkeypatch):
        # the reason the cache exists: a fetch is a cloud round-trip (seconds, measured)
        assets = _install_samples(monkeypatch, _SAMPLE_ASSETS)
        ct._build_type_map("flat end mill")
        before = list(assets.fetched)
        src, err = ct._source_tool("systemlibraryroot://Samples/Milling Tools (Metric)", 0)
        assert err is None and src is not None
        assert assets.fetched == before             # served from the cache, no second round-trip


class TestSampleFetchFailureIsRetried:
    """A cloud fetch that comes back with nothing is transient. Recording it as scanned would
    truncate the type vocabulary for the whole process life - and the full-walk fallback, which is
    what makes a miss list every type, would short out on the same record."""

    def _flaky(self, monkeypatch, failing_leaf):
        """The sample assets with one library that fails its FIRST fetch and succeeds after."""
        assets = _install_samples(monkeypatch, _SAMPLE_ASSETS)
        real = assets.toolLibraryAtURL
        failed = []

        def _fetch(url):
            if url.leafName == failing_leaf and not failed:
                failed.append(url.leafName)
                assets.fetched.append(url.leafName)
                return None                      # the cloud round-trip came back empty
            return real(url)

        monkeypatch.setattr(assets, "toolLibraryAtURL", _fetch)
        return assets

    def test_a_failed_fetch_is_retried_and_its_types_come_back(self, monkeypatch):
        self._flaky(monkeypatch, "Turning Tools (Metric)")
        assert "turning general" not in ct._build_type_map()   # the failed fetch contributed nothing
        assert "turning general" in ct._build_type_map()       # ...and the next call retries it

    def test_a_type_behind_a_failed_fetch_is_still_cloneable(self, monkeypatch):
        # The user-visible consequence: from_type='turning general' must not be refused because one
        # cloud read blipped. _sample_for_type's own full-walk fallback re-reads the failed library
        # in the SAME call, so the clone lands.
        self._flaky(monkeypatch, "Turning Tools (Metric)")
        src, serr = ct._sample_for_type("turning general")
        assert serr is None and src.desc == "turning general"

    def test_an_empty_asset_listing_does_not_retire_the_whole_vocabulary(self, monkeypatch):
        # The worst case of the same bug: the ROOT read blips and no sample library is found at all.
        # Marking all five scanned would leave the type vocabulary permanently empty.
        assets = _install_samples(monkeypatch, _SAMPLE_ASSETS)
        blipped = []

        def _children(url):
            if not blipped:
                blipped.append(True)
                return []
            return [_AssetURL(leaf) for leaf in _SAMPLE_ASSETS]

        monkeypatch.setattr(assets, "childAssetURLs", _children)
        assert ct._build_type_map() == {}
        assert "flat end mill" in ct._build_type_map()

    def test_a_successful_library_is_still_scanned_only_once(self, monkeypatch):
        # The retry must not cost a re-fetch of the libraries that DID come back.
        assets = self._flaky(monkeypatch, "Turning Tools (Metric)")
        ct._build_type_map()
        before = list(assets.fetched)
        ct._build_type_map()
        added = assets.fetched[len(before):]
        assert added == ["Turning Tools (Metric)"]      # only the one that failed is re-read


# ── presets on an EXISTING tool: add_preset / remove_preset ─────────────────────────────────────

def _tool_with_presets(desc, names=(), **params):
    t = _Tool(desc, **params)
    t.presets = _Presets(names, owner=t)
    return t


class TestAddPreset:
    def test_adds_a_named_preset_with_its_values(self, monkeypatch):
        tool = _tool_with_presets("EM")
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        out = _payload(ct.handler(action="add_preset", scope="document", tool=0,
                                  preset={"name": "Alu 6061", "spindle_speed": 12000, "feed": 900}))
        assert _preset_names(tool) == ["Alu 6061"]
        p = tool.presets.item(0)
        assert p.parameters.itemByName("tool_spindleSpeed").expression == "12000"
        assert p.parameters.itemByName("tool_feedCutting").expression == "900"
        assert out["preset_index"] == 0 and out["preset_count"] == 1
        assert out["presets"] == ["Alu 6061"]
        assert tgt.updated and tgt.persisted == 0        # document scope commits via update_tool

    def test_shared_scope_persists_the_library(self, monkeypatch):
        tool = _tool_with_presets("EM", ["Steel"])
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=False))
        out = _payload(ct.handler(action="add_preset", scope="cloud", library="L", tool=0,
                                  preset={"name": "Alu 6061"}))
        assert tgt.persisted == 1 and tgt.updated == []
        assert out["presets"] == ["Steel", "Alu 6061"]   # appended after the existing preset

    def test_duplicate_name_refused(self, monkeypatch):
        tool = _tool_with_presets("EM", ["Alu 6061"])
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0,
                         preset={"name": "alu 6061"})       # same name, different case
        assert res["isError"] is True and "already has a preset" in res["message"]
        assert tool.presets.count == 1

    def test_detached_preset_that_never_lands_in_the_collection_errors(self, monkeypatch):
        # add() hands back a preset that is NOT in the collection - the values apply to an object
        # nobody can reach again, so a bare 'added' would be a lie.
        class _Detached(_Presets):
            def add(self):
                return _Preset()

        tool = _tool_with_presets("EM")
        tool.presets = _Detached()
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True and "did not take" in res["message"]

    def test_name_that_does_not_land_errors_and_rolls_the_preset_back(self, monkeypatch):
        # The assigned name is read back, so a name that does not store is an error and the empty
        # preset add() created is not left behind.
        class _Unnamed(_Preset):
            def __setattr__(self, key, value):
                if key == "name" and getattr(self, "name", None) is not None:
                    return                              # the assignment is accepted and ignored
                object.__setattr__(self, key, value)

        class _UnnamedPresets(_Presets):
            def add(self):
                p = _Unnamed(); self._items.append(p); return p

        tool = _tool_with_presets("EM")
        tool.presets = _UnnamedPresets()
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True and "did not land" in res["message"]
        assert tool.presets.count == 0                  # rolled back, no half-populated preset
        assert tgt.updated == [] and tgt.persisted == 0

    def test_missing_spindle_speed_parameter_rolls_back_and_errors(self, monkeypatch):
        class _Bare(_Preset):
            def __init__(self, name=""):
                super().__init__(name)
                self.parameters = _Params({})

        class _BarePresets(_Presets):
            def add(self):
                p = _Bare(); self._items.append(p); return p

        tool = _tool_with_presets("EM")
        tool.presets = _BarePresets()
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0,
                         preset={"name": "Alu", "spindle_speed": 9000})
        assert res["isError"] is True and "tool_spindleSpeed" in res["message"]
        assert tool.presets.count == 0                  # nothing half-built survives

    def test_persisted_library_without_the_preset_bites(self, monkeypatch):
        # update_tool/updateToolLibrary returning is not proof - the tool re-read from the library
        # has no such preset, so the add did not persist.
        tool = _tool_with_presets("EM")
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        tgt.reread_preset_names = lambda index: []
        res = ct.handler(action="add_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True and "did not reach the library" in res["message"]

    def test_requires_a_name(self, monkeypatch):
        _install(monkeypatch, _Target(tools=[_tool_with_presets("EM")], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0, preset={"spindle_speed": 900})
        assert res["isError"] is True and "'name'" in res["message"]

    def test_bad_tool_index(self, monkeypatch):
        _install(monkeypatch, _Target(tools=[_tool_with_presets("EM")], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=4, preset={"name": "Alu"})
        assert res["isError"] is True and "0..0" in res["message"]

    def test_document_scope_note_claims_presence_not_persistence(self, monkeypatch):
        # a re-read of the document library returns the document's own unsaved state, so the note
        # may claim the preset is there - not that anything was stored.
        _install(monkeypatch, _Target(tools=[_tool_with_presets("EM")], is_document=True))
        out = _payload(ct.handler(action="add_preset", scope="document", tool=0,
                                  preset={"name": "Alu"}))
        assert "persist" not in out["note"].lower() and "doc_save" in out["note"]

    def test_document_scope_add_preset_never_claims_storage_though_the_refetch_reads(self, monkeypatch):
        # Readable document refetch: the preset is confirmed PRESENT, never stored - weak flag, and
        # the note carries the document rung rather than the persistence claim.
        _install(monkeypatch, _Target(tools=[_tool_with_presets("EM")], is_document=True))
        out = _payload(ct.handler(action="add_preset", scope="document", tool=0,
                                  preset={"name": "Alu"}))
        assert out["presets"] == ["Alu"]                 # the effect still reports
        assert out["verified_in_memory_only"] is True
        assert "present, not that it was stored" in out["note"] and "doc_save" in out["note"]
        assert "persist" not in out["note"].lower()

    def test_shared_scope_note_claims_persistence(self, monkeypatch):
        # the shared branch round-trips through the library url, which does prove the write stored
        _install(monkeypatch, _Target(tools=[_tool_with_presets("EM")], is_document=False))
        out = _payload(ct.handler(action="add_preset", scope="cloud", library="L", tool=0,
                                  preset={"name": "Alu"}))
        assert "persisted" in out["note"] and out["verified_in_memory_only"] is False

    def test_unreadable_refetch_drops_the_persistence_claim(self, monkeypatch):
        # The library could not be re-read, so nothing proves the preset reached storage - the
        # payload keeps the in-memory verdict and names that weaker basis.
        tgt = _install(monkeypatch, _Target(tools=[_tool_with_presets("EM")], is_document=False))
        tgt._fresh = lambda: None
        out = _payload(ct.handler(action="add_preset", scope="cloud", library="L", tool=0,
                                  preset={"name": "Alu"}))
        assert out["presets"] == ["Alu"]                 # the in-memory read still reports it
        assert out["verified_in_memory_only"] is True
        assert "persisted" not in out["note"] and "in-memory" in out["note"]


# ── preset VALUES: a CAM parameter stores an expression it cannot evaluate and still reads a ────
# ── finite 0.0 back, so setting one is only done when .error is clear AND the value read back ───
# ── agrees with what was asked for. ─────────────────────────────────────────────────────────────

class _BrokenParam(_Param):
    """A CAM parameter whose expression is stored verbatim but never evaluates: .error carries the
    platform's message and .value stays where it was."""
    error = "Failed to evaluate expression."
    warning = ""

    @property
    def expression(self):
        return self._expression

    @expression.setter
    def expression(self, v):
        self._expression = v


class _StuckParam(_Param):
    """A parameter that accepts the expression, reports no error, and evaluates to something else."""
    error = ""
    warning = ""

    @property
    def expression(self):
        return self._expression

    @expression.setter
    def expression(self, v):
        self._expression = v
        self.value = _val(0.0)


def _preset_param_tool(cls, pname):
    """A tool whose presets carry `pname` as an instance of the misbehaving parameter class."""
    tool = _tool_with_presets("EM")

    class _Presets2(_Presets):
        def add(self):
            p = super().add()
            _replace(p.parameters, cls(pname, "0"))
            return p

    tool.presets = _Presets2(owner=tool)
    return tool


class TestPresetValues:
    def test_expression_that_does_not_evaluate_errors_and_rolls_back(self, monkeypatch):
        tool = _preset_param_tool(_BrokenParam, "tool_spindleSpeed")
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        # the expression opens with a number, so it clears the spec gate and reaches the parameter,
        # which stores it verbatim and reads a finite value back - only .error reveals the break
        res = ct.handler(action="add_preset", scope="document", tool=0,
                         preset={"name": "Alu", "spindle_speed": "900 * NoSuchParamXyz"})
        assert res["isError"] is True and "failed to evaluate" in res["message"]
        assert tool.presets.count == 0
        assert tgt.updated == [] and tgt.persisted == 0

    def test_value_that_reads_back_different_errors_and_rolls_back(self, monkeypatch):
        tool = _preset_param_tool(_StuckParam, "tool_feedCutting")
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0,
                         preset={"name": "Alu", "feed": 900})
        assert res["isError"] is True and "did not land" in res["message"]
        assert "mm/min" in res["message"]
        assert tool.presets.count == 0
        assert tgt.updated == [] and tgt.persisted == 0

    def test_units_carrying_expression_is_accepted_and_stored_verbatim(self, monkeypatch):
        # a bare number is a fixed unit whatever the document uses, so a string carries its own. The
        # read-back must judge such a value by what the parameter EVALUATED to - judging it by
        # comparing against the text would fail this add, so _payload's no-error gate is the assert.
        tool = _tool_with_presets("EM")
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        out = _payload(ct.handler(action="add_preset", scope="document", tool=0,
                                  preset={"name": "Alu", "feed": "35in/min"}))
        assert out["preset_count"] == 1 and out["presets"] == ["Alu"]
        p = tool.presets.item(0).parameters.itemByName("tool_feedCutting")
        assert p.expression == "35in/min"        # stored verbatim, units and all

    def test_turning_preset_without_spindle_speed_names_its_surface_speed(self, monkeypatch):
        # a turning preset cutting at constant surface speed carries no tool_spindleSpeed - the
        # refusal names what it DOES carry instead of asserting the mill parameter.
        tool = _tool_with_presets("Turning General", preset_params=_TURNING_CUTTING_DATA)
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0,
                         preset={"name": "Steel", "spindle_speed": 400})
        assert res["isError"] is True
        assert "tool_surfaceSpeed" in res["message"] and "tool_spindleSpeed" in res["message"]
        assert tool.presets.count == 0                  # rolled back

    def test_turning_preset_takes_a_feed(self, monkeypatch):
        # the same turning preset still carries a cutting feed - only the speed has nowhere to go
        tool = _tool_with_presets("Turning General", preset_params=_TURNING_CUTTING_DATA)
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        _payload(ct.handler(action="add_preset", scope="document", tool=0,
                            preset={"name": "Steel", "feed": 250}))
        assert tool.presets.item(0).parameters.itemByName("tool_feedCutting").expression == "250"

    def test_a_mistyped_key_is_refused_not_silently_dropped(self, monkeypatch):
        tool = _tool_with_presets("EM")
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0,
                         preset={"name": "Alu", "spindel_speed": 12000})
        assert res["isError"] is True and "spindel_speed" in res["message"]
        assert tool.presets.count == 0                  # nothing was created

    def test_a_value_that_is_not_a_number_or_expression_is_refused(self, monkeypatch):
        tool = _tool_with_presets("EM")
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0,
                         preset={"name": "Alu", "feed": "fast"})
        assert res["isError"] is True and "'fast'" in res["message"]
        assert "does not open with a number" in res["message"]     # refused, never handed to a param
        assert tool.presets.count == 0

    def test_a_non_scalar_value_is_refused(self, monkeypatch):
        tool = _tool_with_presets("EM")
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0,
                         preset={"name": "Alu", "spindle_speed": [12000]})
        assert res["isError"] is True and "rpm" in res["message"]
        assert tool.presets.count == 0

    def test_a_boolean_value_is_refused_before_it_reaches_the_parameter(self, monkeypatch):
        # bool is a SUBCLASS of int, so a number check alone accepts True and the expression 'True'
        # reaches the preset parameter; the spec gate refuses it by TYPE instead, and no preset is
        # created to roll back.
        tool = _tool_with_presets("EM")
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0,
                         preset={"name": "Alu", "feed": True})
        assert res["isError"] is True
        assert "must be a number" in res["message"] and "mm/min" in res["message"]
        assert tool.presets.count == 0

    def test_creation_time_presets_are_validated_too(self, monkeypatch):
        # add_tools[].presets[] runs the same spec gate, before any preset is created
        tgt = _install(monkeypatch)
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill", "presets": [{"feed_rate": 900}]}])
        assert res["isError"] is True and "feed_rate" in res["message"]
        assert len(tgt.tools) == 2 and tgt.persisted == 0


class TestPresetSpecNumberGate:
    """_preset_spec_error is the FIRST judge of a preset spec's VALUES - it refuses by TYPE before
    any preset is created, where the downstream read-back gate judges only what LANDED - and a bool
    is what an isinstance(int, float) test alone lets through: True IS an int. The gate is read
    directly here because branch coverage cannot see an 'or' sub-clause: the bool half can be
    deleted with the line still counted covered."""

    def test_a_boolean_feed_is_refused_and_named(self):
        err = cp._preset_spec_error({"name": "Alu", "feed": True})
        assert err is not None
        assert "must be a number" in err and "mm/min" in err
        assert "True" in err                    # the refusal names the offending value

    def test_a_boolean_spindle_speed_is_refused(self):
        # False is the other bool AND falsy - the skip above the type test is `is None`, so it
        # reaches the test rather than reading as "no speed asked for"
        err = cp._preset_spec_error({"name": "Alu", "spindle_speed": False})
        assert err is not None and "must be a number" in err and "rpm" in err

    @pytest.mark.parametrize("value", [900, 900.5, 0, -1])
    def test_a_plain_number_clears_the_gate(self, value):
        # the boundary the bool clause sits beside: an int or a float is legal, 0 and a negative
        # included, so refusing bools can never become refusing numbers
        assert cp._preset_spec_error({"name": "Alu", "feed": value}) is None

    def test_the_preset_input_states_the_fixed_unit_a_bare_number_carries(self):
        # The gate never fires on {"name": "Rough", "feed": 500}, so the wire is the only place
        # an agent learns a bare number is rpm / mm-per-min whatever the document's units.
        desc = ct.tool.to_dict()["inputSchema"]["properties"]["preset"]["description"]
        assert "rpm / mm-per-min" in desc and "35in/min" in desc


class TestRemovePreset:
    def test_removes_the_named_preset_by_index(self, monkeypatch):
        tool = _tool_with_presets("EM", ["Steel", "Alu 6061", "Brass"])
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        out = _payload(ct.handler(action="remove_preset", scope="document", tool=0,
                                  preset={"name": "Alu 6061"}))
        assert _preset_names(tool) == ["Steel", "Brass"]
        assert out["removed_index"] == 1 and out["preset_count"] == 2
        assert out["presets"] == ["Steel", "Brass"] and tgt.updated

    def test_document_scope_removal_never_claims_storage_though_the_refetch_reads(self, monkeypatch):
        # Readable document refetch: the removal is confirmed gone from the document library, which
        # is presence, not storage - weak flag, document-rung note, effect still reported.
        _install(monkeypatch, _Target(tools=[_tool_with_presets("EM", ["Alu"])], is_document=True))
        out = _payload(ct.handler(action="remove_preset", scope="document", tool=0,
                                  preset={"name": "Alu"}))
        assert out["removed_index"] == 0 and out["presets"] == []   # the effect still reports
        assert out["verified_in_memory_only"] is True
        assert "present, not that it was stored" in out["note"] and "doc_save" in out["note"]
        assert "persist" not in out["note"].lower()

    def test_matches_case_insensitively(self, monkeypatch):
        tool = _tool_with_presets("EM", ["Alu 6061"])
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        _payload(ct.handler(action="remove_preset", scope="document", tool=0,
                            preset={"name": "ALU 6061"}))
        assert _preset_names(tool) == []

    def test_a_name_is_never_a_wildcard_pattern(self, monkeypatch):
        # ToolPresets.itemsByName reads '*' as a wild-card; the preset NAME must not, or 'Alu*'
        # would target 'Alu 6061' instead of the preset actually called 'Alu*'.
        tool = _tool_with_presets("EM", ["Alu 6061", "Alu*"])
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        out = _payload(ct.handler(action="remove_preset", scope="document", tool=0,
                                  preset={"name": "Alu*"}))
        assert out["removed_index"] == 1 and _preset_names(tool) == ["Alu 6061"]

    def test_an_unreadable_preset_holds_its_slot_so_the_indices_stay_aligned(self, monkeypatch):
        # The published 'presets' list is read against preset_count AND against the index the
        # removal reports, so a preset whose read fails must come back as a null in place. Dropping
        # it would shorten the list and slide every later name onto the wrong index.
        class _OneUnreadable(_Presets):
            def item(self, i):
                if i == 1:
                    raise RuntimeError("preset read failed")
                return self._items[i]

        tool = _tool_with_presets("EM", ["Steel", "Ghost", "Brass"])
        tool.presets = _OneUnreadable(["Steel", "Ghost", "Brass"])
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        tgt._fresh = lambda: None            # no library re-read: the in-memory names are published
        out = _payload(ct.handler(action="remove_preset", scope="document", tool=0,
                                  preset={"name": "Brass"}))
        assert out["removed_index"] == 2                    # Brass is still addressed at 2
        assert out["presets"] == ["Steel", None]            # the survivors, slot for slot

    def test_a_name_is_matched_WHOLE_never_as_a_substring(self, monkeypatch):
        # 'Rough' and 'Roughing' are two different presets with two different feeds. A substring
        # match would remove whichever came first - deleting cutting data the caller never named.
        tool = _tool_with_presets("EM", ["Roughing", "Rough"])
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        out = _payload(ct.handler(action="remove_preset", scope="document", tool=0,
                                  preset={"name": "Rough"}))
        assert out["removed_index"] == 1 and _preset_names(tool) == ["Roughing"]

    def test_a_longer_existing_name_is_not_the_named_preset(self, monkeypatch):
        # The other direction: 'Rough' alone on the tool is NOT the preset called 'Roughing', so the
        # removal must refuse and list what is there rather than take the near miss.
        tool = _tool_with_presets("EM", ["Rough"])
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="remove_preset", scope="document", tool=0,
                         preset={"name": "Roughing"})
        assert res["isError"] is True and "Rough" in res["message"]
        assert _preset_names(tool) == ["Rough"]          # nothing removed

    def test_add_preset_does_not_read_a_longer_name_as_a_duplicate(self, monkeypatch):
        # The duplicate gate runs through the same matcher: 'Rough' must still be addable to a tool
        # that already carries 'Roughing'.
        tool = _tool_with_presets("EM", ["Roughing"])
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        out = _payload(ct.handler(action="add_preset", scope="document", tool=0,
                                  preset={"name": "Rough"}))
        assert out["preset_index"] == 1 and _preset_names(tool) == ["Roughing", "Rough"]

    def test_ambiguous_name_refused_with_candidates(self, monkeypatch):
        tool = _tool_with_presets("EM", ["Alu", "Steel", "alu"])
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="remove_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True
        assert "0, 2" in res["message"]                  # both candidate indices named
        assert _preset_names(tool) == ["Alu", "Steel", "alu"]     # nothing removed
        assert tgt.updated == [] and tgt.persisted == 0

    def test_unknown_name_lists_what_the_tool_has(self, monkeypatch):
        tool = _tool_with_presets("EM", ["Steel", "Brass"])
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="remove_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True
        assert "Steel, Brass" in res["message"] and tool.presets.count == 2

    def test_a_long_preset_list_is_capped_and_the_remainder_counted(self, monkeypatch):
        # A cloned mill ships 25 presets. An uncapped join floods the wire AND reads as the whole
        # set; the shared renderer names the cap's worth and COUNTS the rest.
        names = [f"P{i:02d}" for i in range(25)]
        tool = _tool_with_presets("EM", names)
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="remove_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True
        assert "P07" in res["message"] and "P08" not in res["message"]
        assert "(+17 more not listed)" in res["message"]

    def test_no_presets_at_all_says_none(self, monkeypatch):
        tool = _tool_with_presets("EM")
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="remove_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True and "(none)" in res["message"]

    def test_remove_reporting_failure_errors(self, monkeypatch):
        # ToolPresets.remove returns false for an unsuccessful deletion - never report it as removed.
        class _Refuses(_Presets):
            def remove(self, index):
                return False

        tool = _tool_with_presets("EM", ["Alu"])
        tool.presets = _Refuses(["Alu"])
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="remove_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True and "reported failure" in res["message"]

    def test_survivor_of_the_same_name_errors(self, monkeypatch):
        # remove() claims success but the preset is still on the tool - a lying delete, not an ok.
        class _NoOp(_Presets):
            def remove(self, index):
                return True

        tool = _tool_with_presets("EM")
        tool.presets = _NoOp(["Alu"])
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="remove_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True and "did not take" in res["message"]
        assert tgt.updated == [] and tgt.persisted == 0

    def test_persisted_library_that_still_holds_it_bites(self, monkeypatch):
        tool = _tool_with_presets("EM", ["Alu"])
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        tgt.reread_preset_names = lambda index: ["Alu"]
        res = ct.handler(action="remove_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True and "did not reach the library" in res["message"]

    def test_preset_must_be_an_object(self, monkeypatch):
        _install(monkeypatch, _Target(tools=[_tool_with_presets("EM", ["Alu"])], is_document=True))
        res = ct.handler(action="remove_preset", scope="document", tool=0, preset="Alu")
        assert res["isError"] is True and "'preset' must be an object" in res["message"]


class TestCreationTimePresetName:
    def test_add_tools_preset_name_is_applied(self, monkeypatch):
        # the creation-time preset path and add_preset share one value applier, so a named preset
        # requested at creation carries that name.
        tgt = _install(monkeypatch)
        _payload(ct.handler(action="add", scope="cloud", library="L",
                            add_tools=[{"from_type": "drill",
                                        "presets": [{"name": "Alu 6061", "spindle_speed": 8000}]}]))
        built = tgt.tools[-1]
        assert _preset_names(built) == ["Alu 6061"]
        assert built.presets.item(0).parameters.itemByName("tool_spindleSpeed").expression == "8000"


# ── misbehaving parameters shared by the guard tests below ──────────────────────────────────────

class _WriteProtected(_Param):
    """A parameter whose expression assignment RAISES - the platform refusing the write."""
    error = ""
    warning = ""

    def __setattr__(self, key, value):
        if key == "expression":
            raise RuntimeError("locked by Fusion")
        object.__setattr__(self, key, value)


class _TextValued(_Param):
    """A parameter that accepts the expression, reports no error, and evaluates to a NON-number."""
    error = ""
    warning = ""

    @property
    def expression(self):
        return self._expression

    @expression.setter
    def expression(self, v):
        self._expression = v
        self.value = _val("n/a")


class _Unnameable(_Preset):
    """A preset whose name assignment raises once it carries a name - the platform refusing it."""
    def __setattr__(self, key, value):
        if key == "name" and getattr(self, "name", None) is not None:
            raise RuntimeError("preset name is read-only")
        object.__setattr__(self, key, value)


# ── the REAL _Target: refetch is the ONE seam every read-back goes through ──────────────────────

class TestRealTargetReadBacks:
    """persisted_count / reread_param / reread_preset_names / stored_tool_numbers all read through
    refetch(). A target that cannot re-read its library must answer None from every one of them -
    a number there would publish evidence nothing produced."""

    def test_a_target_with_no_refetch_answers_none_everywhere(self):
        tgt = ct._Target(_SrcLib([_tool_with_presets("EM", ["Alu"], tool_number="4")]),
                         is_document=True)
        assert tgt.refetch() is None
        assert tgt.persisted_count() is None
        assert tgt.reread_param(0, "tool_number") is None
        assert tgt.reread_preset_names(0) is None
        assert tgt.stored_tool_numbers() is None

    def test_a_refetching_target_reads_the_stored_library_back(self):
        stored = _SrcLib([_tool_with_presets("EM", ["Alu"], tool_number="4")])
        tgt = ct._Target(_SrcLib([_Tool("held", tool_number="9")]), is_document=False,
                         refetch_fn=lambda: stored)
        assert tgt.refetch() is stored
        assert tgt.persisted_count() == 1
        assert tgt.reread_param(0, "tool_number") == "4"     # the STORED value, not the held one
        assert tgt.reread_preset_names(0) == ["Alu"]
        assert tgt.stored_tool_numbers() == [4]

    def test_a_refetch_that_comes_back_empty_proves_nothing(self):
        tgt = ct._Target(_SrcLib([_Tool("EM", tool_number="4")]), is_document=False,
                         refetch_fn=lambda: None)
        assert tgt.persisted_count() is None and tgt.stored_tool_numbers() is None
        assert tgt.reread_param(0, "tool_number") is None
        assert tgt.reread_preset_names(0) is None

    def test_a_tool_index_the_stored_library_does_not_hold_reads_none(self):
        stored = _SrcLib([])
        tgt = ct._Target(_SrcLib([_Tool("EM")]), is_document=False, refetch_fn=lambda: stored)
        assert tgt.reread_param(0, "tool_number") is None
        assert tgt.reread_preset_names(0) is None


class TestRealTargetMutations:
    def test_add_remove_and_update_reach_the_library(self):
        lib = _SrcLib([_Tool("A"), _Tool("B")])
        lib.add = lib._items.append
        lib.remove = lambda i: lib._items.pop(i)
        updated = []
        tgt = ct._Target(lib, is_document=True, update_tool_fn=updated.append)
        tgt.add(_Tool("C"))
        assert [t.desc for t in tgt.tools] == ["A", "B", "C"]
        tgt.remove(0)
        assert [t.desc for t in tgt.tools] == ["B", "C"]
        tool = tgt.tools[0]
        tgt.update_tool(tool)
        assert updated == [tool]

    def test_the_commit_seam_a_target_lacks_is_a_no_op_not_a_raise(self):
        # a shared target has no per-tool updateTool and a document target no library persist
        recorded = []
        tgt = ct._Target(_SrcLib([]), is_document=False,
                         persist_fn=lambda: recorded.append("persist"))
        tgt.update_tool(_Tool("EM"))
        tgt.persist()
        assert recorded == ["persist"]
        doc = ct._Target(_SrcLib([]), is_document=True, update_tool_fn=recorded.append)
        doc.persist()
        assert recorded == ["persist"]      # no persist_fn: nothing else was called


class TestRealTargetOperationsByTool:
    """operationsByTool hands back an OperationVector - index/len accessible, NOT a Python list - so
    the walk tries len()/[i] first and falls back to the shared count/item walk."""

    def test_a_target_with_no_operations_seam_reports_none(self):
        tgt = ct._Target(_SrcLib([]), is_document=True)
        assert tgt.operations_by_tool(_Tool("EM")) == []

    def test_a_null_vector_is_no_operations(self):
        tgt = ct._Target(_SrcLib([]), is_document=True, ops_fn=lambda t: None)
        assert tgt.operations_by_tool(_Tool("EM")) == []

    def test_an_index_len_vector_is_read_in_order(self):
        vec = [FakeOperation("Face1"), FakeOperation("Adaptive1")]
        tgt = ct._Target(_SrcLib([]), is_document=True, ops_fn=lambda t: vec)
        assert tgt.operations_by_tool(_Tool("EM")) == ["Face1", "Adaptive1"]

    def test_a_count_item_collection_falls_back_to_the_shared_walk(self):
        # a collection carrying no len() must not report zero operations - the tool is used
        coll = _NamedCollection([FakeOperation("Face1"), FakeOperation("Adaptive1")])
        tgt = ct._Target(_SrcLib([]), is_document=True, ops_fn=lambda t: coll)
        assert tgt.operations_by_tool(_Tool("EM")) == ["Face1", "Adaptive1"]


# ── the shared-library listing: _tool_libraries / _shared_libraries (the walk itself is ─────────
# ── _cam_common.library_assets, covered in test__cam_common.py) ─────────────────────────────────

class TestSharedLibraryListing:
    def _libs(self, assets=(), folders=()):
        root = _AssetURL("root")
        return root, SimpleNamespace(
            urlByLocation=lambda loc: root,
            childAssetURLs=lambda u: list(assets) if u is root else [_AssetURL("Team Mill")],
            childFolderURLs=lambda u: list(folders) if u is root else [])

    def test_the_libraries_come_off_the_cam_manager_not_the_document(self, monkeypatch):
        # CAMManager.get().libraryManager - the document's CAM product has no libraryManager, so a
        # shared-scope listing must work with no CAM job open at all
        assets = _install_samples(monkeypatch, {})
        assert ct._tool_libraries() is assets

    def test_an_unreadable_library_manager_reads_as_no_libraries(self, monkeypatch):
        import adsk.cam as _c

        def _boom():
            raise RuntimeError("no CAM manager")

        monkeypatch.setattr(_c.CAMManager, "get", _boom)
        assert ct._tool_libraries() is None

    def test_no_tool_libraries_is_an_error_not_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(ct, "_tool_libraries", lambda: None)
        entries, truncated, err = ct._shared_libraries("cloud")
        assert entries is None and truncated is False and "unavailable" in err
        res = ct.handler(action="list", scope="cloud")
        assert res["isError"] is True and "unavailable" in res["message"]

    def test_a_library_nested_in_a_folder_is_listed(self, monkeypatch):
        # Hub/Cloud nest their libraries in folders; a walk reading only the root's own assets
        # would report the team libraries as absent
        _root, libs = self._libs(assets=[], folders=[_AssetURL("Team")])
        monkeypatch.setattr(ct, "_tool_libraries", lambda: libs)
        entries, truncated, err = ct._shared_libraries("hub")
        assert err is None and truncated is False
        assert entries == [{"name": "Team Mill", "url": _AssetURL("Team Mill").toString()}]

    def test_a_root_that_does_not_resolve_lists_nothing(self, monkeypatch):
        # urlByLocation answers None for a location this install has not configured - the listing
        # is then empty, never the root's children read against a null url. (The walk's own depth
        # and folder bounds live on _cam_common.library_assets, covered in test__cam_common.py.)
        libs = SimpleNamespace(urlByLocation=lambda loc: None,
                               childAssetURLs=lambda u: [_AssetURL("L")],
                               childFolderURLs=lambda u: [])
        monkeypatch.setattr(ct, "_tool_libraries", lambda: libs)
        entries, truncated, err = ct._shared_libraries("cloud")
        assert err is None and entries == []

    def test_a_capped_walk_is_disclosed_not_presented_as_complete(self, monkeypatch):
        # the walk reporting truncated must reach the payload - a capped listing that reads as
        # complete asserts absence the search never proved
        monkeypatch.setattr(ct, "_shared_libraries",
                            lambda scope: ([{"name": "Team Mill", "url": "u"}], True, None))
        out = _payload(ct.handler(action="list", scope="hub"))
        assert out["truncated"] is True
        assert "may exist unlisted" in out["note"]


# ── _resolve_target: the document library, and the shared-library guards ────────────────────────

class TestResolveTargetDocument:
    def test_document_scope_needs_an_open_cam_document(self, monkeypatch):
        monkeypatch.setattr(ct, "get_cam", lambda: (None, "Switch to the Manufacture workspace."))
        target, err = ct._resolve_target("document", "")
        assert target is None and "Manufacture" in err
        # and the handler hands that refusal straight through instead of a generic failure
        res = ct.handler(action="add", scope="document", add_tools=[{"from_type": "drill"}])
        assert res["isError"] is True and "Manufacture" in res["message"]

    def test_a_cam_product_without_a_document_library(self, monkeypatch):
        monkeypatch.setattr(ct, "get_cam",
                            lambda: (SimpleNamespace(documentToolLibrary=None), None))
        target, err = ct._resolve_target("document", "")
        assert target is None and err == "No document tool library."

    def test_the_document_target_commits_per_tool_and_re_reads_the_live_library(self, monkeypatch):
        # the document library has NO url: its refetch is the CAM product's own live library, and a
        # tool change commits through updateTool rather than a library persist
        updated = []
        dtl = _SrcLib([_Tool("EM", tool_numberOfFlutes="3")])
        dtl.updateTool = updated.append
        dtl.operationsByTool = lambda t: [FakeOperation("Face1")]
        monkeypatch.setattr(ct, "get_cam",
                            lambda: (SimpleNamespace(documentToolLibrary=dtl), None))
        target, err = ct._resolve_target("document", "")
        assert err is None and target.is_document is True
        assert target.refetch() is dtl
        tool = target.tools[0]
        target.update_tool(tool)
        assert updated == [tool]
        assert target.operations_by_tool(tool) == ["Face1"]


class TestResolveTargetShared:
    def _libs(self, assets, loads=True):
        root = _AssetURL("root")
        return SimpleNamespace(
            urlByLocation=lambda loc: root,
            childAssetURLs=lambda u: list(assets),
            childFolderURLs=lambda u: [],
            toolLibraryAtURL=lambda u: _SrcLib([_Tool("EM")]) if loads else None)

    def test_no_tool_libraries(self, monkeypatch):
        monkeypatch.setattr(ct, "_tool_libraries", lambda: None)
        target, err = ct._resolve_target("cloud", "L")
        assert target is None and "unavailable" in err

    def test_a_shared_scope_with_no_library_names_the_ones_there(self, monkeypatch):
        monkeypatch.setattr(ct, "_tool_libraries", lambda: self._libs([_AssetURL("Team Mill")]))
        target, err = ct._resolve_target("hub", "   ")
        assert target is None
        assert "Provide 'library'" in err and "Team Mill" in err

    def test_an_unknown_library_lists_the_available_ones(self, monkeypatch):
        monkeypatch.setattr(ct, "_tool_libraries", lambda: self._libs([_AssetURL("Team Mill")]))
        target, err = ct._resolve_target("cloud", "Ghost")
        assert target is None
        assert "No cloud library 'Ghost'" in err and "Team Mill" in err

    def test_a_library_resolves_by_url_as_well_as_by_name(self, monkeypatch):
        asset = _AssetURL("Team Mill")
        monkeypatch.setattr(ct, "_tool_libraries", lambda: self._libs([asset]))
        by_name, e1 = ct._resolve_target("cloud", "Team Mill")
        by_url, e2 = ct._resolve_target("cloud", asset.toString())
        assert e1 is None and e2 is None
        assert by_name.is_document is False and len(by_url.tools) == 1

    def test_a_library_that_does_not_load(self, monkeypatch):
        monkeypatch.setattr(ct, "_tool_libraries",
                            lambda: self._libs([_AssetURL("Team Mill")], loads=False))
        target, err = ct._resolve_target("cloud", "Team Mill")
        assert target is None and "Could not load cloud library 'Team Mill'" in err


# ── the library cache guards + _source_tool ─────────────────────────────────────────────────────

class TestLibraryCacheGuards:
    def test_a_fetch_that_came_back_empty_is_never_cached(self, monkeypatch):
        # caching a failed fetch would hand every later call the same nothing
        monkeypatch.setattr(ct, "_library_cache", {})
        ct._cache_library("k", None)
        ct._cache_library("", object())
        assert ct._library_cache == {}

    def test_invalidating_without_a_key_clears_nothing(self, monkeypatch):
        held = object()
        monkeypatch.setattr(ct, "_library_cache", {"k": held})
        ct._invalidate_library(None)
        assert ct._library_cache == {"k": held}


class TestSourceTool:
    _URL = "systemlibraryroot://Samples/Milling Tools (Metric)"

    def test_no_tool_libraries(self, monkeypatch):
        import adsk.cam as _c
        monkeypatch.setattr(ct, "_library_cache", {})
        monkeypatch.setattr(_c.CAMManager, "get", lambda: SimpleNamespace(
            libraryManager=SimpleNamespace(toolLibraries=None)))
        t, err = ct._source_tool(self._URL, 0)
        assert t is None and "unavailable" in err

    def test_an_uncached_library_is_fetched_once_and_then_served_from_the_cache(self, monkeypatch):
        assets = _install_samples(monkeypatch, _SAMPLE_ASSETS)
        t, err = ct._source_tool(self._URL, 1)
        assert err is None and t.desc == "ball end mill"
        assert assets.fetched == ["Milling Tools (Metric)"]
        t2, err2 = ct._source_tool(self._URL, 0)
        assert err2 is None and t2.desc == "flat end mill"
        assert assets.fetched == ["Milling Tools (Metric)"]   # no second cloud round-trip

    def test_a_library_that_does_not_load(self, monkeypatch):
        assets = _install_samples(monkeypatch, _SAMPLE_ASSETS)
        monkeypatch.setattr(assets, "toolLibraryAtURL", lambda u: None)
        t, err = ct._source_tool(self._URL, 0)
        assert t is None and "Could not load source library" in err

    def test_an_index_past_the_end_names_the_library_size(self, monkeypatch):
        _install_samples(monkeypatch, _SAMPLE_ASSETS)
        t, err = ct._source_tool(self._URL, 9)
        assert t is None and "tool_index 9 out of range" in err and "(2 tools)" in err

    def test_a_negative_index_is_out_of_range_too(self, monkeypatch):
        # a bare 'index < count' check would take -1 as the LAST tool
        _install_samples(monkeypatch, _SAMPLE_ASSETS)
        t, err = ct._source_tool(self._URL, -1)
        assert t is None and "out of range" in err


class TestCreationSeams:
    def test_the_factories_are_the_adsk_ones(self, monkeypatch):
        # the two seams every other test stubs: the JSON reaches Tool.createFromJson verbatim, and
        # a new library comes from ToolLibrary.createEmpty
        import adsk.cam as _c
        seen = []
        monkeypatch.setattr(_c.Tool, "createFromJson", lambda js: seen.append(js) or "TOOL")
        monkeypatch.setattr(_c.ToolLibrary, "createEmpty", lambda: "LIB")
        assert ct._tool_from_json('{"description": "d"}') == "TOOL"
        assert seen == ['{"description": "d"}']
        assert ct._empty_library() == "LIB"


class TestFusion360Child:
    def test_finds_the_sample_library_whose_leaf_matches(self, monkeypatch):
        assets = _install_samples(monkeypatch, _SAMPLE_ASSETS)
        libs, url = ct._fusion360_child("Turning Tools")
        assert libs is assets and url.leafName == "Turning Tools (Metric)"

    def test_a_leaf_that_is_not_there_answers_no_url(self, monkeypatch):
        _install_samples(monkeypatch, _SAMPLE_ASSETS)
        libs, url = ct._fusion360_child(ct._HOLDERS_LIB)
        assert libs is not None and url is None

    def test_no_tool_libraries_answers_nothing_at_all(self, monkeypatch):
        import adsk.cam as _c
        monkeypatch.setattr(_c.CAMManager, "get", lambda: SimpleNamespace(
            libraryManager=SimpleNamespace(toolLibraries=None)))
        assert ct._fusion360_child(ct._HOLDERS_LIB) == (None, None)

    def test_an_unreadable_library_manager_leaves_the_type_vocabulary_empty(self, monkeypatch):
        import adsk.cam as _c
        monkeypatch.setattr(ct, "_type_map_cache", None)
        monkeypatch.setattr(_c.CAMManager, "get", lambda: SimpleNamespace(
            libraryManager=SimpleNamespace(toolLibraries=None)))
        assert ct._build_type_map() == {}
        res = ct.handler(action="list_types")
        assert res["isError"] is True and "sample tool libraries" in res["message"]


# ── _holder_json: the {library_url, index} holder reference ─────────────────────────────────────

class TestHolderJson:
    def test_a_holder_that_is_not_an_object_is_refused(self):
        hd, err = ct._holder_json("Holders/3")
        assert hd is None and "must be {library_url, index}" in err

    def test_a_holder_without_an_index_is_refused(self):
        hd, err = ct._holder_json({"library_url": "u"})
        assert hd is None and "needs an 'index'" in err

    def test_no_library_url_falls_back_to_the_default_holders_library(self, monkeypatch):
        asked = []
        monkeypatch.setattr(ct, "_fusion360_child",
                            lambda leaf: asked.append(leaf) or (None, _AssetURL(leaf)))
        monkeypatch.setattr(ct, "_source_tool", lambda url, idx: (_Tool("CT40"), None))
        hd, err = ct._holder_json({"index": 2})
        # the leaf name is written out LITERALLY, not read back off _HOLDERS_LIB: an assertion
        # built from the same constant the code asked with cannot fail when that constant is
        # rewritten, and the string is matched against Fusion's own sample-library leaf names,
        # so a typo here resolves to nothing live while the suite stays green.
        assert err is None and asked == ["Holders (Metric)"]
        assert hd == {"description": "stock holder", "segments": []}

    def test_no_default_holders_library_asks_for_an_explicit_one(self, monkeypatch):
        monkeypatch.setattr(ct, "_fusion360_child", lambda leaf: (None, None))
        hd, err = ct._holder_json({"index": 0})
        assert hd is None and "Default holders library not found" in err

    def test_a_holder_reference_that_does_not_resolve_is_reported_verbatim(self, monkeypatch):
        monkeypatch.setattr(ct, "_source_tool", lambda url, idx: (None, "tool_index 9 out of range"))
        hd, err = ct._holder_json({"library_url": "u", "index": 9})
        assert hd is None and err == "tool_index 9 out of range"

    def test_a_holders_library_item_is_used_whole(self, monkeypatch):
        # a Holders-library item IS a holder doc (type='holder', carries 'segments'), so there is no
        # 'holder' sub-key to descend into - taking one would drop the whole holder
        doc = {"type": "holder", "description": "CT40", "segments": [1, 2]}
        monkeypatch.setattr(ct, "_source_tool", lambda url, idx:
                            (SimpleNamespace(toJson=lambda: json.dumps(doc)), None))
        hd, err = ct._holder_json({"library_url": "u", "index": 0})
        assert err is None and hd == doc

    def test_json_that_is_not_an_object_is_refused(self, monkeypatch):
        monkeypatch.setattr(ct, "_source_tool", lambda url, idx:
                            (SimpleNamespace(toJson=lambda: "[]"), None))
        hd, err = ct._holder_json({"library_url": "u", "index": 0})
        assert hd is None and "Could not read holder JSON" in err


# ── tool_number: reading it, and the two ways assigning one fails ───────────────────────────────

class TestToolNumberAssignment:
    def test_a_tool_without_the_parameter_has_no_number(self):
        t = _Tool("EM")
        _drop(t.parameters, "tool_number")
        assert ct._read_tool_number(t) is None

    def test_a_number_that_is_not_an_integer_reads_as_none(self):
        # never a coerced 0 - an unreadable number must not look like tool 0 to the free-number walk
        assert ct._read_tool_number(_Tool("EM", tool_number="T7")) is None

    def test_a_locked_tool_number_aborts_the_add(self, monkeypatch):
        tgt = _install(monkeypatch)

        def _from_json(js):
            t = _Tool(json.loads(js).get("description", "built"))
            _replace(t.parameters, _WriteProtected("tool_number", "0"))
            return t

        monkeypatch.setattr(ct, "_tool_from_json", _from_json)
        res = ct.handler(action="add", scope="cloud", library="L", add_tools=[{"from_type": "drill"}])
        assert res["isError"] is True and "Could not set tool_number to 1" in res["message"]
        assert len(tgt.tools) == 2 and tgt.persisted == 0

    def test_a_tool_number_that_does_not_land_aborts_the_add(self, monkeypatch):
        # the assignment is accepted and the parameter still evaluates to something else
        tgt = _install(monkeypatch)

        def _from_json(js):
            t = _Tool(json.loads(js).get("description", "built"))
            _replace(t.parameters, _StuckParam("tool_number", "0"))
            return t

        monkeypatch.setattr(ct, "_tool_from_json", _from_json)
        res = ct.handler(action="add", scope="cloud", library="L", add_tools=[{"from_type": "drill"}])
        assert res["isError"] is True and "did not land" in res["message"]
        assert "read back 0" in res["message"]
        assert len(tgt.tools) == 2 and tgt.persisted == 0

    def test_a_number_the_add_itself_moves_bites(self, monkeypatch):
        # the assignment stuck at set time and the library COUNT is right - only re-reading the
        # in-memory tools after the add shows the number is no longer the assigned one
        tgt = _install(monkeypatch)
        library_add = tgt.add

        def _renumbering_add(t):
            t.parameters.itemByName("tool_number").expression = "42"
            library_add(t)

        tgt.add = _renumbering_add
        res = ct.handler(action="add", scope="cloud", library="L", add_tools=[{"from_type": "drill"}])
        assert res["isError"] is True and "did not persist" in res["message"]
        assert "[1]" in res["message"] and "[42]" in res["message"]


# ── list/parameters read shapes: a non-scalar value, and a tool carrying no holder ──────────────

class TestReadShapes:
    def test_a_non_scalar_parameter_value_is_reported_as_text_never_dropped(self, monkeypatch):
        tool = _Tool("EM")
        _replace(tool.parameters, SimpleNamespace(
            name="tool_coolant", expression="flood",
            value=SimpleNamespace(value=("flood", "mist"))))
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        out = _payload(ct.handler(action="parameters", scope="document", tool=0))
        row = next(r for r in out["parameters"] if r["name"] == "tool_coolant")
        assert row["value"] == "('flood', 'mist')"

    def test_a_tool_carrying_no_holder_publishes_no_holder_field(self, monkeypatch):
        # holder is present-only: a null 'holder' would read as a holder the tool does not have
        tool = _Tool("EM")
        tool.toJson = lambda: json.dumps({"description": "EM", "type": "x"})
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        out = _payload(ct.handler(action="list", scope="document"))
        assert "holder" not in out["tools"][0]


# ── preset value plumbing: the resolver's empty case, and the two set-then-read failures ────────

class TestPresetValuePlumbing:
    def test_a_preset_exposing_no_parameters_collection(self):
        p, avail = _feed_param(SimpleNamespace(parameters=None))
        assert p is None and avail == []

    def test_a_boolean_is_never_a_plain_number(self):
        # the float is taken off str(value), so True reads as 'True' and refuses rather than as the
        # 1.0 float(True) gives - and _set_preset_param checks a read-back against this number
        assert cp._plain_number(True) is None and cp._plain_number(False) is None
        assert cp._plain_number("900") == 900.0
        assert cp._plain_number("35in/min") is None      # units carried, not a plain number

    def test_a_preset_parameter_that_refuses_the_write_rolls_back(self, monkeypatch):
        tool = _preset_param_tool(_WriteProtected, "tool_feedCutting")
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0,
                         preset={"name": "Alu", "feed": 900})
        assert res["isError"] is True and "Could not set 'feed' = 900" in res["message"]
        assert tool.presets.count == 0
        assert tgt.updated == [] and tgt.persisted == 0

    def test_a_preset_value_that_stores_nothing_numeric_rolls_back(self, monkeypatch):
        tool = _preset_param_tool(_TextValued, "tool_spindleSpeed")
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0,
                         preset={"name": "Alu", "spindle_speed": 12000})
        assert res["isError"] is True and "no numeric value was stored" in res["message"]
        assert tool.presets.count == 0
        assert tgt.updated == [] and tgt.persisted == 0

    def test_a_preset_name_the_platform_refuses_rolls_back(self, monkeypatch):
        tool = _tool_with_presets("EM")
        presets = tool.presets
        presets.add = lambda: (presets._items.append(_Unnameable("", tool.preset_params))
                               or presets._items[-1])
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True
        assert "Could not set the preset name to 'Alu'" in res["message"]
        assert tool.presets.count == 0
        assert tgt.updated == [] and tgt.persisted == 0


# ── _build_entry: the per-entry guards on the add path ──────────────────────────────────────────

class TestBuildEntryGuards:
    def test_an_entry_that_is_not_an_object(self, monkeypatch):
        tgt = _install(monkeypatch)
        res = ct.handler(action="add", scope="cloud", library="L", add_tools=["drill"])
        assert res["isError"] is True and "must be an object" in res["message"]
        assert len(tgt.tools) == 2 and tgt.persisted == 0

    def test_a_holder_reference_that_does_not_resolve_aborts_the_entry(self, monkeypatch):
        tgt = _install(monkeypatch)
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill", "holder": {"library_url": "h"}}])
        assert res["isError"] is True and "bad holder ref" in res["message"]
        assert len(tgt.tools) == 2 and tgt.persisted == 0

    def test_a_source_tool_whose_json_is_not_an_object(self, monkeypatch):
        tgt = _install(monkeypatch)
        monkeypatch.setattr(ct, "_sample_for_type",
                            lambda ty: (SimpleNamespace(toJson=lambda: "[]"), None))
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill"}])
        assert res["isError"] is True and "Could not read the source tool's JSON" in res["message"]
        assert len(tgt.tools) == 2

    def test_a_tool_the_factory_cannot_build(self, monkeypatch):
        tgt = _install(monkeypatch)
        monkeypatch.setattr(ct, "_tool_from_json", lambda js: None)
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill"}])
        assert res["isError"] is True and "Could not create the tool from JSON" in res["message"]
        assert len(tgt.tools) == 2

    def test_a_product_id_the_parameter_refuses(self, monkeypatch):
        tgt = _install(monkeypatch)

        def _from_json(js):
            t = _Tool(json.loads(js).get("description", "built"))
            _replace(t.parameters, _WriteProtected("tool_productId", ""))
            return t

        monkeypatch.setattr(ct, "_tool_from_json", _from_json)
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill", "product_id": "HAM-123"}])
        assert res["isError"] is True and "Could not set tool_productId" in res["message"]
        assert len(tgt.tools) == 2

    def test_a_vendor_expression_that_never_evaluates(self, monkeypatch):
        # the quoted-string expression is stored verbatim and reads back fine; only .error reveals
        # that it never evaluated
        tgt = _install(monkeypatch)

        def _from_json(js):
            t = _Tool(json.loads(js).get("description", "built"))
            _replace(t.parameters, _BrokenParam("tool_vendor", ""))
            return t

        monkeypatch.setattr(ct, "_tool_from_json", _from_json)
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "drill", "vendor": "Hoffmann"}])
        assert res["isError"] is True
        assert "tool_vendor" in res["message"] and "failed to evaluate" in res["message"]
        assert len(tgt.tools) == 2

    def test_a_creation_time_preset_value_with_nowhere_to_go_aborts_the_add(self, monkeypatch):
        # a turning tool's presets carry tool_surfaceSpeed and no spindle speed at all
        tgt = _install(monkeypatch)
        monkeypatch.setattr(ct, "_sample_for_type",
                            lambda ty: (_Tool("sample-" + ty, tool_type=ty), None))
        monkeypatch.setattr(ct, "_tool_from_json",
                            lambda js: _Tool(json.loads(js).get("description", "built"),
                                             preset_params=_TURNING_CUTTING_DATA))
        res = ct.handler(action="add", scope="cloud", library="L",
                         add_tools=[{"from_type": "turning general",
                                     "presets": [{"name": "Steel", "spindle_speed": 400}]}])
        assert res["isError"] is True and "tool_surfaceSpeed" in res["message"]
        assert len(tgt.tools) == 2 and tgt.persisted == 0


# ── remove / edit / where_used guards ───────────────────────────────────────────────────────────

class TestRemoveGuards:
    def test_remove_requires_indices(self, monkeypatch):
        tgt = _install(monkeypatch)
        res = ct.handler(action="remove", scope="local", library="L")
        assert res["isError"] is True and "Provide 'remove_indices'" in res["message"]
        assert len(tgt.tools) == 2 and tgt.persisted == 0

    def test_a_document_removal_commits_without_a_library_persist(self, monkeypatch):
        tgt = _install(monkeypatch, _Target(tools=[_Tool("A"), _Tool("B")], is_document=True))
        out = _payload(ct.handler(action="remove", scope="document", remove_indices=[0]))
        assert [t.desc for t in tgt.tools] == ["B"] and out["removed"] == 1
        assert tgt.persisted == 0        # the document library has no url to persist to

    def test_a_removal_whose_url_reread_disagrees_bites(self, monkeypatch):
        tgt = _Target(tools=[_Tool("A"), _Tool("B")], persisted_count_value=2)
        _install(monkeypatch, target=tgt)
        res = ct.handler(action="remove", scope="local", library="L", remove_indices=[0])
        assert res["isError"] is True and "did not land" in res["message"]
        assert "holds 2 tool(s), not 1" in res["message"]


class TestEditGuards:
    def test_edit_needs_a_valid_tool_index(self, monkeypatch):
        _install(monkeypatch, _Target(tools=[_Tool("only")], is_document=True))
        res = ct.handler(action="edit", scope="document", tool=3,
                         parameters={"tool_diameter": "6 mm"})
        assert res["isError"] is True and "0..0" in res["message"]

    def test_edit_needs_parameters(self, monkeypatch):
        tgt = _install(monkeypatch, _Target(tools=[_Tool("only")], is_document=True))
        res = ct.handler(action="edit", scope="document", tool=0)
        assert res["isError"] is True and "Provide 'parameters'" in res["message"]
        assert tgt.updated == []


class TestWhereUsedGuards:
    def test_where_used_needs_a_valid_tool_index(self, monkeypatch):
        _install(monkeypatch, _Target(tools=[_Tool("EM")], is_document=True))
        res = ct.handler(action="where_used", scope="document", tool=9)
        assert res["isError"] is True and "0..0" in res["message"]


# ── add_preset: the tool with no presets, and the two rollback paths ────────────────────────────

class TestAddPresetRollback:
    def test_a_tool_exposing_no_presets_collection_is_refused(self, monkeypatch):
        tool = _Tool("EM")
        tool.presets = None
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True and "exposes no presets collection" in res["message"]

    def test_a_preset_the_tool_never_creates_is_an_error(self, monkeypatch):
        tool = _tool_with_presets("EM")
        tool.presets.add = lambda: None
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))
        res = ct.handler(action="add_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True and "none was created" in res["message"]
        assert tgt.updated == [] and tgt.persisted == 0

    def test_a_raising_value_applier_rolls_the_preset_back(self, monkeypatch):
        # add() has already appended the preset, so ANY failure in the applier - not just a returned
        # error string - has to drop it again rather than leave a half-populated preset on the tool
        tool = _tool_with_presets("EM")
        tgt = _install(monkeypatch, _Target(tools=[tool], is_document=True))

        def _boom(preset, spec):
            raise RuntimeError("preset store offline")

        monkeypatch.setattr(ct, "_apply_preset_values", _boom)
        res = ct.handler(action="add_preset", scope="document", tool=0, preset={"name": "Alu"})
        assert res["isError"] is True
        assert "Could not populate the new preset 'Alu'" in res["message"]
        assert "preset store offline" in res["message"]
        assert tool.presets.count == 0
        assert tgt.updated == [] and tgt.persisted == 0

    def test_a_rollback_the_tool_refuses_is_disclosed(self, monkeypatch):
        # the half-populated preset really is still on the tool - the error says so instead of
        # reporting only the value failure
        tool = _tool_with_presets("EM")
        tool.presets.remove = lambda index: False
        _install(monkeypatch, _Target(tools=[tool], is_document=True))
        monkeypatch.setattr(ct, "_apply_preset_values", lambda p, s: "no room for 'feed'.")
        res = ct.handler(action="add_preset", scope="document", tool=0,
                         preset={"name": "Alu", "feed": 900})
        assert res["isError"] is True
        assert "no room for 'feed'." in res["message"]
        assert "could not be removed again" in res["message"]
        assert tool.presets.count == 1


# ── create_library guards ───────────────────────────────────────────────────────────────────────

class TestCreateLibraryGuards:
    def test_no_tool_libraries(self, monkeypatch):
        _install_create(monkeypatch)
        monkeypatch.setattr(ct, "_tool_libraries", lambda: None)
        res = ct.handler(action="create_library", scope="local", library="X")
        assert res["isError"] is True and "unavailable" in res["message"]

    def test_a_root_that_does_not_resolve(self, monkeypatch):
        libs = _install_create(monkeypatch)
        libs.urlByLocation = lambda loc: None
        res = ct.handler(action="create_library", scope="cloud", library="X")
        assert res["isError"] is True
        assert "Could not resolve the 'cloud' library root" in res["message"]
        assert len(libs.imported) == 0

    def test_a_hub_without_a_team_folder(self, monkeypatch):
        # hub can't import at the bare hub:// root, so with no folder to descend into there is
        # nowhere to create the library
        libs = _install_create(monkeypatch)
        libs.childFolderURLs = lambda url: []
        res = ct.handler(action="create_library", scope="hub", library="X")
        assert res["isError"] is True and "No hub folder" in res["message"]
        assert len(libs.imported) == 0

    def test_a_seed_that_is_not_an_object(self, monkeypatch):
        libs = _install_create(monkeypatch)
        res = ct.handler(action="create_library", scope="local", library="X", add_tools=["u:0"])
        assert res["isError"] is True and "Each seed entry must be" in res["message"]
        assert len(libs.imported) == 0

    def test_an_empty_library_that_cannot_be_created(self, monkeypatch):
        libs = _install_create(monkeypatch)
        monkeypatch.setattr(ct, "_empty_library", lambda: None)
        res = ct.handler(action="create_library", scope="local", library="X")
        assert res["isError"] is True and "Could not create an empty tool library" in res["message"]
        assert len(libs.imported) == 0

    def test_an_import_that_raises_reports_the_platform_message(self, monkeypatch):
        libs = _install_create(monkeypatch)

        def _boom(lib, dest, name):
            raise RuntimeError("disk full")

        libs.importToolLibrary = _boom
        res = ct.handler(action="create_library", scope="local", library="X")
        assert res["isError"] is True and "disk full" in res["message"]
        assert "UI" not in res["message"]          # the hub-only hint stays off the local path

    def test_a_failed_hub_import_points_at_the_ui(self, monkeypatch):
        # hub team libraries use a write path importToolLibrary does not satisfy
        libs = _install_create(monkeypatch)

        def _boom(lib, dest, name):
            raise RuntimeError("access denied")

        libs.importToolLibrary = _boom
        res = ct.handler(action="create_library", scope="hub", library="X")
        assert res["isError"] is True and "create Hub libraries in the UI" in res["message"]

    def test_an_import_that_returns_no_url(self, monkeypatch):
        libs = _install_create(monkeypatch)
        libs.importToolLibrary = lambda lib, dest, name: None
        res = ct.handler(action="create_library", scope="cloud", library="X")
        assert res["isError"] is True and "returned no URL" in res["message"]


_READ_ACTIONS = ("list", "list_types", "parameters", "where_used")


class TestFusionScopeIsReadOnly:
    """scope='fusion' reaches the libraries the installation ships - where the sample clones already
    come from - so they can be listed and read. Every write action is refused there."""

    def test_the_scope_reads_the_fusion360_library_location(self, monkeypatch):
        import adsk.cam as _c
        seen = []

        def _by_location(loc):
            seen.append(loc)
            return _AssetURL("root")

        libs = SimpleNamespace(urlByLocation=_by_location,
                               childAssetURLs=lambda u: [_AssetURL("Probing Tools (Metric)")],
                               childFolderURLs=lambda u: [])
        monkeypatch.setattr(ct, "_tool_libraries", lambda: libs)
        entries, truncated, err = ct._shared_libraries("fusion")
        assert err is None and truncated is False
        assert seen == [_c.LibraryLocations.Fusion360LibraryLocation]
        assert [e["name"] for e in entries] == ["Probing Tools (Metric)"]

    def test_the_write_list_partitions_the_action_vocabulary(self):
        # a write action left out of the list would reach the shipped libraries unguarded
        assert set(ct._WRITE_ACTIONS) | set(_READ_ACTIONS) == set(ct._ACTIONS)
        assert not set(ct._WRITE_ACTIONS) & set(_READ_ACTIONS)

    @pytest.mark.parametrize("action", ct._WRITE_ACTIONS)
    def test_every_write_action_is_refused_at_the_fusion_scope(self, action, monkeypatch):
        # THE BITE: _resolve_target is patched to hand back a live target, so a missing guard lets
        # each of these write to the shipped library instead of refusing.
        _install(monkeypatch)
        res = ct.handler(action=action, scope="fusion", library="Milling Tools (Metric)",
                         add_tools=[{"from_type": "drill"}], remove_indices=[0], tool=0,
                         parameters={"tool_numberOfFlutes": "4"}, preset={"name": "Alu"})
        assert res["isError"] is True
        assert "only READS" in res["message"] and f"'{action}'" in res["message"]
        assert "scope='document'" in res["message"]

    def test_list_and_parameters_still_read_at_the_fusion_scope(self, monkeypatch):
        _install(monkeypatch)
        out = _payload(ct.handler(action="list", scope="fusion", library="Milling Tools (Metric)"))
        assert out["tool_count"] == 2
        params = _payload(ct.handler(action="parameters", scope="fusion",
                                     library="Milling Tools (Metric)", tool=0))
        assert params["tool"] == 0 and params["parameter_count"] > 0


# ── read_library: the READ half cam_get(include=['library']) shares ─────────────────────────────

class TestReadLibraryEntryPoint:
    def test_read_library_refuses_an_unknown_scope(self):
        res = ct.read_library(scope="moon")
        assert res["isError"] is True and "Unknown scope 'moon'" in res["message"]

    def test_read_library_lists_the_libraries_when_a_shared_scope_names_none(self, monkeypatch):
        monkeypatch.setattr(ct, "_shared_libraries",
                            lambda scope: ([{"name": "Team Mill", "url": "u"}], False, None))
        out = _payload(ct.read_library(scope="hub"))
        assert out["scope"] == "hub" and out["library_count"] == 1
