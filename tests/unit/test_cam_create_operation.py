"""Unit tests for ``cam_create_operation`` — apply a CAM milling operation.

The adsk.cam API is mocked; what we pin is the tool's OWN logic: resolving the target setup by name,
validating the strategy against the setup's compatibleStrategies (by .name), fetching the tool from a
library by (library_url, index) - the reference handle cam_get(include=['library']) produces -
assigning it to the OperationInput, adding the operation, and (optionally) generating the toolpath.
Plus the guards (no CAM, setup not found, bad strategy, tool ref out of range).
"""

import json

from conftest import (FakeOperation, FakeSetup, FakeTool, _NamedCollection, _Strategy as _Entitled,
                      load_tool, make_cam, make_cam_parameters)

cco = load_tool("cam_create_operation")
_cam = load_tool("_cam_common")


# ── fakes mirroring the proven adsk.cam create path ─────────────────────────

# The classification flag names an OperationStrategy really carries, from the measured member list
# in api_surface.py. The fake spells them exactly, so a reader reaching for a short form (is2D)
# reads nothing here just as it would live.
_FLAG_PROPS = ("is2DStrategy", "is3DStrategy", "isDrillingStrategy", "isMillingStrategy",
               "isRotaryStrategy", "isTurningStrategy", "isFinishingStrategy",
               "isAdditiveStrategy", "isCuttingStrategy", "isSupportStrategy", "isSuppressible")


class _Strategy(_Entitled):
    """An OperationStrategy: a name, a title, the shared fake's isGenerationAllowed entitlement flag
    (allowed=None makes the flag itself RAISE) and the classification flags. A flag not named here
    answers False, as an unset one does live."""

    def __init__(self, name, allowed=True, title=None, **flags):
        assert not set(flags) - set(_FLAG_PROPS), (
            f"not OperationStrategy members: {sorted(set(flags) - set(_FLAG_PROPS))}")
        super().__init__(allowed)
        self.name = name
        self.title = title if title is not None else (name or "").replace("_", " ").title()
        for prop in _FLAG_PROPS:
            setattr(self, prop, flags.get(prop, False))


class _StrategyVector:
    """OperationStrategyVector: a RAW std::vector binding - len() and [i] answer, and .count /
    .item() do not exist at all. A walk that reaches for either gets AttributeError here, exactly as
    it would live, so a reader built on _common.iter_collection reads an empty vocabulary."""

    def __init__(self, items):
        self._i = [_Strategy(s) if isinstance(s, str) else s for s in items]

    def __len__(self):
        return len(self._i)

    def __getitem__(self, i):
        return self._i[i]


class _OperationInput:
    """OperationInput: strategy, tool, the generationMode the create assigns, and the displayName
    the new operation is born under. Both arrive unset."""

    def __init__(self, strategy):
        self.strategy = strategy
        self.tool = None
        self.generationMode = None
        self.displayName = None


class _Operation(FakeOperation):
    """An added Operation: it is born under the input's displayName when one was carried, else the
    platform's own auto-name."""

    def __init__(self, inp):
        super().__init__(getattr(inp, "displayName", None) or "Op1", has_toolpath=False, valid=False,
                         strategy=inp.strategy, tool=inp.tool)


# The two Deaf shapes below are a setter that accepts the value and keeps the default, so nothing
# raises; the two Raising ones are a member whose setter refuses outright.


class _DeafModeInput:
    """An OperationInput whose generationMode assignment silently no-ops, keeping its default."""

    def __init__(self, strategy):
        self.strategy = strategy
        self.tool = None
        self.displayName = None

    @property
    def generationMode(self):
        return None

    @generationMode.setter
    def generationMode(self, value):
        pass


class _RaisingModeInput:
    """An OperationInput whose generationMode reads and assigns by raising."""

    def __init__(self, strategy):
        self.strategy = strategy
        self.tool = None
        self.displayName = None

    @property
    def generationMode(self):
        raise AttributeError("'OperationInput' object has no attribute 'generationMode'")

    @generationMode.setter
    def generationMode(self, value):
        raise AttributeError("'OperationInput' object has no attribute 'generationMode'")


class _DeafDisplayNameInput:
    """An OperationInput whose displayName assignment silently no-ops, keeping its default."""

    def __init__(self, strategy):
        self.strategy = strategy
        self.tool = None
        self.generationMode = None

    @property
    def displayName(self):
        return None

    @displayName.setter
    def displayName(self, value):
        pass


class _RaisingDisplayNameInput:
    """An OperationInput whose displayName reads and assigns by raising."""

    def __init__(self, strategy):
        self.strategy = strategy
        self.tool = None
        self.generationMode = None

    @property
    def displayName(self):
        raise AttributeError("'OperationInput' object has no attribute 'displayName'")

    @displayName.setter
    def displayName(self, value):
        raise AttributeError("'OperationInput' object has no attribute 'displayName'")


class _ToollessOperation(_Operation):
    """The add lands an operation carrying NO tool - the OperationInput assignment never reached it."""

    def __init__(self, inp):
        super().__init__(inp)
        self.tool = None


class _SwappedToolOperation(_Operation):
    """The add lands an operation carrying a DIFFERENT tool than the input was given."""

    def __init__(self, inp):
        super().__init__(inp)
        self.tool = _tool("6mm Ball Endmill", number=7)


class _PrefixedToolOperation(_Operation):
    """The operation's own COPY of the tool, whose description carries the '#<n> - ' number prefix
    the shared library's tool does not."""

    def __init__(self, inp):
        super().__init__(inp)
        self.tool = _tool("#1 - " + inp.tool.description, number=1)


class _Operations(_NamedCollection):
    op_class = _Operation                     # swapped by a test that needs a different Operation

    def __init__(self, strategies):
        super().__init__()
        # the same list the shared collection counts and items: what add() appends, a walk reads
        self.added = self._items
        self._offered = list(strategies)
        # the generationMode each input carried AT the add
        self.modes_at_add = []
        # the displayName each input carried AT the add - the name the operation is born under
        self.names_at_add = []

    @property
    def compatibleStrategies(self):
        return _StrategyVector(self._offered)

    def createInput(self, strategy):
        if strategy not in [s.name for s in self.compatibleStrategies]:
            raise RuntimeError("invalid strategy")
        return _OperationInput(strategy)
    def add(self, inp):
        self.modes_at_add.append(getattr(inp, "generationMode", None))
        self.names_at_add.append(getattr(inp, "displayName", None))
        op = self.op_class(inp)
        self.added.append(op)
        return op


class _Setup(FakeSetup):
    def __init__(self, name, strategies):
        super().__init__(name)
        self.operations = _Operations(strategies)


def _tool(desc, number=1, tool_type="flat end mill"):
    """A library Tool: the description, tool_number and tool_type the create reads it by (a
    tool_type of None is the parameter the tool does not carry at all)."""
    rows = [("tool_number", "", number)]
    if tool_type is not None:
        rows.append(("tool_type", "", tool_type))
    return FakeTool(description=desc, parameters=make_cam_parameters(*rows))


def _make_cam(setups, strategies, doc_tools):
    """A CAM product whose setups carry `strategies`, plus the document tool library and the
    generateToolpath launch (its future is kept so a test can assert THIS one was registered)."""
    cam = make_cam(*[_Setup(n, strategies) for n in setups])
    cam.documentToolLibrary = _NamedCollection(list(doc_tools))   # this doc's tools
    cam.generated = []
    cam.futures = []

    def _generate(op):
        op.hasToolpath = True
        op.isToolpathValid = True
        cam.generated.append(op)
        fut = type("Fut", (), {"numberOfOperations": 1})()
        cam.futures.append(fut)
        return fut

    cam.generateToolpath = _generate
    return cam


