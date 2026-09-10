# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The CAM worlds: the setup/folder/operation tree, the inspection results, and the job objects."""

import types

import live_api_facts as _api_facts

from tests.fakes.geometry import FakePoint, FakeVector3D
from tests.fakes.scaffold import _NamedCollection, fusion_fake


# ── CAM tree fakes (setup / folder / operation) ────────────────────────────
#
# The shared object model for the CAM setup/operation tree every cam_* tool walks. The one
# load-bearing behavior lives here ONCE: allOperations mirrors the measured live flatten
# (BEHAVIOR flags) - folder/pattern children are flattened IN while the folder/pattern
# CONTAINERS are DROPPED - so a container is only reachable through the explicit
# .operations/.folders/.patterns walk, exactly like live Fusion.

@fusion_fake(live_type="Operation", facts=("shape-dump-cam-world",))
class FakeOperation:
    """A CAM Operation leaf. Every attribute is real per live_api_facts.SHAPES['Operation'].

    state_readable=False drives the DEFENSIVE branch for an operationState that will not read (the
    _InspPoint shape) - no walk of a live document here has answered one, so it is a guard rather
    than a measured live shape. Callers read the member through safe() with no default, so the facts
    carry operation_state None - every value operationState CAN answer is itself a state, so a
    coerced default would publish one of them off a read that never happened."""
    def __init__(self, name, has_toolpath=True, valid=True, suppressed=False, shown=False,
                 operation_state=0, has_error=False, error="", has_warning=False, warning="",
                 state_readable=True, strategy="contour2d", parameters=None, tool=None):
        self.name = name
        # An operation with no `parameters` given still answers a collection - the read every CAM
        # tool makes; `tool` unset answers None, the empty read a tool row has to survive.
        self.parameters = FakeCAMParameters() if parameters is None else parameters
        self.tool = tool
        # Operation.strategy - 'manual' is the Manual NC pass-through, which carries no toolpath by
        # construction and is excluded from the empty-toolpath class.
        self.strategy = strategy
        self.hasToolpath = has_toolpath
        self.isToolpathValid = valid
        self.isSuppressed = suppressed
        self.isLightBulbOn = shown
        self.hasError = has_error
        self.error = error
        self.hasWarning = has_warning
        self.warning = warning
        self.isGenerating = False
        self.generatingProgress = None
        self._operation_state = operation_state
        self._state_readable = state_readable

    @property
    def operationState(self):
        if not self._state_readable:
            raise RuntimeError("operationState cannot be read on this operation")
        return self._operation_state


@fusion_fake(live_type="CAMFolder", facts=("shape-dump-cam-world", "cam-alloperations-shape"))
class FakeCAMFolder:
    """A CAM folder/pattern container: .operations/.folders/.patterns hold the DIRECT children
    (Fusion's count/item protocol); allOperations applies the measured flatten (see the section
    note above). Use it for a pattern too - the tools classify nodes STRUCTURALLY (by which
    collection yielded them), never by type name."""
    def __init__(self, name, ops=(), folders=(), patterns=()):
        self.name = name
        self.operations = _NamedCollection(list(ops))
        self.folders = _NamedCollection(list(folders))
        self.patterns = _NamedCollection(list(patterns))

    @property
    def allOperations(self):
        flat = list(self.operations)
        for coll in (self.folders, self.patterns):
            for child in coll:
                if _api_facts.BEHAVIOR["alloperations_flattens_folder_children"]:
                    flat.extend(child.allOperations)
                if not _api_facts.BEHAVIOR["alloperations_drops_folder_objects"]:
                    flat.append(child)
        return _NamedCollection(flat)


