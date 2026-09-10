"""Unit tests for ``cam_generate.py`` - launching toolpath generation.

Covers the launch's real logic (no live Fusion): target resolution through the shared
``_cam_common.resolve_cam_node`` (setup/folder classification, the duplicate-name refusal,
not-found), the skip-valid short-circuit, the scope walk behind operations_to_generate, and the
entitlement pre-flight that launches around an operation reading isGenerationAllowed false. The
status read this launch hands completion to is pinned in test_cam_get_status.py.
"""

import json
from types import SimpleNamespace


from conftest import load_tool, make_cam
from conftest import FakeSetup as SharedSetup, FakeCAMFolder as SharedFolder, FakeOperation as SharedOp

gen = load_tool("cam_generate")
st = load_tool("cam_get_status")

# The generation registry lives in _cam_common - the ONE home the launch registers into and the
# status read polls; these names are that same object.
_GENERATIONS = gen._cam_common._GENERATIONS
_HANDLE_SEQ = gen._cam_common._HANDLE_SEQ


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── target resolution (via the shared _cam_common.resolve_cam_node) ─────────────────────────────────

def _FakeCAM(setups, machining_times=None):
    """A CAM product that RECORDS its launches: generate_calls holds ('target', obj) / ('all',
    skip_valid) in order. The machining-time answer is the shared one, borrowed not re-rolled."""
    cam = make_cam(*setups, machining_times=machining_times)
    cam.generate_calls = []

    def generate_toolpath(tgt):
        cam.generate_calls.append(("target", tgt))
        return SimpleNamespace(numberOfOperations=1)

    def generate_all(skip_valid):
        cam.generate_calls.append(("all", skip_valid))
        return SimpleNamespace(numberOfOperations=3)

    cam.generateToolpath = generate_toolpath
    cam.generateAllToolpaths = generate_all
    return cam


def _setup(name, ops=()):
    """A setup as the launch walks it. No machine member is set: cam_generate's blocked list is the
    entitlement one (isGenerationAllowed), and it never reads Setup.machine."""
    return SharedSetup(name, ops=ops)


class TestTargetResolution:
    """cam_generate's 'target' resolves through the shared _cam_common resolver - pin the
    classification, the folder reachability, and THE contract fix: a duplicated operation name is
    refused at this entry point, never first-matched."""

    def _install(self, monkeypatch, setups):
        cam = _FakeCAM(setups)
        monkeypatch.setattr(gen._cam_common, "get_cam", lambda: (cam, None))
        return cam

    def test_setup_target_launches_scoped_generation_ci(self, monkeypatch):
        setup = SharedSetup("Roughing", ops=[SharedOp("Face1", operation_state=1)])
        cam = self._install(monkeypatch, [setup])
        out = _payload(gen.handler(target="roughing"))   # case-insensitive exact
        assert out["launched"] is True
        assert cam.generate_calls == [("target", setup)]
        assert out["target"] == "setup 'roughing'"

    def test_scoped_launch_records_the_resolved_target_name(self, monkeypatch):
        # The handle carries the RAW resolved name (not just the display description), because the
        # status read settles completion on that target's OWN operations - see _handle_scope_state.
        _GENERATIONS.clear()
        setup = SharedSetup("Roughing", ops=[SharedOp("Face1", operation_state=1)])
        self._install(monkeypatch, [setup])
        out = _payload(gen.handler(target="roughing"))
        entry = _GENERATIONS[out["handle"]]
        assert entry["target_name"] == "Roughing" and entry["scope"] == "setup"

    def test_document_launch_records_no_target_name(self, monkeypatch):
        _GENERATIONS.clear()
        self._install(monkeypatch, [SharedSetup("S", ops=[SharedOp("Face1", operation_state=1)])])
        out = _payload(gen.handler(target=""))
        entry = _GENERATIONS[out["handle"]]
        assert entry["target_name"] == "" and entry["scope"] == "document"

    def test_folder_target_resolves_via_explicit_folder_walk(self, monkeypatch):
        # setup.allOperations DROPS folder containers (live-verified), so a folder target is only
        # reachable through the shared walk's explicit .folders recursion.
        folder = SharedFolder("Drilling", ops=[SharedOp("D1", operation_state=1)])
        setup = SharedSetup("S", folders=[folder])
        cam = self._install(monkeypatch, [setup])
        out = _payload(gen.handler(target="Drilling", skip_valid=False))
        assert out["launched"] is True
        assert cam.generate_calls == [("target", folder)]
        assert out["target"] == "folder 'Drilling'"

    def test_duplicate_op_name_across_setups_is_refused(self, monkeypatch):
        # "Drill1" exists in TWO setups - generating by that name must REFUSE with both setup
        # paths and launch NOTHING, never regenerate whichever op the walk met first.
        cam = self._install(monkeypatch, [SharedSetup("Setup1", ops=[SharedOp("Drill1")]),
                                          SharedSetup("Setup2", ops=[SharedOp("Drill1")])])
        res = gen.handler(target="Drill1", skip_valid=False)
        assert res["isError"] is True and "ambiguous" in res["message"].lower()
        assert "Setup1 / Drill1" in res["message"] and "Setup2 / Drill1" in res["message"]
        assert cam.generate_calls == []                      # nothing was launched