def _install(monkeypatch, setups=("Setup1",), tools=2, doc_tools=(),
             strategies=("face", "adaptive", "drill", "bore")):
    cam = _make_cam(list(setups), strategies, doc_tools)
    monkeypatch.setattr(cco, "get_cam", lambda: (cam, None))
    # tool-by-reference resolver: (library_url, index) -> Tool, mirrors cam_edit_tools's shared handle
    lib = _NamedCollection([_tool("12mm Flat Endmill"), _tool("6mm Ball Endmill")][:tools])
    cco._tool_at = lambda url, idx: (lib.item(idx) if 0 <= idx < lib.count else None,
                                     None if 0 <= idx < lib.count else "tool index %d out of range" % idx)
    # the document-library path is NOT patched — it runs the real _doc_tool_at against cam.documentToolLibrary
    return cam


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_no_cam(self, monkeypatch):
        monkeypatch.setattr(cco, "get_cam", lambda: (None, "no CAM data"))
        res = cco.handler(setup="Setup1", strategy="face",
                          tool_library_url="u", tool_index=0)
        assert res["isError"] is True and "cam" in res["message"].lower()

    def test_setup_not_found(self, monkeypatch):
        _install(monkeypatch, setups=("Setup1",))
        res = cco.handler(setup="Ghost", strategy="face",
                          tool_library_url="u", tool_index=0)
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_bad_strategy(self, monkeypatch):
        _install(monkeypatch)
        res = cco.handler(setup="Setup1", strategy="frobnicate",
                          tool_library_url="u", tool_index=0)
        # the pre-check should name the bad strategy AND list the compatible ones (not just fail at createInput)
        assert res["isError"] is True and "frobnicate" in res["message"]
        assert "compatible" in res["message"].lower() and "face" in res["message"]

    def test_tool_ref_out_of_range(self, monkeypatch):
        _install(monkeypatch, tools=2)
        res = cco.handler(setup="Setup1", strategy="face",
                          tool_library_url="u", tool_index=9)
        assert res["isError"] is True and "range" in res["message"].lower()

    def test_missing_tool_ref(self, monkeypatch):
        _install(monkeypatch)
        res = cco.handler(setup="Setup1", strategy="face")
        assert res["isError"] is True and "tool" in res["message"].lower()


class TestToolIndexRequest:
    """A 'tool_index' the wire can deliver as text, as a bool, or not at all. It is read to an int
    before any ORDER comparison, and an ABSENT index is told apart from an unusable one."""

    def test_a_boolean_is_not_an_index(self):
        # bool is an int in Python, so True would otherwise select the SECOND library tool
        assert cco.tool_index_of(True) is None and cco.tool_index_of(False) is None

    def test_text_and_numbers_read_as_indexes_and_junk_does_not(self):
        assert cco.tool_index_of("0") == 0 and cco.tool_index_of(" 3 ") == 3
        assert cco.tool_index_of(2) == 2
        assert cco.tool_index_of("first") is None and cco.tool_index_of(None) is None

    def test_absent_asks_for_an_index_and_present_junk_names_it(self):
        assert "Provide 'tool_index'" in cco.index_request_error(None)
        assert "Provide 'tool_index'" in cco.index_request_error(-1)      # the schema default
        assert "'first'" in cco.index_request_error("first")
        assert "True" in cco.index_request_error(True)
        assert cco.index_request_error(0) is None and cco.index_request_error("2") is None

    def test_a_boolean_index_is_refused_before_any_tool_is_fetched(self, monkeypatch):
        cam = _install(monkeypatch, tools=2)
        res = cco.handler(setup="Setup1", strategy="face", tool_library_url="u", tool_index=True)
        assert res["isError"] is True and "not a whole number" in res["message"]
        assert cam.setups.item(0).operations.count == 0            # nothing was created


class TestProbeStrategyNeedsAProbe:
    """A probing strategy takes ANY tool at create and only reports at generate, so the tool_type
    is read here and a cutting tool is refused before the add."""

    _STRATEGIES = ("face", "probe", "probe_geometry", "inspect_surface")

    def _with_tool(self, monkeypatch, tool):
        cam = _install(monkeypatch, strategies=self._STRATEGIES)
        monkeypatch.setattr(cco, "_tool_at", lambda url, idx: (tool, None))
        return cam

    def test_a_cutting_tool_on_a_probing_strategy_is_refused_before_the_add(self, monkeypatch):
        cam = self._with_tool(monkeypatch, _tool("50mm Face Mill", tool_type="face mill"))
        res = cco.handler(setup="Setup1", strategy="probe",
                          tool_library_url="u", tool_index=0)
        assert res["isError"] is True
        assert "'face mill'" in res["message"]                 # the type actually read
        assert "not supported for the strategy" in res["message"]
        assert "'from_type': 'probe'" in res["message"]        # the clone remedy
        assert "scope='fusion'" in res["message"]              # the shipped-library remedy
        assert cam.setups.item(0).operations.count == 0        # nothing was created

    def test_a_probe_creates_on_a_probing_strategy(self, monkeypatch):
        cam = self._with_tool(monkeypatch, _tool("OMP400", tool_type="probe"))
        out = _payload(cco.handler(setup="Setup1", strategy="probe",
                                   tool_library_url="u", tool_index=0))
        assert out["tool"] == "OMP400"
        assert cam.setups.item(0).operations.count == 1

    def test_every_probing_strategy_is_gated_and_a_cutting_one_is_not(self, monkeypatch):
        # THE BOUNDARY: the gate is keyed on the STRATEGY, so a face mill still creates a 'face'.
        for strategy in ("probe", "probe_geometry", "inspect_surface"):
            self._with_tool(monkeypatch, _tool("50mm Face Mill", tool_type="face mill"))
            res = cco.handler(setup="Setup1", strategy=strategy,
                              tool_library_url="u", tool_index=0)
            assert res["isError"] is True, strategy
        cam = self._with_tool(monkeypatch, _tool("50mm Face Mill", tool_type="face mill"))
        _payload(cco.handler(setup="Setup1", strategy="face", tool_library_url="u", tool_index=0))
        assert cam.setups.item(0).operations.count == 1

    def test_inspect_surface_says_its_points_are_ui_only(self, monkeypatch):
        # inspectSurfacePositions takes no write through the API, so an agent that created the
        # operation and waited for a points call would wait forever - the note says so once, and
        # only for that strategy.
        self._with_tool(monkeypatch, _tool("OMP400", tool_type="probe"))
        out = _payload(cco.handler(setup="Setup1", strategy="inspect_surface",
                                   tool_library_url="u", tool_index=0))
        assert "UI-only" in out["note"] and "inspectSurfacePositions" in out["note"]
        self._with_tool(monkeypatch, _tool("OMP400", tool_type="probe"))
        other = _payload(cco.handler(setup="Setup1", strategy="probe",
                                     tool_library_url="u", tool_index=0))
        assert "inspectSurfacePositions" not in other["note"]

    def test_a_tool_type_that_does_not_read_refuses_nothing(self, monkeypatch):
        # an unread type is no verdict - refusing on it would block a create off a read that
        # never answered.
        cam = self._with_tool(monkeypatch, _tool("Mystery", tool_type=None))
        _payload(cco.handler(setup="Setup1", strategy="probe", tool_library_url="u", tool_index=0))
        assert cam.setups.item(0).operations.count == 1