@fusion_fake(live_type="Setup", facts=("shape-dump-cam-world", "cam-alloperations-shape"))
class FakeSetup(FakeCAMFolder):
    """A CAM Setup: the same container protocol + measured allOperations flatten as FakeCAMFolder,
    plus Setup.parameters - the collection the WCS binding is written to and read back from
    (build one with wcs_params below; None models a setup whose parameters do not read) - and the
    activate()/isActive pair. activate() returns a bool AND flips isActive, as the live one does;
    'activate_lies' models it returning true while the setup never becomes active.

    `machine` and the `has_error`/`error` fault pair are set only when given, so a setup whose
    machine or fault channel does not read stays a testable state; live every setup carries all
    three, so an unset one is this fake's DECLARED absence rather than a shape live presents.
    `machine=None` is the measured "no machine assigned" answer (what setup_blockers calls
    no_machine_selected), and `has_error=False` the plain no-fault answer live_readiness reads."""

    _UNSET = object()

    def __init__(self, name, ops=(), folders=(), patterns=(), parameters=None, is_active=False,
                 activate_ok=True, activate_lies=False, machine=_UNSET, has_error=_UNSET,
                 error=_UNSET):
        super().__init__(name, ops=ops, folders=folders, patterns=patterns)
        self.parameters = parameters
        if machine is not FakeSetup._UNSET:
            self.machine = machine
        if has_error is not FakeSetup._UNSET:
            self.hasError = has_error
        if error is not FakeSetup._UNSET:
            self.error = error
        self.isActive = is_active
        self._activate_ok = activate_ok
        self._activate_lies = activate_lies
        self._activate_calls = 0     # bookkeeping, private: the live Setup has no such member

    def activate(self):
        self._activate_calls += 1
        if self._activate_ok and not self._activate_lies:
            self.isActive = True
        return self._activate_ok


@fusion_fake(factory_for="_NamedCollection")
def wcs_params(origin_mode=None, orientation_mode=None, origin=None, z_axis=None):
    """A Setup.parameters collection of FakeCAMParameter, holding the WCS parameters as the
    read-back sees them - each one's .value.value is the payload (the mode string on a
    ChoiceParameterValue, the bound entities on a CadObjectParameterValue). A mode is its string;
    `origin`/`z_axis` are lists of (object_type, name) entity specs - None omits that parameter,
    [] models a present-but-unbound one, and name=None an entity with no readable name."""
    def _entity(spec):
        object_type, name = spec
        ent = types.SimpleNamespace(objectType=object_type)
        if name is not None:
            ent.name = name
        return ent

    params = []
    for pname, mode in (("wcs_origin_mode", origin_mode),
                        ("wcs_orientation_mode", orientation_mode)):
        if mode is not None:
            params.append(FakeCAMParameter(pname, value=mode))
    for pname, specs in (("wcs_origin_point", origin), ("wcs_orientation_axisZ", z_axis)):
        if specs is not None:
            params.append(FakeCAMParameter(pname, value=[_entity(s) for s in specs]))
    return _NamedCollection(params)


class _Strategy:
    """An OperationStrategy as the entitlement seam reads it - only isGenerationAllowed. Named
    without a Fake prefix because OperationStrategy has no live SHAPES dump to sweep against
    (test_fake_shapes_exist), like the _Insp* trio below. allowed=None makes the flag itself RAISE."""

    def __init__(self, allowed):
        self._allowed = allowed

    @property
    def isGenerationAllowed(self):
        if self._allowed is None:
            raise RuntimeError("isGenerationAllowed is unreadable on this build")
        return self._allowed


def strategy_factory(table, seen=None):
    """A stand-in for _cam_common._create_strategy (the OperationStrategy.createFromString seam
    workspace_orient's capability block and cam_generate's launch pre-flight share): `table` maps a
    strategy name -> True / False / None (None => the flag raises when read). A name ABSENT from the
    table RAISES the way the live factory does on an unknown/renamed strategy, which must degrade to
    null. `seen` counts the calls per name, for pinning the probe-once-per-strategy contract."""
    def create(name):
        if seen is not None:
            seen[name] = seen.get(name, 0) + 1
        if name not in table:
            raise RuntimeError(f"3 : Unknown strategy: {name}")
        return _Strategy(table[name])
    return create


@fusion_fake(factory_for="_NamedCollection")
def make_cam(*setups, machining_times=None):
    """A minimal CAM product carrying `setups` (count/item protocol) - pair with
    `monkeypatch.setattr(mod, "get_cam", lambda: (cam, None))`.

    getMachiningTime answers off `machining_times`, a {operation name: seconds} mapping - the
    second signal the EMPTY-toolpath class reads (an operation that generated an empty toolpath
    still reads hasToolpath True and answers 0.0 s). An operation the mapping does not carry RAISES
    '3 : Machining time could not be calculated.', which is what an operation holding no toolpath
    answers, so a test that wants a time has to name the operation it wants one for. The call
    records into .machining_time_calls, for a test that cares how often it was made."""
    times = dict(machining_times or {})
    calls = []

    def _machining_time(obj, *knobs):
        calls.append((obj, knobs))
        name = getattr(obj, "name", None)
        if name not in times:
            raise RuntimeError("3 : Machining time could not be calculated.")
        return types.SimpleNamespace(machiningTime=times[name])

    return types.SimpleNamespace(setups=_NamedCollection(list(setups)),
                                 getMachiningTime=_machining_time,
                                 machining_time_calls=calls)