# ── generate_handler: scope selection + skip-valid short-circuit ────────────────────────────────────

class TestGenerateHandler:
    def test_whole_document_calls_generate_all(self, monkeypatch):
        # the setup carries an out-of-date operation: a scope with nothing to build is the
        # skipped payload, not a launch, so a launch test needs something in scope.
        cam = _FakeCAM([_setup("S", [SharedOp("Face1", operation_state=1)])])
        monkeypatch.setattr(gen._cam_common, "get_cam", lambda: (cam, None))
        out = _payload(gen.handler(target=""))
        assert out["launched"] is True
        assert cam.generate_calls[0][0] == "all"

    def test_target_not_found_errors(self, monkeypatch):
        cam = _FakeCAM([_setup("S", [SharedOp("Face1")])])
        import adsk.cam
        monkeypatch.setattr(adsk.cam.CAMFolder, "cast", staticmethod(lambda x: None))
        monkeypatch.setattr(adsk.cam.Operation, "cast", staticmethod(lambda x: x))
        monkeypatch.setattr(gen._cam_common, "get_cam", lambda: (cam, None))
        res = gen.handler(target="Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_skip_valid_short_circuits_already_valid_operation(self, monkeypatch):
        op = SharedOp("Face1", operation_state=0)              # 0 = valid/up-to-date
        cam = _FakeCAM([_setup("S", [op])])
        import adsk.cam
        monkeypatch.setattr(adsk.cam.CAMFolder, "cast", staticmethod(lambda x: None))
        monkeypatch.setattr(adsk.cam.Operation, "cast", staticmethod(lambda x: x))
        monkeypatch.setattr(gen._cam_common, "get_cam", lambda: (cam, None))
        out = _payload(gen.handler(target="Face1", skip_valid=True))
        assert out["launched"] is False and out["skipped"] is True
        assert cam.generate_calls == []          # never launched a generation

    def test_skip_valid_false_forces_regen_of_valid_op(self, monkeypatch):
        op = SharedOp("Face1", operation_state=0)
        cam = _FakeCAM([_setup("S", [op])])
        import adsk.cam
        monkeypatch.setattr(adsk.cam.CAMFolder, "cast", staticmethod(lambda x: None))
        monkeypatch.setattr(adsk.cam.Operation, "cast", staticmethod(lambda x: x))
        monkeypatch.setattr(gen._cam_common, "get_cam", lambda: (cam, None))
        out = _payload(gen.handler(target="Face1", skip_valid=False))
        assert out["launched"] is True
        assert cam.generate_calls[0][0] == "target"


# ── the launch hands completion to the status read, and claims none of its own ──────────────────


class TestLaunchHandsOffToTheStatusRead:
    """Generation runs in the background at its own pace once launched, so this call can only
    report the LAUNCH. The payload therefore names the read that settles completion and mints the
    handle that read is spent on - and asserts nothing about a toolpath being finished."""

    def test_the_launch_claims_no_completion_and_names_the_poller(self, monkeypatch):
        _GENERATIONS.clear()
        cam = _FakeCAM([SharedSetup("S", ops=[SharedOp("Face1", operation_state=1)])])
        monkeypatch.setattr(gen._cam_common, "get_cam", lambda: (cam, None))
        out = _payload(gen.handler(target=""))
        assert out["launched"] is True                       # the launch, and only the launch
        assert "completed" not in out and "generated" not in out
        assert "cam_get_status" in out["note"]               # where completion IS confirmed
        assert out["handle"] in _GENERATIONS             # the handle that read is spent on


class TestLaunchCountIsTheScopeWalk:
    """operations_to_generate names the operations THIS launch covers, walked off the resolved
    scope. The generation Future's own numberOfOperations counts some other collection (2 over a
    six-operation turning setup, measured), so it is never what the payload publishes."""

    def _install(self, monkeypatch, setups, future_count):
        import adsk.cam
        monkeypatch.setattr(adsk.cam.Operation, "cast", staticmethod(lambda x: x))
        cam = _FakeCAM(setups)
        cam.generateAllToolpaths = lambda skip_valid: SimpleNamespace(
            numberOfOperations=future_count)
        monkeypatch.setattr(gen._cam_common, "get_cam", lambda: (cam, None))
        return cam

    def _ops(self):
        return [SharedOp("Todo1", operation_state=1), SharedOp("Todo2", operation_state=1),
                SharedOp("Todo3", operation_state=1), SharedOp("Done", operation_state=0),
                SharedOp("Parked", operation_state=2, suppressed=True)]

    def test_the_document_count_is_the_scope_not_the_futures_number(self, monkeypatch):
        # THE BITE: the Future counts 13 while three operations in scope are out of date - its
        # counter is not this scope, so it is not what the payload publishes.
        _GENERATIONS.clear()
        self._install(monkeypatch, [SharedSetup("S", ops=self._ops())], 13)
        out = _payload(gen.handler(target="", skip_valid=True))
        assert out["operations_to_generate"] == 3
        _GENERATIONS.clear()

    def test_the_launch_names_each_operation_it_covers_and_why(self, monkeypatch):
        # MEASURED: skip_valid=true launched 11 operations where 5 were new - a count alone cannot
        # say whether the extra six were stale or the launch over-reached, so each covered operation
        # is named beside the state it read before the launch.
        _GENERATIONS.clear()
        self._install(monkeypatch, [SharedSetup("S", ops=[
            SharedOp("Stale", operation_state=1),
            SharedOp("New", operation_state=3),
            SharedOp("Done", operation_state=0),
            SharedOp("Parked", operation_state=2, suppressed=True)])], 13)
        out = _payload(gen.handler(target="", skip_valid=True))
        assert out["launch_reasons"] == {"out_of_date": 1, "no_toolpath": 1}
        assert out["launched_operations"] == [{"operation": "Stale", "reason": "out_of_date"},
                                              {"operation": "New", "reason": "no_toolpath"}]
        assert "launch_reasons tallies the state each operation read BEFORE" in out["note"]
        _GENERATIONS.clear()

    def test_a_forced_launch_says_the_valid_ones_were_forced(self, monkeypatch):
        # skip_valid=false is the one way a valid operation joins a launch; a reason of
        # 'out_of_date' over it would misdescribe the whole call.
        _GENERATIONS.clear()
        self._install(monkeypatch, [SharedSetup("S", ops=[
            SharedOp("Done", operation_state=0), SharedOp("Stale", operation_state=1)])], 13)
        out = _payload(gen.handler(target="", skip_valid=False))
        assert out["launch_reasons"] == {"valid_forced": 1, "out_of_date": 1}
        _GENERATIONS.clear()

    def test_a_scoped_launch_says_skip_valid_was_not_applied(self, monkeypatch):
        # MEASURED: cam.generateToolpath over a SETUP regenerated all four of its operations, two
        # already valid, under skip_valid=true - the flag narrows the DOCUMENT sweep only. Without
        # this the valid_forced rows read as a stale-state fault the caller would go hunting.
        _GENERATIONS.clear()
        self._install(monkeypatch, [SharedSetup("S", ops=[
            SharedOp("Done", operation_state=0), SharedOp("Stale", operation_state=1)])], 13)
        out = _payload(gen.handler(target="S", skip_valid=True))
        assert out["skip_valid_applied"] is False
        assert out["launch_reasons"] == {"valid_forced": 1, "out_of_date": 1}
        assert "skip_valid was requested but NOT applied" in out["note"]
        assert "regenerates its whole target whatever the flag says" in out["note"]
        _GENERATIONS.clear()

    def test_the_document_sweep_publishes_no_such_disclosure(self, monkeypatch):
        # the boundary: the flag DOES narrow a document sweep, so the key would be noise there.
        _GENERATIONS.clear()
        self._install(monkeypatch, [SharedSetup("S", ops=self._ops())], 13)
        out = _payload(gen.handler(target="", skip_valid=True))
        assert "skip_valid_applied" not in out
        assert "skip_valid was requested but NOT applied" not in out["note"]
        _GENERATIONS.clear()

    def test_the_named_rows_are_capped_and_the_overflow_is_flagged(self, monkeypatch):
        # A whole-document sweep can cover hundreds; a silently cut list would read as the complete
        # set of what is being rebuilt.
        _GENERATIONS.clear()
        monkeypatch.setattr(gen, "_LAUNCH_ROWS_CAP", 2)
        self._install(monkeypatch, [SharedSetup("S", ops=[
            SharedOp(f"Op{i}", operation_state=1) for i in range(4)])], 13)
        out = _payload(gen.handler(target="", skip_valid=True))
        assert len(out["launched_operations"]) == 2
        assert out["launched_operations_truncated"] is True
        assert out["launch_reasons"] == {"out_of_date": 4}     # the tally covers every one
        _GENERATIONS.clear()

    def test_the_walk_runs_before_the_launch_that_moves_the_state_it_reads(self, monkeypatch):
        # generateAllToolpaths marks every operation valid here. Counting after it would read the
        # states the launch just wrote and report 0 over a launch covering three.
        _GENERATIONS.clear()
        ops = self._ops()
        cam = self._install(monkeypatch, [SharedSetup("S", ops=ops)], 13)
        plain = cam.generateAllToolpaths

        def _launch_and_settle(skip_valid):
            for op in ops:
                op._operation_state = 0
            return plain(skip_valid)

        cam.generateAllToolpaths = _launch_and_settle
        out = _payload(gen.handler(target="", skip_valid=True))
        assert out["operations_to_generate"] == 3
        _GENERATIONS.clear()

    def test_a_scope_with_nothing_to_build_is_skipped_not_a_launch(self, monkeypatch):
        # launched=true with a count of 0 sends the caller to poll a generation nobody started.
        _GENERATIONS.clear()
        ops = [SharedOp("Done", operation_state=0),
               SharedOp("Parked", operation_state=2, suppressed=True)]
        cam = self._install(monkeypatch, [SharedSetup("S", ops=ops)], 13)
        out = _payload(gen.handler(target="", skip_valid=True))
        assert out["launched"] is False and out["skipped"] is True
        assert "1 already valid, 1 suppressed" in out["reason"]
        assert out["hint"] == "Pass skip_valid=false to force-regenerate the valid one(s)."
        assert "handle" not in out                    # no Future was minted
        assert cam.generate_calls == []               # and nothing was launched
        assert _GENERATIONS == {}

    def test_a_scope_holding_no_operations_says_so_and_names_the_create(self, monkeypatch):
        # '0 already valid, 0 suppressed' describes an exclusion that never happened - the scope is
        # simply empty, and the remedy is a create, not a flag.
        _GENERATIONS.clear()
        cam = self._install(monkeypatch, [SharedSetup("Empty")], 13)
        out = _payload(gen.handler(target="Empty"))
        assert out["launched"] is False and out["skipped"] is True
        assert out["reason"] == "no operations in scope - there is nothing to generate."
        assert "cam_create_operation" in out["hint"]
        assert "already valid" not in out["reason"] and "suppressed" not in out["reason"]
        assert cam.generate_calls == []

    def test_an_all_suppressed_scope_names_the_suppression_not_skip_valid(self, monkeypatch):
        # skip_valid excluded nothing here, so pointing at it would be a remedy that changes
        # nothing - the exclusion that emptied the scope is what the hint names.
        _GENERATIONS.clear()
        ops = [SharedOp("Parked", operation_state=2, suppressed=True)]
        self._install(monkeypatch, [SharedSetup("Roughing", ops=ops)], 13)
        out = _payload(gen.handler(target="Roughing", skip_valid=False))
        assert out["launched"] is False and "0 already valid, 1 suppressed" in out["reason"]
        assert "cam_edit_operation(suppressed=false)" in out["hint"]

    def test_skip_valid_false_counts_the_valid_op_and_never_the_suppressed_one(self, monkeypatch):
        # the boundary of the skip: state 0 joins the count only while skip_valid is off, and the
        # suppressed operation stays out either way - it has no toolpath to build.
        _GENERATIONS.clear()
        self._install(monkeypatch, [SharedSetup("S", ops=self._ops())], 13)
        out = _payload(gen.handler(target="", skip_valid=False))
        assert out["operations_to_generate"] == 4
        _GENERATIONS.clear()

    def test_a_setup_target_counts_every_unsuppressed_op_whatever_skip_valid_says(self, monkeypatch):
        # generateToolpath takes no skip_valid flag - it regenerates the whole target - so a count
        # narrowed by that flag would under-report what the platform is about to rebuild.
        _GENERATIONS.clear()
        ops = [SharedOp("Todo1", operation_state=1), SharedOp("Done", operation_state=0),
               SharedOp("Parked", operation_state=2, suppressed=True)]
        self._install(monkeypatch, [SharedSetup("Roughing", ops=ops)], 13)
        out = _payload(gen.handler(target="Roughing", skip_valid=True))
        assert out["operations_to_generate"] == 2
        _GENERATIONS.clear()


# ── the entitlement pre-flight ─────────────────────────────────────────────────────────────────────
#
# Measured: a whole-document generate holding two operations whose strategy reads
# isGenerationAllowed false regenerated NOTHING while three healthy out-of-date ops sat in the same
# scope, and targeting one of those healthy ops alone generated it in 2.6 s. So the launch reads the
# entitlement first, excludes the operations reading false, and launches the rest one at a time.

def _entitlement(table):
    """A ``_cam_common.strategy_generation_allowed`` stand-in: strategy name -> True / False / None.
    A name absent from the table reads None - the flag that would not read, which blocks nothing."""
    return lambda name: table.get(name)


class TestEntitlementPreflight:
    def _install(self, monkeypatch, setups, table):
        import adsk.cam
        monkeypatch.setattr(adsk.cam.Operation, "cast", staticmethod(lambda x: x))
        cam = _FakeCAM(setups)
        monkeypatch.setattr(gen._cam_common, "get_cam", lambda: (cam, None))
        monkeypatch.setattr(gen._cam_common, "strategy_generation_allowed", _entitlement(table))
        return cam

    def _launched(self, cam):
        return [tgt.name for kind, tgt in cam.generate_calls if kind == "target"]

    def test_a_blocked_op_is_excluded_and_the_healthy_ones_still_launch(self, monkeypatch):
        # THE BITE: the whole-document sweep is what regenerated nothing, so it must not be the call
        # made here - each operation that did not read false is launched on its own.
        _GENERATIONS.clear()
        setup = SharedSetup("S", ops=[SharedOp("Cham", operation_state=1, strategy="chamfer"),
                                      SharedOp("Face1", operation_state=1, strategy="face"),
                                      SharedOp("Face2", operation_state=1, strategy="face")])
        cam = self._install(monkeypatch, [setup], {"chamfer": False, "face": True})
        out = _payload(gen.handler(target=""))
        assert out["launched"] is True
        assert [kind for kind, _ in cam.generate_calls] == ["target", "target"]
        assert self._launched(cam) == ["Face1", "Face2"]
        assert out["entitlement_blocked"] == [{"name": "Cham", "strategy": "chamfer"}]
        # the split arm names what it launched too, and the excluded one is not in that list
        assert out["launched_operations"] == [{"operation": "Face1", "reason": "out_of_date"},
                                              {"operation": "Face2", "reason": "out_of_date"}]
        assert out["launch_reasons"] == {"out_of_date": 2}
        assert out["operations_to_generate"] == 2
        assert "isGenerationAllowed false" in out["note"] and "Cham" in out["note"]
        assert "Machining Extension" in out["note"]          # the remedy cam_create_operation uses
        assert "cam_get_status" in out["note"]               # this launch claims no completion either

    def test_one_handle_covers_every_future_the_split_launch_made(self, monkeypatch):
        # the futures must all stay REFERENCED: Fusion abandons an in-progress generation whose
        # Future is garbage-collected, so the registry entry holds them, not just the first.
        _GENERATIONS.clear()
        setup = SharedSetup("S", ops=[SharedOp("Cham", operation_state=1, strategy="chamfer"),
                                      SharedOp("Face1", operation_state=1, strategy="face"),
                                      SharedOp("Face2", operation_state=1, strategy="face")])
        self._install(monkeypatch, [setup], {"chamfer": False, "face": True})
        out = _payload(gen.handler(target=""))
        entry = _GENERATIONS[out["handle"]]
        assert len(entry["futures"]) == 2
        assert entry["future"] is entry["futures"][0]
        _GENERATIONS.clear()

    def test_every_op_blocked_refuses_and_launches_nothing(self, monkeypatch):
        setup = SharedSetup("S", ops=[SharedOp("Cham", operation_state=1, strategy="chamfer"),
                                      SharedOp("Walls", operation_state=1, strategy="inclined_walls")])
        cam = self._install(monkeypatch, [setup],
                            {"chamfer": False, "inclined_walls": False})
        res = gen.handler(target="")
        assert res["isError"] is True
        assert "Cham" in res["message"] and "Walls" in res["message"]
        assert "Machining Extension" in res["message"]
        assert cam.generate_calls == []

    def test_a_blocked_operation_target_is_refused_by_name(self, monkeypatch):
        setup = SharedSetup("S", ops=[SharedOp("Cham", operation_state=1, strategy="chamfer")])
        cam = self._install(monkeypatch, [setup], {"chamfer": False})
        res = gen.handler(target="Cham", skip_valid=False)
        assert res["isError"] is True and "isGenerationAllowed false" in res["message"]
        assert cam.generate_calls == []

    def test_an_unreadable_flag_excludes_nothing_and_is_disclosed(self, monkeypatch):
        # an unread flag is no entitlement verdict, so the plain document sweep still runs - and the
        # payload says the pre-flight was not made rather than staying silent about it.
        _GENERATIONS.clear()
        setup = SharedSetup("S", ops=[SharedOp("Odd", operation_state=1, strategy="mystery")])
        cam = self._install(monkeypatch, [setup], {})
        out = _payload(gen.handler(target=""))
        assert cam.generate_calls == [("all", True)]
        assert "entitlement_blocked" not in out
        assert out["entitlement_unread"] == 1 and "did not read" in out["note"]
        _GENERATIONS.clear()

    def test_the_split_launch_skips_the_valid_and_the_suppressed_ones(self, monkeypatch):
        _GENERATIONS.clear()
        ops = [SharedOp("Cham", operation_state=1, strategy="chamfer"),
               SharedOp("Done", operation_state=0, strategy="face"),
               SharedOp("Parked", operation_state=2, suppressed=True, strategy="face"),
               SharedOp("Todo", operation_state=1, strategy="face")]
        cam = self._install(monkeypatch, [SharedSetup("S", ops=ops)],
                            {"chamfer": False, "face": True})
        out = _payload(gen.handler(target="", skip_valid=True))
        assert self._launched(cam) == ["Todo"]
        assert out["operations_to_generate"] == 1
        _GENERATIONS.clear()

    def test_skip_valid_false_regenerates_the_valid_op_but_never_the_suppressed_one(self, monkeypatch):
        # the boundary of the skip test: state 0 is skipped only while skip_valid is set, and a
        # suppressed op is skipped either way (measured: generateAllToolpaths skips it too).
        _GENERATIONS.clear()
        ops = [SharedOp("Cham", operation_state=1, strategy="chamfer"),
               SharedOp("Done", operation_state=0, strategy="face"),
               SharedOp("Parked", operation_state=2, suppressed=True, strategy="face"),
               SharedOp("Todo", operation_state=1, strategy="face")]
        cam = self._install(monkeypatch, [SharedSetup("S", ops=ops)],
                            {"chamfer": False, "face": True})
        _payload(gen.handler(target="", skip_valid=False))
        assert self._launched(cam) == ["Done", "Todo"]
        _GENERATIONS.clear()

    def test_the_blocked_arms_skip_is_built_by_the_one_shared_builder(self, monkeypatch):
        # Two arms returning a hand-built skip drift: this one carries the entitlement rows and the
        # note ON TOP of the shared reason/hint, never a second wording of them.
        _GENERATIONS.clear()
        ops = [SharedOp("Cham", operation_state=1, strategy="chamfer"),
               SharedOp("Done", operation_state=0, strategy="face")]
        cam = self._install(monkeypatch, [SharedSetup("S", ops=ops)],
                            {"chamfer": False, "face": True})
        out = _payload(gen.handler(target="", skip_valid=True))
        shared = gen._nothing_to_launch("all setups", 0, 1)
        assert out["reason"] == shared["reason"] and out["hint"] == shared["hint"]
        assert out["entitlement_blocked"] == [{"name": "Cham", "strategy": "chamfer"}]
        assert "isGenerationAllowed false" in out["note"]
        assert cam.generate_calls == []

    def test_blocked_beside_only_valid_ops_launches_nothing_and_says_why(self, monkeypatch):
        ops = [SharedOp("Cham", operation_state=1, strategy="chamfer"),
               SharedOp("Done", operation_state=0, strategy="face")]
        cam = self._install(monkeypatch, [SharedSetup("S", ops=ops)],
                            {"chamfer": False, "face": True})
        out = _payload(gen.handler(target="", skip_valid=True))
        assert out["launched"] is False and cam.generate_calls == []
        assert "1 already valid" in out["reason"] and "0 suppressed" in out["reason"]
        assert out["entitlement_blocked"][0]["name"] == "Cham"

    def test_a_launch_that_raises_is_named_beside_the_ones_that_started(self, monkeypatch):
        # partial success is reported as partial: the two that started keep their handle, and the
        # one the platform refused is named rather than swallowed into the count.
        _GENERATIONS.clear()
        ops = [SharedOp("Cham", operation_state=1, strategy="chamfer"),
               SharedOp("Bad", operation_state=1, strategy="face"),
               SharedOp("Good", operation_state=1, strategy="face")]
        cam = self._install(monkeypatch, [SharedSetup("S", ops=ops)],
                            {"chamfer": False, "face": True})
        real = cam.generateToolpath

        def _launch(tgt):
            if tgt.name == "Bad":
                raise RuntimeError("3 : Toolpath requires tool to be selected.")
            return real(tgt)
        cam.generateToolpath = _launch
        out = _payload(gen.handler(target=""))
        assert out["launched"] is True and out["operations_to_generate"] == 1
        assert out["launch_failures"][0]["name"] == "Bad"
        assert "tool to be selected" in out["launch_failures"][0]["error"]
        _GENERATIONS.clear()

    def test_every_remaining_launch_failing_is_an_error_not_a_launched_payload(self, monkeypatch):
        ops = [SharedOp("Cham", operation_state=1, strategy="chamfer"),
               SharedOp("Bad", operation_state=1, strategy="face")]
        cam = self._install(monkeypatch, [SharedSetup("S", ops=ops)],
                            {"chamfer": False, "face": True})
        cam.generateToolpath = lambda tgt: None      # returns no future: nothing is running
        res = gen.handler(target="")
        assert res["isError"] is True and "Bad" in res["message"]

    def test_a_clean_scope_still_takes_the_plain_launch(self, monkeypatch):
        # no blocked op means no behaviour change: the document sweep and the scoped launch stay
        # the single measured calls they were.
        _GENERATIONS.clear()
        setup = SharedSetup("S", ops=[SharedOp("Face1", operation_state=1, strategy="face")])
        cam = self._install(monkeypatch, [setup], {"face": True})
        out = _payload(gen.handler(target=""))
        assert cam.generate_calls == [("all", True)]
        assert "entitlement_blocked" not in out and "entitlement_unread" not in out
        _GENERATIONS.clear()

    def test_the_scoped_readiness_names_the_blocked_op_instead_of_a_regenerate(self, monkeypatch):
        # the poll's own half of the row: 'run cam_generate to finish the rest' is circular advice
        # over an operation cam_generate excludes, so the verdict names it.
        ops = [SharedOp("Cham", operation_state=1, strategy="chamfer"),
               SharedOp("Face1", operation_state=0, strategy="face")]
        self._install(monkeypatch, [SharedSetup("S", ops=ops)], {"chamfer": False, "face": True})
        out = _payload(st.handler(target="S"))
        readiness = out["live_states"]["readiness"]
        assert "Cham" in readiness and "isGenerationAllowed false" in readiness
        assert "run cam_generate to finish the rest" not in readiness

    def test_a_stalled_poll_carries_the_readiness_that_names_them(self, monkeypatch):
        # the stall warning fires exactly where a blocked op parks: nothing generating, out-of-date
        # ops left. The verdict naming the blocked ops rides as its own key (an op name and its
        # reason are unbounded), and the note that warns points the reader at it.
        _GENERATIONS.clear()
        _GENERATIONS["gen1"] = {
            "future": SimpleNamespace(isGenerationCompleted=False, numberOfOperations=2,
                                      numberOfCompleted=0),
            "target": "all setups", "started_at": 0.0, "total": 2,
            "doc_name": "Doc", "doc_urn": "urn:doc", "doc_key": "urn:doc"}
        monkeypatch.setattr(st, "_active_identity", lambda: ("Doc", "urn:doc"))
        monkeypatch.setattr(st, "document_key", lambda: "urn:doc")
        monkeypatch.setattr(gen._cam_common, "live_readiness",
                            lambda: ({"valid": 0, "out_of_date": 2, "generating": 0, "total": 2,
                                      "readiness": "0 of 2 active ops valid; 1 of them read "
                                                   "isGenerationAllowed false"}, None))
        out = _payload(st.handler(handle="gen1"))
        assert out["completed"] is False and "WARNING" in out["note"]
        assert "isGenerationAllowed false" in out["readiness"]
        assert "'readiness' names what this installation will not generate" in out["note"]
        _GENERATIONS.clear()