class TestToolReadBack:
    """opin.tool is the REQUEST. What the payload publishes is Operation.tool read off the
    operation the add returned, and a read-back that names another tool - or none - is an error."""

    def test_the_payload_publishes_the_tool_the_operation_carries(self, monkeypatch):
        _install(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_library_url="u", tool_index=0))
        assert out["tool"] == "12mm Flat Endmill" and out["tool_number"] == 1
        assert "tool_identity_checked" not in out      # absent = both sides read and agreed

    def test_an_add_that_drops_the_tool_is_refused_with_the_read_back(self, monkeypatch):
        # THE BITE: the request echoed would report '12mm Flat Endmill' on an operation carrying
        # nothing, and the caller would learn otherwise only at generate.
        cam = _install(monkeypatch)
        cam.setups.item(0).operations.op_class = _ToollessOperation
        res = cco.handler(setup="Setup1", strategy="face", tool_library_url="u", tool_index=0)
        assert res["isError"] is True
        assert "Operation.tool reads back null" in res["message"]
        assert "cam_edit_operation" in res["message"] and "cam_delete" in res["message"]

    def test_a_read_back_naming_another_tool_is_refused_and_names_both(self, monkeypatch):
        cam = _install(monkeypatch)
        cam.setups.item(0).operations.op_class = _SwappedToolOperation
        res = cco.handler(setup="Setup1", strategy="face", tool_library_url="u", tool_index=0)
        assert res["isError"] is True
        assert "'6mm Ball Endmill'" in res["message"]        # what it carries
        assert "'12mm Flat Endmill'" in res["message"]       # what was asked for

    def test_the_tool_number_prefix_is_stripped_from_both_sides(self, monkeypatch):
        # the operation's COPY carries a '#<n> - ' prefix the shared library's tool does not, so a
        # prefix-blind compare would refuse every create made against a sample library.
        cam = _install(monkeypatch)
        cam.setups.item(0).operations.op_class = _PrefixedToolOperation
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_library_url="u", tool_index=0))
        assert out["tool"] == "#1 - 12mm Flat Endmill"


# ── the operation has to LAND in the setup, not just come back from add() ────
#
# operations.add hands back an Operation object, and that return is not evidence the setup took it.
# The count read either side of the add is what the created=... claim rests on.


class _DeafOperations(_Operations):
    """add() builds the operation and hands it back without the setup ever taking it."""
    def add(self, inp):
        return _Operation(inp)


class _MuteOperations(_Operations):
    """The count cannot be READ at all - the state a gate written as `before is not None and after
    is not None and after <= before` skips entirely, letting an unconfirmed create report ok."""
    @property
    def count(self):
        raise RuntimeError("operations.count is unreadable")


class _NonNumericCount(_Operations):
    """count answers with something that is not a number at all - what an unmodelled property hands
    back. Comparing the two sides of the add against it is not a count comparison."""
    @property
    def count(self):
        return object()


class _NonNumericCountBeforeAdd(_Operations):
    """count answers with something that is not a number BEFORE the add and a real count after it -
    the one-sided shape, which the two sides being read separately makes possible."""
    def __init__(self, strategies):
        super().__init__(strategies)
        self._landed = False

    @property
    def count(self):
        return len(self.added) if self._landed else object()

    def add(self, inp):
        op = super().add(inp)
        self._landed = True
        return op


class _CountStopsAfterAdd(_Operations):
    """count answers BEFORE the add and stops answering after it - only one side of the gate is
    missing, and the message has to name which."""
    def __init__(self, strategies):
        super().__init__(strategies)
        self._mute = False

    @property
    def count(self):
        if self._mute:
            raise RuntimeError("operations.count is unreadable")
        return len(self.added)

    def add(self, inp):
        op = super().add(inp)
        self._mute = True
        return op


def _with_operations(cam, cls):
    setup = cam.setups.item(0)
    setup.operations = cls([s.name for s in setup.operations.compatibleStrategies])
    return setup


class TestOperationLanding:
    def test_an_operation_that_never_lands_in_the_setup_is_an_error(self, monkeypatch):
        cam = _install(monkeypatch)
        setup = _with_operations(cam, _DeafOperations)
        res = cco.handler(setup="Setup1", strategy="face",
                          tool_library_url="u", tool_index=0, generate=False)
        assert res["isError"] is True
        assert "did not land" in res["message"]
        assert setup.operations.count == 0
        # the other side of `ops_after <= ops_before` is TestCreate.test_creates_operation_with_tool,
        # where the same add grows the setup by exactly one and the call succeeds.

    def test_an_unreadable_count_is_unconfirmed_not_a_pass(self, monkeypatch):
        # a count that does not answer cannot CLEAR the landing, so the gate may not be skipped for
        # it - an unlanded operation would report ok on exactly this read.
        cam = _install(monkeypatch)
        _with_operations(cam, _MuteOperations)
        res = cco.handler(setup="Setup1", strategy="face",
                          tool_library_url="u", tool_index=0, generate=False)
        assert res["isError"] is True
        assert "UNCONFIRMED" in res["message"]
        assert "could not be read before and after the add" in res["message"]

    def test_a_count_that_is_not_a_number_is_unconfirmed_too(self, monkeypatch):
        # the count is read through _common.counted, so a non-int answer is UNKNOWN rather than a
        # value to compare: `after <= before` against a bare object either raises or compares
        # something that is not a count at all.
        cam = _install(monkeypatch)
        _with_operations(cam, _NonNumericCount)
        res = cco.handler(setup="Setup1", strategy="face",
                          tool_library_url="u", tool_index=0, generate=False)
        assert res["isError"] is True and "UNCONFIRMED" in res["message"]
        # BOTH sides answered a non-count here, so both are named: a message naming one side is a
        # gate reading only the OTHER side as a count.
        assert "could not be read before and after the add" in res["message"]

    def test_a_non_numeric_count_on_the_BEFORE_side_alone_is_unconfirmed(self, monkeypatch):
        # The two sides are read separately, so each needs its own not-a-number read: a gate that
        # took the before count as whatever answered would compare an int against a bare object.
        cam = _install(monkeypatch)
        _with_operations(cam, _NonNumericCountBeforeAdd)
        res = cco.handler(setup="Setup1", strategy="face",
                          tool_library_url="u", tool_index=0, generate=False)
        assert res["isError"] is True and "UNCONFIRMED" in res["message"]
        assert "could not be read before the add" in res["message"]
        assert "before and after" not in res["message"]

    def test_the_message_names_which_side_of_the_add_went_unread(self, monkeypatch):
        # both counts unreadable and only the after one are different observations; naming the side
        # is what tells the reader whether the before-count was ever taken.
        cam = _install(monkeypatch)
        _with_operations(cam, _CountStopsAfterAdd)
        res = cco.handler(setup="Setup1", strategy="face",
                          tool_library_url="u", tool_index=0, generate=False)
        assert res["isError"] is True
        assert "could not be read after the add" in res["message"]
        assert "before and after" not in res["message"]


# ── create (no generate) ─────────────────────────────────────────────────────