# ── inspection results (CAM.inspectionResults) - the recorded probing measurements ────────────────
#
# The member sets are exactly the live ones. _InspMeasure has NO .name on purpose: a measure folder
# exposes none (the browser name is not readable, and no call enumerates the legal names), so a fake
# that carried one would teach a round-trip that cannot exist. Named without a Fake prefix because
# these types have no live SHAPES dump to sweep against (test_fake_shapes_exist).

class _InspPoint:
    """InspectionPointResult: nominalPosition/projectedPoint/contact (Point3D), delta (Vector3D),
    offset/deviation/error (CM) and state. readable=False models a property that raises."""

    def __init__(self, state, deviation=0.0, error=0.0, offset=0.0, nominal=(0.0, 0.0, 0.0),
                 contact=(0.0, 0.0, 0.0), projected=(0.0, 0.0, 0.0), delta=(0.0, 0.0, 0.0),
                 readable=True):
        self.state = state
        self._deviation = deviation
        self.error = error
        self.offset = offset
        self.nominalPosition = FakePoint(*nominal)
        self.contact = FakePoint(*contact)
        self.projectedPoint = FakePoint(*projected)
        self.delta = FakeVector3D(*delta)
        self._readable = readable

    @property
    def deviation(self):
        if not self._readable:
            raise RuntimeError("deviation cannot be read on this point")
        return self._deviation


class _InspPath:
    """InspectionPathResult: pointResults is its only member (a counted collection; the shared
    _NamedCollection's itemByName goes unused - the live one carries only count/item)."""

    def __init__(self, points):
        self.pointResults = _NamedCollection(list(points))


class _InspMeasure:
    """CAMMeasure: inspectionPathResults is its only member. paths=None models the documented
    'null if none found'."""

    def __init__(self, paths):
        self.inspectionPathResults = None if paths is None else _NamedCollection(list(paths))


@fusion_fake(factory_for="_NamedCollection")
def make_inspection_cam(measures):
    """A CAM product whose inspectionResults is a collection of `measures`. measures=None drives the
    DEFENSIVE branch for the property answering None: a CAM product that never recorded a probe
    reads a COUNT-0 CAMInspectionResults instead, and a document carrying no CAM product never
    reaches the read at all (itemByProductType raises)."""
    return types.SimpleNamespace(
        inspectionResults=None if measures is None else _NamedCollection(list(measures)))


def make_gated_cam(member="inspectionResults", text="preview feature is not enabled"):
    """A CAM product whose `member` RAISES on read - the gated-member shape (measured on
    stockMaterialLibrary), which is a different answer from the property reading None."""
    def _raise(self):
        raise RuntimeError(text)
    return type("_GatedCam", (), {member: property(_raise)})()


# ── CAM job world ─────────────────────────────────────────────────────────

@fusion_fake(live_type="CAMParameter",
             facts=("shape-dump-cam-job-world", "cam-parameter-expressions",
                    "cam-parameter-locked-write-lands", "cam-parameter-bad-reference"))
class FakeCAMParameter:
    """One CAM parameter as the CAM tools read it: name/title, the `expression` a write goes through
    and reads back (measured), the isEditable/isEnabled/isVisible flags a selection filters on,
    `value` - whose OWN .value is the payload, the second hop every value read makes - and the
    error/warning channels a post-write read consults (`warning` never gates; `error` does).

    The cam-parameter-bad-reference row is what `error` stands on: an expression naming a parameter
    that does not exist is stored verbatim and reports success, and only .error names the failure.
    An expression holding 'NoSuch' answers that text here, read from BEHAVIOR (the literal below is
    the fallback until that row's regen lands the key). Pass `error` to pin the channel instead,
    and `choices` to make .value answer getChoices() the way a ChoiceParameterValue does."""
    def __init__(self, name, expression="", value=None, title=None, editable=True, enabled=True,
                 visible=True, error=None, warning="", choices=None):
        self.name = name
        self._expression = expression
        self.value = types.SimpleNamespace(value=value)
        if choices is not None:
            # A ChoiceParameterValue answers getChoices() -> (ok, titles, values); every other
            # value class carries no such member at all.
            legal = list(choices)
            self.value.getChoices = lambda: (True, [str(v).title() for v in legal], list(legal))
        self.title = name if title is None else title
        self.isEditable = editable
        self.isEnabled = enabled
        self.isVisible = visible
        self._error = error
        self.warning = warning

    @property
    def error(self):
        if self._error is not None:
            return self._error
        expr = self._expression
        text = _api_facts.BEHAVIOR["cam_bad_reference_error_text"]
        return text if isinstance(expr, str) and "NoSuch" in expr else ""

    @property
    def expression(self):
        return self._expression

    @expression.setter
    def expression(self, value):
        # Measured: isEditable False does not make the platform drop a write - a locked parameter
        # takes it, expression and value both change. The flag is the fallback this fake falls to
        # only if that measurement ever reads False.
        if _api_facts.BEHAVIOR["cam_locked_parameter_write_lands"] or self.isEditable:
            self._expression = value