class TestCreate:
    def test_creates_operation_with_tool(self, monkeypatch):
        cam = _install(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_library_url="u", tool_index=0, generate=False))
        op = cam.setups.item(0).operations.item(0)
        assert op.strategy == "face"
        assert op.tool is not None and op.tool.description == "12mm Flat Endmill"
        assert out["operation"] == "Op1" and out["strategy"] == "face"
        assert out["generation_started"] is False
        # not generated -> no toolpath yet
        assert len(cam.generated) == 0

    def test_create_then_generate(self, monkeypatch):
        cam = _install(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="adaptive",
                                   tool_library_url="u", tool_index=1, generate=True))
        assert out["generation_started"] is True
        # generation is ASYNC: hasToolpath read at launch time is stale, so the payload must NOT
        # carry it (a false negative would send the agent chasing a phantom failure)
        assert "has_toolpath" not in out and "toolpath_valid" not in out
        assert len(cam.generated) == 1

    def test_generation_future_is_REGISTERED_not_dropped(self, monkeypatch):
        # Fusion ABANDONS an in-progress generation whose Future is garbage-collected, so the launch
        # must hand it to _cam_common.register_future - the one thing keeping it alive - and return
        # the handle cam_get_status polls. Discarding it reports generation_started with nothing
        # actually generating.
        _cam._GENERATIONS.clear()
        cam = _install(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="adaptive",
                                   tool_library_url="u", tool_index=1, generate=True))
        assert out["generation_started"] is True
        handle = out["generation_handle"]
        entry = _cam._GENERATIONS[handle]
        assert entry["future"] is cam.futures[-1]      # THE launched future, still referenced
        _cam._GENERATIONS.clear()

    def test_a_null_future_is_reported_not_claimed_as_started(self, monkeypatch):
        # generateToolpath returning nothing means no generation is running; saying otherwise sends
        # the agent to post a job whose toolpath was never computed.
        cam = _install(monkeypatch)
        monkeypatch.setattr(cam, "generateToolpath", lambda op: None)
        out = _payload(cco.handler(setup="Setup1", strategy="adaptive",
                                   tool_library_url="u", tool_index=1, generate=True))
        assert out["generation_started"] is False
        assert "no future" in out["generate_error"]

    def test_default_does_not_generate(self, monkeypatch):
        # generate defaults to FALSE: a selection-driven strategy generated before its geometry is
        # selected does not fail - it lands a warned op with no toolpath that still reads valid
        # (measured on a live 2D Contour), so the honest default is to wait for cam_select_geometry.
        cam = _install(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_library_url="u", tool_index=0))
        assert out["generation_started"] is False and cam.generated == []
        assert "generation_handle" not in out

    def test_the_default_note_names_the_order_geometry_then_generate(self, monkeypatch):
        # the note is the only place a caller learns why nothing generated and what comes next.
        _install(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_library_url="u", tool_index=0))
        assert "cam_select_geometry" in out["note"] and "cam_generate" in out["note"]

    def test_the_wire_default_matches_the_handler_default(self, monkeypatch):
        # the schema an agent reads must not promise a different default than the handler applies.
        prop = cco.tool.to_dict()["inputSchema"]["properties"]["generate"]
        assert prop["default"] is False
        assert "default true" not in cco.TOOL_DESCRIPTION.lower()


class _DeafModeOperations(_Operations):
    """A setup whose createInput hands back an input that silently drops the generationMode."""

    def createInput(self, strategy):
        return _DeafModeInput(strategy)


class _RaisingModeOperations(_Operations):
    """A setup whose createInput hands back an input that raises on generationMode."""

    def createInput(self, strategy):
        return _RaisingModeInput(strategy)


class TestGenerationMode:
    """A create that is not asked to generate assigns SkipGeneration to the input BEFORE the add,
    then READS the mode back off the input - the assignment not raising is not evidence the input
    carries the mode, since a setter can accept the value and keep the default."""

    def test_a_non_generating_create_skips_generation_on_the_input(self, monkeypatch):
        cam = _install(monkeypatch)
        ops = cam.setups.item(0).operations
        out = _payload(cco.handler(setup="Setup1", strategy="bore",
                                   tool_library_url="u", tool_index=0, generate=False))
        modes = cco.adsk.cam.AutomaticGenerationModes
        # the member, whatever int it measured to - the add is what reads it, so the assignment has
        # to have happened by then
        assert ops.modes_at_add == [modes.SkipGeneration]
        assert modes.SkipGeneration != modes.UserPreference
        assert modes.SkipGeneration != modes.ForceGeneration
        # the note is a DISAGREEMENT disclosure: the mode read back as assigned, so nothing is published
        assert "generation_mode_note" not in out

    def test_a_generating_create_leaves_the_mode_and_still_launches_the_future(self, monkeypatch):
        # generate=true keeps the explicit generateToolpath launch: its Future is the handle
        # cam_get_status polls, and a mode set on the input hands back no handle at all.
        cam = _install(monkeypatch)
        ops = cam.setups.item(0).operations
        out = _payload(cco.handler(setup="Setup1", strategy="adaptive",
                                   tool_library_url="u", tool_index=1, generate=True))
        assert ops.modes_at_add == [None]
        assert "generation_mode_note" not in out
        assert out["generation_started"] is True and len(cam.generated) == 1

    def test_a_mode_the_input_silently_drops_is_reported_unset_with_what_it_reads_back(
            self, monkeypatch):
        # THE read-back: this input takes the assignment without raising and keeps its default, so a
        # mode_set derived from "the assignment did not raise" claims a mode the input never carries.
        cam = _install(monkeypatch)
        setup = _with_operations(cam, _DeafModeOperations)
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_library_url="u", tool_index=0, generate=False))
        assert setup.operations.count == 1                 # the create still lands
        assert setup.operations.modes_at_add == [None]     # nothing landed on the input
        assert "reads back None" in out["generation_mode_note"]

    def test_an_input_that_refuses_the_mode_still_creates_and_discloses(self, monkeypatch):
        # the OTHER false path: a setter that raises must not turn into a failed create - the
        # operation lands and the payload says the mode was not set, with the platform's own text.
        cam = _install(monkeypatch)
        setup = _with_operations(cam, _RaisingModeOperations)
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_library_url="u", tool_index=0, generate=False))
        assert setup.operations.count == 1
        assert "generationMode" in out["generation_mode_note"]
        assert "reads back" not in out["generation_mode_note"]


# ── document-library tool reference (the scriptless-CAM-chain fix) ───────────

class TestDocumentToolScope:
    def test_creates_op_from_document_library(self, monkeypatch):
        # tool_scope='document' takes the tool from cam.documentToolLibrary by index — no url needed
        cam = _install(monkeypatch, doc_tools=(_tool("Demo Face Mill"), _tool("Demo Flat Endmill")))
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_scope="document", tool_index=1, generate=False))
        op = cam.setups.item(0).operations.item(0)
        assert op.tool.description == "Demo Flat Endmill"
        assert out["operation"] == "Op1"

    def test_document_index_out_of_range(self, monkeypatch):
        _install(monkeypatch, doc_tools=(_tool("only one"),))
        res = cco.handler(setup="Setup1", strategy="face", tool_scope="document", tool_index=5)
        assert res["isError"] is True and "range" in res["message"].lower()

    def test_empty_document_library(self, monkeypatch):
        _install(monkeypatch, doc_tools=())
        res = cco.handler(setup="Setup1", strategy="face", tool_scope="document", tool_index=0)
        assert res["isError"] is True and "empty" in res["message"].lower()

    def test_document_scope_ignores_url(self, monkeypatch):
        # tool_scope=document WINS over a supplied url: the tool comes from the document library and
        # url resolution is never attempted - a bogus url must not even be looked at.
        cam = _install(monkeypatch, doc_tools=(_tool("Demo Tool"),))

        def _boom(url, idx):
            raise AssertionError("url resolution attempted despite tool_scope=document")
        monkeypatch.setattr(cco, "_tool_at", _boom)
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_scope="document", tool_index=0, generate=False,
                                   tool_library_url="bogus://not-a-library"))
        assert cam.setups.item(0).operations.item(0).tool.description == "Demo Tool"
        assert out["operation"] == "Op1"

    def test_no_ref_at_all_errors(self, monkeypatch):
        # neither tool_scope=document nor a url -> a clear error
        _install(monkeypatch)
        res = cco.handler(setup="Setup1", strategy="face", tool_index=0)
        assert res["isError"] is True and "tool" in res["message"].lower()


# ── the strategy vocabulary read (_strategy_rows / read_strategies) ──────────
#
# One compatibleStrategies walk serves the create pre-flight below and
# cam_get(include=['strategies']). Measured on the base license (Fusion 2705.1.4): a milling setup
# offers 54 strategies, 33 reading isGenerationAllowed true and 21 false.


def _blocked_setup(monkeypatch, extra=()):
    """A setup offering one allowed strategy, one generation-blocked one, and whatever else a test
    adds. Returns the cam."""
    return _install(monkeypatch, strategies=(
        _Strategy("face", allowed=True, is2DStrategy=True, isMillingStrategy=True),
        _Strategy("steep_and_shallow", allowed=False, is3DStrategy=True, isMillingStrategy=True,
                  isFinishingStrategy=True),
    ) + tuple(extra))


class _DeafVocabulary(_Operations):
    """compatibleStrategies will not READ, while createInput and add still work - the platform path
    a create can still take when the vocabulary is unreadable, so no pre-flight has anything to
    check and createInput is the only gate left."""

    @property
    def compatibleStrategies(self):
        raise RuntimeError("compatibleStrategies is unreadable")

    def createInput(self, strategy):
        if strategy not in self._offered:
            raise RuntimeError("invalid strategy")
        return _OperationInput(strategy)


class TestStrategyRows:
    def test_every_property_read_is_a_measured_OperationStrategy_member(self):
        # A SWIG proxy does not raise on a name it lacks in the way a typo'd READ is visible: the
        # read simply answers nothing, read_flag turns that into null, and every classification
        # flag publishes as unknown on a live document while every test passes against a fake that
        # spells it the same wrong way. api_surface.py is generated from the installed bindings, so
        # it is the one place the real spelling lives - the short forms (is2D, isDrilling,
        # isFinishing) are NOT members of cam.OperationStrategy.
        from api_surface import PROPERTIES
        members = set(PROPERTIES["cam.OperationStrategy"])
        read = {prop for _key, prop in cco._STRATEGY_FLAGS} | {"isGenerationAllowed", "name",
                                                               "title"}
        assert read <= members, f"not OperationStrategy members: {sorted(read - members)}"

    def test_a_row_carries_title_the_entitlement_and_the_classification_flags(self, monkeypatch):
        cam = _blocked_setup(monkeypatch)
        rows = cco._strategy_rows(cam.setups.item(0))
        by_name = {r["name"]: r for r in rows}
        assert by_name["steep_and_shallow"]["title"] == "Steep And Shallow"
        assert by_name["steep_and_shallow"]["allowed"] is False
        assert by_name["steep_and_shallow"]["is_3d"] is True
        assert by_name["steep_and_shallow"]["is_finishing"] is True
        assert by_name["steep_and_shallow"]["is_2d"] is False        # read false, not missing
        assert by_name["face"]["allowed"] is True and by_name["face"]["is_2d"] is True

    def test_an_unreadable_entitlement_reads_null_not_false(self, monkeypatch):
        # THE honesty boundary: safe(read, False) here would mint a blocked strategy out of a
        # property that never answered, and the create would refuse a strategy this license allows.
        cam = _blocked_setup(monkeypatch,
                             extra=(_Strategy("mystery", allowed=None, isMillingStrategy=True),))
        rows = {r["name"]: r for r in cco._strategy_rows(cam.setups.item(0))}
        assert rows["mystery"]["allowed"] is None
        assert rows["mystery"]["is_milling"] is True     # the readable flags still read

    def test_a_raw_std_vector_with_no_count_or_item_is_still_walked(self, monkeypatch):
        # OperationStrategyVector answers len()/[i] only. A reader built on _common.iter_collection
        # (count/item) reads nothing at all through safe(), and the tool would then call every
        # strategy incompatible.
        cam = _blocked_setup(monkeypatch)
        vec = cam.setups.item(0).operations.compatibleStrategies
        assert not hasattr(vec, "count") and not hasattr(vec, "item")
        assert [r["name"] for r in cco._strategy_rows(cam.setups.item(0))] == [
            "face", "steep_and_shallow"]

    def test_a_strategy_whose_name_does_not_read_is_dropped(self, monkeypatch):
        # a nameless row is not addressable by the 'strategy' input, so publishing it would offer a
        # value no call can pass back.
        cam = _blocked_setup(monkeypatch, extra=(_Strategy(None),))
        assert [r["name"] for r in cco._strategy_rows(cam.setups.item(0))] == [
            "face", "steep_and_shallow"]

    def test_an_unreadable_vector_reads_None_not_an_empty_vocabulary(self, monkeypatch):
        # [] here would say "this setup offers no strategies", which every caller then turns into a
        # refusal naming an empty list. None is the third state, and it is what the callers branch on.
        cam = _install(monkeypatch)
        setup = cam.setups.item(0)

        class _Deaf:
            @property
            def compatibleStrategies(self):
                raise RuntimeError("compatibleStrategies is unreadable")

        setup.operations = _Deaf()
        assert cco._strategy_rows(setup) is None

    def test_a_setup_offering_nothing_still_reads_an_empty_list(self, monkeypatch):
        # the other side of the tri-state: a vector that ANSWERS with no entries is [], not None -
        # collapsing the two would make an unreadable read indistinguishable from an empty one.
        cam = _install(monkeypatch, strategies=())
        assert cco._strategy_rows(cam.setups.item(0)) == []

    def test_the_four_added_classification_flags_are_published(self, monkeypatch):
        # isCuttingStrategy / isAdditiveStrategy / isSupportStrategy / isSuppressible are
        # OperationStrategy members a subtractive-vs-additive caller reads to tell the job apart.
        cam = _blocked_setup(monkeypatch, extra=(
            _Strategy("lattice", allowed=True, isAdditiveStrategy=True, isSupportStrategy=True),))
        rows = {r["name"]: r for r in cco._strategy_rows(cam.setups.item(0))}
        assert rows["lattice"]["is_additive"] is True and rows["lattice"]["is_support"] is True
        assert rows["lattice"]["is_cutting"] is False
        assert rows["lattice"]["is_suppressible"] is False


class TestReadStrategies:
    """The wire wrapper cam_get(include=['strategies']) unwraps."""

    def _payload_of(self, res):
        assert res["isError"] is False, res
        return json.loads(res["content"][0]["text"])

    def test_the_tallies_split_true_false_and_unreadable(self, monkeypatch):
        _blocked_setup(monkeypatch, extra=(_Strategy("mystery", allowed=None),))
        out = self._payload_of(cco.read_strategies())
        row = out["setups"][0]
        assert row["strategy_count"] == 3
        assert row["allowed_count"] == 1 and row["blocked_count"] == 1
        assert row["unreadable_count"] == 1
        # each tally is an identity test, so an unreadable flag lands in neither of the other two
        assert row["allowed_count"] + row["blocked_count"] + row["unreadable_count"] == 3

    def test_unreadable_count_is_absent_when_every_flag_answered(self, monkeypatch):
        # absent means "every flag read", so publishing a 0 would make the honest case look like a
        # partial read every time.
        _blocked_setup(monkeypatch)
        out = self._payload_of(cco.read_strategies())
        assert "unreadable_count" not in out["setups"][0]

    def test_an_unreadable_vocabulary_publishes_null_tallies_not_zeros(self, monkeypatch):
        # a 0 strategy_count reads as "this setup can create nothing", which is a verdict the read
        # never made - the row says the list did not read and leaves every tally unanswered.
        cam = _install(monkeypatch, setups=("Top",))
        cam.setups.item(0).operations = _DeafVocabulary(["face"])
        row = self._payload_of(cco.read_strategies())["setups"][0]
        assert row["strategies_read"] is False
        assert row["strategy_count"] is None and row["allowed_count"] is None
        assert row["blocked_count"] is None and row["strategies"] == []

    def test_a_readable_setup_publishes_no_strategies_read_key(self, monkeypatch):
        # absent = the list read, so emitting the key on every row would make the disclosure invisible.
        _blocked_setup(monkeypatch)
        assert "strategies_read" not in self._payload_of(cco.read_strategies())["setups"][0]

    def test_every_setup_is_read_when_none_is_named(self, monkeypatch):
        _install(monkeypatch, setups=("Top", "Bottom"))
        out = self._payload_of(cco.read_strategies())
        assert out["setup_count"] == 2
        assert [r["setup"] for r in out["setups"]] == ["Top", "Bottom"]

    def test_a_named_setup_scopes_the_read(self, monkeypatch):
        _install(monkeypatch, setups=("Top", "Bottom"))
        out = self._payload_of(cco.read_strategies("Bottom"))
        assert out["setup_count"] == 1 and out["setups"][0]["setup"] == "Bottom"

    def test_a_setup_miss_returns_the_shared_resolvers_refusal(self, monkeypatch):
        _install(monkeypatch, setups=("Top",))
        res = cco.read_strategies("Ghost")
        assert res["isError"] is True
        assert "Ghost" in res["message"] and "Top" in res["message"]

    def test_no_cam_is_refused_before_any_walk(self, monkeypatch):
        monkeypatch.setattr(cco, "get_cam", lambda: (None, "no CAM data"))
        res = cco.read_strategies()
        assert res["isError"] is True and "cam" in res["message"].lower()