@fusion_fake(live_type="CAMParameters", facts=("shape-dump-cam-job-world",))
class FakeCAMParameters:
    """An operation's, setup's or tool's parameters: itemByName is the lookup every CAM read and
    write goes through, None on a miss."""
    def __init__(self, parameters=()):
        self._coll = _NamedCollection(list(parameters))

    @property
    def count(self):
        return self._coll.count

    def item(self, i):
        return self._coll.item(i)

    def itemByName(self, name):
        return self._coll.itemByName(name)


@fusion_fake(live_type="Tool", facts=("shape-dump-cam-job-world",
                                      "cam-tool-dimension-parameter-names"))
class FakeTool:
    """An operation's cutting tool: the `description` every tool row publishes, the `parameters`
    its dimensions and feeds are read by name from, the `presets` a preset read walks, and toJson()
    - the text a tool copy is rebuilt from."""
    def __init__(self, description="", parameters=None, presets=(), json_text="{}"):
        self.description = description
        self.parameters = FakeCAMParameters() if parameters is None else parameters
        self.presets = _NamedCollection(list(presets))
        self._json = json_text

    def toJson(self):
        return self._json


@fusion_fake(live_type="Machine", facts=("shape-dump-cam-job-world",
                                         "cam-machine-query-keyed-on-model"))
class FakeMachine:
    """A machine from the library or off a setup: the description/vendor/model a label is built
    from (adsk.cam.Machine has no .name), its id, the capabilities flags a kind list reads, the
    elements tree a limits read walks, and the hasPost/hasSimulationModel pair a create publishes."""
    def __init__(self, description="", vendor="", model="", machine_id=None, capabilities=None,
                 elements=None, has_post=False, has_simulation_model=False):
        self.description = description
        self.vendor = vendor
        self.model = model
        self.id = machine_id
        self.capabilities = capabilities
        self.elements = elements
        self.hasPost = has_post
        self.hasSimulationModel = has_simulation_model


@fusion_fake(live_type="SetupInput", facts=("shape-dump-cam-job-world",))
class FakeSetupInput:
    """The input cam.setups.createInput() hands back: the operationType it was created for, and the
    name/models/machine/stockMode a create assigns before add() consumes it."""
    def __init__(self, operation_type=None, parameters=None):
        self.operationType = operation_type
        self.name = ""
        self.models = []
        self.machine = None
        self.stockMode = None
        self.parameters = FakeCAMParameters() if parameters is None else parameters


@fusion_fake(live_type="Setups", facts=("shape-dump-cam-job-world", "shape-dump-cam-world"))
class FakeSetups:
    """cam.setups: the counted/by-name walk plus createInput/add. add() appends `new_setup` - or a
    FakeSetup named after the input - and hands it back, so a create read-back finds the setup in
    the walk the way live does."""
    def __init__(self, setups=(), new_setup=None, setup_input=None):
        self._setups = list(setups)
        self._new = new_setup
        self._input = setup_input
        self._added = []

    @property
    def count(self):
        return len(self._setups)

    def item(self, i):
        return _NamedCollection(self._setups).item(i)

    def itemByName(self, name):
        return _NamedCollection(self._setups).itemByName(name)

    def createInput(self, operation_type):
        return FakeSetupInput(operation_type) if self._input is None else self._input

    def add(self, setup_input):
        self._added.append(setup_input)
        setup = (self._new if self._new is not None
                 else FakeSetup(getattr(setup_input, "name", "") or "Setup1"))
        self._setups.append(setup)
        return setup


@fusion_fake(factory_for="FakeCAMParameters")
def make_cam_parameters(*rows):
    """A CAMParameters collection from (name, expression) or (name, expression, value) rows - the
    by-name lookup every CAM read and write goes through."""
    return FakeCAMParameters([FakeCAMParameter(*row) for row in rows])