# ── the entitlement pre-flight ───────────────────────────────────────────────
#
# Measured live on the base license (Fusion 2705.1.4): creating a strategy that reads
# isGenerationAllowed false SUCCEEDS - the operation is minted and the tool bound - and cam_generate
# then declines it with no error and no warning text, parking the op at no_toolpath/is_out_of_date.
# So the guard replaces a silent failure, and both wire strings say only that.


class TestNamesThatAreNotOperations:
    """MEASURED: hole_recognition and folder read isGenerationAllowed TRUE and operations.add over
    either answers with a name while the setup's operation count does not move. That COUNT is what
    refuses - the table only names what does the job instead, so a build where one of them starts
    landing creates it rather than meeting a refusal that outlived its measurement."""

    def _empty_add(self, monkeypatch, strategy):
        cam = _install(monkeypatch, strategies=("face", strategy))
        _with_operations(cam, _DeafOperations)
        return cco.handler(setup="Setup1", strategy=strategy,
                           tool_library_url="u", tool_index=0, generate=False)

    def test_hole_recognition_points_at_the_drilling_cycle_and_its_selection(self, monkeypatch):
        res = self._empty_add(monkeypatch, "hole_recognition")
        assert res["isError"] is True and "did not land" in res["message"]
        assert "'hole_recognition' is not an operation" in res["message"]
        assert "cam_select_geometry(selection='holes')" in res["message"]

    def test_folder_points_at_the_tool_that_makes_one(self, monkeypatch):
        res = self._empty_add(monkeypatch, "folder")
        assert res["isError"] is True and "cam_edit_folders" in res["message"]

    def test_a_real_strategy_that_does_not_land_gets_no_category_clause(self, monkeypatch):
        # the boundary: a 'face' that failed to land is a FAULT, not a category error, and telling
        # its caller to reach for another tool would send it away from a real bug.
        res = self._empty_add(monkeypatch, "face")
        assert res["isError"] is True and "did not land" in res["message"]
        assert "is not an operation" not in res["message"]


class TestDrillingAxisNote:
    """MEASURED on a milling setup whose Z is the world Z, drilling a hole bored along world X: the
    generate errored 'Cylindrical face not in tool orientation!' and binding the setup's Z to that
    hole's own face cleared it. The create is where an agent can still turn the setup."""

    def _drill_setup(self, monkeypatch):
        return _install(monkeypatch, strategies=(
            _Strategy("drill", allowed=True, isDrillingStrategy=True, isMillingStrategy=True),
            _Strategy("face", allowed=True, is2DStrategy=True, isMillingStrategy=True)))

    def test_a_drilling_create_names_the_setup_z_rule_and_the_remedy(self, monkeypatch):
        self._drill_setup(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="drill",
                                   tool_library_url="u", tool_index=0))
        assert "cuts along the SETUP's Z" in out["note"]
        assert "Cylindrical face not in tool orientation!" in out["note"]   # the observed text
        assert "cam_edit_setup(wcs={'z_axis'" in out["note"]

    def test_a_non_drilling_create_carries_none_of_it(self, monkeypatch):
        self._drill_setup(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_library_url="u", tool_index=0))
        assert "SETUP's Z" not in out["note"]

    def test_the_rule_survives_the_generate_arm_that_rewrites_the_note(self, monkeypatch):
        # generate=true REPLACES the note; the axis rule is what the cycle is aimed by either way.
        self._drill_setup(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="drill", generate=True,
                                   tool_library_url="u", tool_index=0))
        assert "generation started" in out["note"] and "cuts along the SETUP's Z" in out["note"]


class TestStrategyEntitlement:
    def test_a_generation_blocked_strategy_is_refused_and_nothing_is_created(self, monkeypatch):
        cam = _blocked_setup(monkeypatch)
        res = cco.handler(setup="Setup1", strategy="steep_and_shallow",
                          tool_library_url="u", tool_index=0)
        assert res["isError"] is True
        assert "steep_and_shallow" in res["message"]
        assert "isGenerationAllowed false" in res["message"]
        assert cam.setups.item(0).operations.count == 0      # the refusal creates nothing

    def test_an_allowed_strategy_in_the_same_setup_still_creates(self, monkeypatch):
        # the other side of `allowed is False`: the guard may not refuse the whole vocabulary.
        cam = _blocked_setup(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_library_url="u", tool_index=0))
        assert out["strategy"] == "face"
        assert cam.setups.item(0).operations.count == 1

    def test_an_unreadable_entitlement_creates_and_discloses_instead_of_refusing(self, monkeypatch):
        # `is False`, not falsiness: None is a flag that never answered, and refusing on it would
        # invent an entitlement verdict. The create runs the platform's own path and says so.
        cam = _blocked_setup(monkeypatch, extra=(_Strategy("mystery", allowed=None),))
        out = _payload(cco.handler(setup="Setup1", strategy="mystery",
                                   tool_library_url="u", tool_index=0))
        assert cam.setups.item(0).operations.count == 1
        assert out["entitlement_checked"] is False
        assert "isGenerationAllowed did not read" in out["note"]

    def test_a_checked_create_publishes_no_entitlement_key(self, monkeypatch):
        # absent = the flag read and the pre-flight ran; emitting the key on every create would make
        # the disclosure invisible.
        _blocked_setup(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_library_url="u", tool_index=0))
        assert "entitlement_checked" not in out
        assert "isGenerationAllowed" not in out["note"]

    def test_the_disclosure_rides_the_generate_path_too(self, monkeypatch):
        # generate=true REPLACES the note, so a disclosure written before that assembly is silently
        # dropped on exactly the call that goes on to launch a generation.
        _blocked_setup(monkeypatch, extra=(_Strategy("mystery", allowed=None),))
        out = _payload(cco.handler(setup="Setup1", strategy="mystery",
                                   tool_library_url="u", tool_index=1, generate=True))
        assert out["generation_started"] is True
        assert "generation started" in out["note"]
        assert "isGenerationAllowed did not read" in out["note"]

    def test_the_refusal_fires_only_after_the_tool_reference_resolves(self, monkeypatch):
        # a blocked strategy AND a bad tool index: the caller hears about the tool reference it
        # actually got wrong, not an entitlement verdict standing in for it.
        _blocked_setup(monkeypatch, extra=())
        res = cco.handler(setup="Setup1", strategy="steep_and_shallow",
                          tool_library_url="u", tool_index=9)
        assert res["isError"] is True
        assert "range" in res["message"].lower()
        assert "isGenerationAllowed" not in res["message"]

    def test_the_refusal_names_the_entitlement_and_the_slice_that_lists_the_allowed_ones(
            self, monkeypatch):
        _blocked_setup(monkeypatch)
        res = cco.handler(setup="Setup1", strategy="steep_and_shallow",
                          tool_library_url="u", tool_index=0)
        msg = res["message"]
        assert "Machining Extension" in msg
        assert "cam_get(include=['strategies'], setup='Setup1')" in msg

    def test_the_refusal_states_the_measured_silent_failure_it_replaces(self, monkeypatch):
        # the whole value of the guard: a caller who does not know that a blocked create SUCCEEDS
        # and then never generates will read the refusal as the tool being over-strict. The silence
        # is the OPERATION's own. What cam_generate then does with such an op is that tool's error.
        _blocked_setup(monkeypatch)
        msg = cco.handler(setup="Setup1", strategy="steep_and_shallow",
                          tool_library_url="u", tool_index=0)["message"]
        assert "SUCCEEDED" in msg and "never generated" in msg
        assert "no toolpath and no error or warning text of its own" in msg

    def test_an_incompatible_strategy_is_still_a_compatibility_miss(self, monkeypatch):
        # the two halves come off ONE walk now; a name the setup does not offer must still be worded
        # as incompatible and list what it does offer, never as an entitlement problem.
        _blocked_setup(monkeypatch)
        res = cco.handler(setup="Setup1", strategy="frobnicate",
                          tool_library_url="u", tool_index=0)
        msg = res["message"]
        assert "isn't compatible" in msg and "steep_and_shallow" in msg
        assert "isGenerationAllowed" not in msg

    def test_a_blocked_strategy_is_still_listed_as_compatible(self, monkeypatch):
        # it IS in the setup's vocabulary - the compatibility listing may not quietly hide it, or a
        # caller comparing the two messages cannot tell "not offered" from "not entitled".
        _blocked_setup(monkeypatch)
        msg = cco.handler(setup="Setup1", strategy="frobnicate",
                          tool_library_url="u", tool_index=0)["message"]
        assert "Compatible: face, steep_and_shallow." in msg


class TestCompatibilityListing:
    """What the compatibility refusal names, and the three states the vocabulary itself can be in:
    a list of names, no names at all, and a list that never read."""

    def test_the_listing_counts_the_names_it_did_not_fit(self, monkeypatch):
        # a silently cut list reads as the COMPLETE vocabulary, and a caller comparing its strategy
        # against it concludes the setup does not offer one it does.
        _install(monkeypatch, strategies=tuple(f"s{i}" for i in range(9)))
        msg = cco.handler(setup="Setup1", strategy="ghost",
                          tool_library_url="u", tool_index=0)["message"]
        assert "s0, s1, s2, s3, s4, s5, s6, s7, ... (+1 more not listed)." in msg
        assert "s8" not in msg

    def test_a_listing_that_fits_carries_no_remainder(self, monkeypatch):
        # the other side of the cap: eight names is the last size that fits, and a "+0 more" there
        # would tell a caller something was withheld when nothing was.
        _install(monkeypatch, strategies=tuple(f"s{i}" for i in range(8)))
        msg = cco.handler(setup="Setup1", strategy="ghost",
                          tool_library_url="u", tool_index=0)["message"]
        assert "Compatible: s0, s1, s2, s3, s4, s5, s6, s7." in msg
        assert "not listed" not in msg

    def test_a_setup_offering_nothing_says_so_instead_of_naming_an_empty_list(self, monkeypatch):
        _install(monkeypatch, strategies=())
        msg = cco.handler(setup="Setup1", strategy="face",
                          tool_library_url="u", tool_index=0)["message"]
        assert "offers no compatible strategies at all" in msg
        assert "Compatible: ." not in msg

    def test_an_unreadable_vocabulary_is_not_a_compatibility_refusal(self, monkeypatch):
        # THE fabricated refusal: with no rows to check against, every strategy read as incompatible
        # and the listing that was supposed to back the claim was empty. A read that never answered
        # is not a verdict, so the create takes the platform's own path and discloses both skipped
        # pre-flights.
        cam = _install(monkeypatch)
        cam.setups.item(0).operations = _DeafVocabulary(["face"])
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_library_url="u", tool_index=0))
        assert cam.setups.item(0).operations.count == 1
        assert out["strategy_checked"] is False and out["entitlement_checked"] is False
        assert "compatibleStrategies did not read" in out["note"]

    def test_an_unreadable_vocabulary_still_lets_createInput_refuse(self, monkeypatch):
        # the disclosure is not a bypass: the platform's own gate is what the create then rests on,
        # so a strategy this setup really does not offer is still refused - by createInput.
        cam = _install(monkeypatch)
        cam.setups.item(0).operations = _DeafVocabulary(["face"])
        res = cco.handler(setup="Setup1", strategy="frobnicate",
                          tool_library_url="u", tool_index=0)
        assert res["isError"] is True and "createInput('frobnicate') failed" in res["message"]
        assert cam.setups.item(0).operations.count == 0

    def test_a_readable_setup_publishes_neither_skipped_pre_flight_key(self, monkeypatch):
        _blocked_setup(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="face",
                                   tool_library_url="u", tool_index=0))
        assert "strategy_checked" not in out and "entitlement_checked" not in out


class TestComposedWireLength:
    """A note or error assembled at RUN TIME is not a literal, so test_prose_budget cannot measure
    it - these compositions are where the 400-char wire budget is actually spent."""

    _BUDGET = 400        # test_prose_budget.NOTE_BUDGET_CHARS

    def test_the_create_note_plus_its_disclosure_fits_the_budget(self, monkeypatch):
        # the longest composition this tool ships: the no-generate note with the entitlement
        # pre-flight disclosed as skipped.
        _blocked_setup(monkeypatch, extra=(_Strategy("mystery", allowed=None),))
        out = _payload(cco.handler(setup="Setup1", strategy="mystery",
                                   tool_library_url="u", tool_index=0))
        assert len(out["note"]) <= self._BUDGET, out["note"]

    def test_the_blocked_strategy_refusal_fits_the_budget_with_long_names(self, monkeypatch):
        # the refusal interpolates the strategy name once and the setup name twice, so the longest
        # names in a real job are what the budget has to hold.
        _install(monkeypatch, setups=("Swarf and Deburr Setup",), strategies=(
            _Strategy("multiaxis_contour_finishing", allowed=False),))
        msg = cco.handler(setup="Swarf and Deburr Setup",
                          strategy="multiaxis_contour_finishing",
                          tool_library_url="u", tool_index=0)["message"]
        assert len(msg) <= self._BUDGET, msg


class _DedupingOperation(_Operation):
    """Operation.name DEDUPES against whatever already answers to the name - the operation ITSELF
    included: writing back the name it already reads lands '<name>1' (measured live)."""

    @property
    def name(self):
        return self._n

    @name.setter
    def name(self, value):
        object.__setattr__(self, "_n",
                           str(value) + "1" if getattr(self, "_n", None) is not None else str(value))


class _StubbornOperation(_Operation):
    """A name the platform declines to change. The operation LANDED, so the payload publishes the
    name it reads back and discloses the decline - it is not a failed create."""

    @property
    def name(self):
        return "Op1"

    @name.setter
    def name(self, value):
        pass


class _RaisingDisplayNameOperations(_Operations):
    """A setup whose createInput hands back an input that raises on displayName."""

    def createInput(self, strategy):
        return _RaisingDisplayNameInput(strategy)


class _DeafDisplayNameOperations(_Operations):
    """A setup whose createInput hands back an input that silently drops the displayName."""

    def createInput(self, strategy):
        return _DeafDisplayNameInput(strategy)


class _RenameCountingOperation(_Operation):
    """Counts the writes to Operation.name that land AFTER the add - the write that makes the
    platform generate a hole operation."""

    def __init__(self, inp):
        object.__setattr__(self, "_after_add_writes", 0)
        object.__setattr__(self, "_landed", False)
        super().__init__(inp)
        object.__setattr__(self, "_landed", True)

    @property
    def name(self):
        return self._n

    @name.setter
    def name(self, value):
        object.__setattr__(self, "_n", str(value))
        if self._landed:
            object.__setattr__(self, "_after_add_writes", self._after_add_writes + 1)


class _InputNameDedupingOperation(_Operation):
    """The platform DEDUPING the name it was handed on the input: the operation lands carrying
    '<name>1', which is what Operation.name then reads back."""

    def __init__(self, inp):
        super().__init__(inp)
        self.name = self.name + "1"


class TestName:
    """'name' rides the OperationInput and is read back off the operation: a name the platform took
    is published as asked, and one it deduped or declined is disclosed rather than re-written."""

    def test_the_new_operation_takes_the_requested_name(self, monkeypatch):
        cam = _install(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="face", tool_library_url="u",
                                   tool_index=0, name="Rough Pocket"))
        assert cam.setups.item(0).operations.added[-1].name == "Rough Pocket"
        assert out["operation"] == "Rough Pocket" and "rename_warning" not in out

    def test_no_name_keeps_the_platforms_own_name(self, monkeypatch):
        cam = _install(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="face", tool_library_url="u",
                                   tool_index=0))
        assert out["operation"] == "Op1"
        assert cam.setups.item(0).operations.added[-1].name == "Op1"
        assert cam.setups.item(0).operations.names_at_add == [None]
        assert "rename_warning" not in out         # absent = no name was asked for

    def test_a_named_create_carries_the_name_on_the_input_and_never_renames_after_the_add(
            self, monkeypatch):
        # A rename applied AFTER the add makes the platform generate a hole operation, and with no
        # faces selected that generation parks the call behind a modal (measured on a bore in a
        # stock-carrying setup). The name has to be on the input the add reads.
        cam = _install(monkeypatch)
        ops = cam.setups.item(0).operations
        ops.op_class = _RenameCountingOperation
        out = _payload(cco.handler(setup="Setup1", strategy="bore", tool_library_url="u",
                                   tool_index=0, name="BoreNamedX"))
        assert ops.names_at_add == ["BoreNamedX"]
        assert ops.added[-1]._after_add_writes == 0
        assert out["operation"] == "BoreNamedX"
        assert "rename_warning" not in out

    def test_a_platform_deduped_name_publishes_the_read_back_name_with_the_disclosure(
            self, monkeypatch):
        # The operation LANDED, so a name the platform reshaped is a disclosure - and re-writing
        # op.name to chase the request is exactly the post-add rename the create must not do, so the
        # published name is whatever Operation.name reads back.
        cam = _install(monkeypatch)
        ops = cam.setups.item(0).operations
        ops.op_class = _InputNameDedupingOperation
        out = _payload(cco.handler(setup="Setup1", strategy="bore", tool_library_url="u",
                                   tool_index=0, name="Bore"))
        assert ops.names_at_add == ["Bore"]
        assert ops.added[-1].name == "Bore1"
        assert out["operation"] == "Bore1"
        assert "'Bore' did not take" in out["rename_warning"]
        assert "named 'Bore1'" in out["rename_warning"]

    def test_an_input_that_refuses_the_name_falls_back_to_the_post_add_rename(self, monkeypatch):
        # A setter that raises must not turn into a nameless create: the operation carries no name
        # at the add and is renamed after it, which is the one write that route makes.
        cam = _install(monkeypatch)
        setup = _with_operations(cam, _RaisingDisplayNameOperations)
        ops = setup.operations
        ops.op_class = _RenameCountingOperation
        out = _payload(cco.handler(setup="Setup1", strategy="face", tool_library_url="u",
                                   tool_index=0, name="Rough Pocket"))
        assert ops.names_at_add == [None]
        assert ops.added[-1].name == "Rough Pocket"
        assert ops.added[-1]._after_add_writes == 1
        assert out["operation"] == "Rough Pocket" and "rename_warning" not in out

    def test_a_name_the_input_silently_drops_publishes_the_read_back_name_and_discloses(
            self, monkeypatch):
        # The input takes the assignment without raising and keeps its default, so the operation is
        # born under the PLATFORM's name. The payload publishes that read-back name and the
        # disclosure names the one that was asked for - no post-add rename chases it.
        cam = _install(monkeypatch)
        setup = _with_operations(cam, _DeafDisplayNameOperations)
        ops = setup.operations
        ops.op_class = _RenameCountingOperation
        out = _payload(cco.handler(setup="Setup1", strategy="bore", tool_library_url="u",
                                   tool_index=0, name="Rough Pocket"))
        assert ops.names_at_add == [None]                  # nothing landed on the input
        assert ops.added[-1].name == "Op1"
        assert ops.added[-1]._after_add_writes == 0        # the modal-parking write never happens
        assert out["operation"] == "Op1"
        assert "'Rough Pocket' did not take" in out["rename_warning"]
        assert "named 'Op1'" in out["rename_warning"]

    def test_a_name_an_operation_already_answers_to_is_refused_before_the_add(self, monkeypatch):
        # Operation.name DEDUPES rather than refusing ('Face1' taken -> 'Face11', measured), so a
        # taken name would land as one the caller never asked for. It is refused before the create,
        # through the same check the rename arm runs - and nothing is added.
        cam = _install(monkeypatch)
        cam.setups.item(0).operations.add(
            cam.setups.item(0).operations.createInput("face"))          # an operation named 'Op1'
        res = cco.handler(setup="Setup1", strategy="face", tool_library_url="u", tool_index=0,
                          name="Op1")
        assert res["isError"] is True
        assert "already answer to 'Op1'" in res["message"]
        assert "dedupes rather than refusing" in res["message"]
        assert cam.setups.item(0).operations.count == 1                 # nothing was created

    def test_the_ops_own_auto_name_requested_back_is_not_a_self_dedupe(self, monkeypatch):
        # Measured: writing onto an operation the name it ALREADY reads dedupes it against ITSELF
        # ('Op1' -> 'Op11'). The clash check cannot see that (no other op carries the name at that
        # point), so an op born under the requested name must be left alone entirely.
        cam = _install(monkeypatch)
        ops = cam.setups.item(0).operations
        ops.add = lambda inp: (ops.added.append(_DedupingOperation(inp)), ops.added[-1])[1]
        out = _payload(cco.handler(setup="Setup1", strategy="face", tool_library_url="u",
                                   tool_index=0, name="Op1"))
        assert out["operation"] == "Op1"
        assert "rename_warning" not in out
        assert cam.setups.item(0).operations.added[-1].name == "Op1"

    def test_a_free_name_still_creates(self, monkeypatch):
        # the other side of the clash gate: a name no operation carries is not refused
        cam = _install(monkeypatch)
        out = _payload(cco.handler(setup="Setup1", strategy="face", tool_library_url="u",
                                   tool_index=0, name="Rough Pocket"))
        assert out["operation"] == "Rough Pocket"
        assert cam.setups.item(0).operations.count == 1

    def test_a_declined_name_is_disclosed_not_published_as_requested(self, monkeypatch):
        cam = _install(monkeypatch)
        ops = cam.setups.item(0).operations
        ops.add = lambda inp: (ops.added.append(_StubbornOperation(inp)), ops.added[-1])[1]
        out = _payload(cco.handler(setup="Setup1", strategy="face", tool_library_url="u",
                                   tool_index=0, name="Rough Pocket"))
        assert out["operation"] == "Op1"
        assert "Rough Pocket" in out["rename_warning"] and "did not take" in out["rename_warning"]
