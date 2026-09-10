"""Unit tests for ``sys_request_selection.py`` - the hand-control-to-the-user hold.

The HOLD path (wait_seconds) is exercised with a FAKE TaskManager (a types.SimpleNamespace stand-in,
not a bespoke class - see _install_fake_task_manager) whose post() runs the marshaled main-thread
step synchronously on the test thread instead of a real cross-thread Fusion event loop. This proves
the orchestration (setup -> wait -> pick/timeout -> cleanup) without needing live Fusion threading;
the actual main-thread safety property (never blocking Fusion) is a live-verify concern, not a
unit-test one. The per-entity record itself is pinned in test__sys_common.py.
"""

import threading
import time
import types

import pytest

from conftest import (
    BRepBody,
    BRepFace,
    FakeOccurrence,
    FakePoint,
    FakeSelection,
    FakeSelections,
    FakeUserInterface,
    FakeVector3D,
    MakeComp,
    MakeDesign,
    Plane,
    load_tool,
    make_sketch,
)

sel = load_tool("sys_request_selection")


def _payload(result):
    import json
    return json.loads(result["content"][0]["text"])


class _ChangedEvent:
    """ui.activeSelectionChanged: the attach/detach pair a pick listener is registered through, and
    the roster every detach is checked against. Bespoke: the event object carries no shape dump, so
    there is no shared fake to stand for it."""

    def __init__(self):
        self.handlers = []

    def add(self, h):
        self.handlers.append(h)
        return True

    def remove(self, h):
        if h in self.handlers:
            self.handlers.remove(h)
            return True
        return False


def _fake_ui(entities=()):
    """A UserInterface holding one pick per entity plus the activeSelectionChanged event
    _begin_request/_PickHandler attach to."""
    picks = [FakeSelection(entity=e, point=FakePoint(0, 0, 0)) for e in entities]
    return FakeUserInterface(FakeSelections(picks), selection_changed=_ChangedEvent())


def _install_fake_task_manager(monkeypatch, run_immediately=True):
    """monkeypatch sel._task_manager to a synchronous stand-in: post(command, callback, data)
    invokes callback(data) immediately on the CALLING (test) thread instead of a real cross-thread
    Fusion marshal - exercises the handler's own orchestration without real Fusion threading.
    Returns the list of (command, data) pairs posted."""
    calls = []

    def _post(command, callback, data):
        calls.append((command, data))
        if run_immediately:
            callback(data)
        return "fake-task"

    ns = types.SimpleNamespace(is_running=lambda: True, start=lambda: True, post=_post,
                               cancel=lambda task_id: False)
    monkeypatch.setattr(sel, "_task_manager", lambda: ns)
    return calls


def _install_fake_pick_handler(monkeypatch):
    """monkeypatch sel._PickHandler to a plain, working stand-in with the same (box, done)
    constructor + notify(args) shape, delegating to the real _on_selection_changed logic.

    adsk.core.ActiveSelectionEventHandler is a bare Mock() under the unit-test harness, and
    `class _PickHandler(that_mock)` does not produce a real subclass there (Python's class
    statement degrades `class X(mock_instance)` to ANOTHER mock - confirmed empirically), so
    _PickHandler's own __init__/notify never run under test. This stand-in sidesteps that mock
    artifact while still exercising the real notify logic via _on_selection_changed."""
    class _Stub:
        def __init__(self, box, done):
            self._box, self._done = box, done

        def notify(self, args):
            sel._on_selection_changed(args, self._box, self._done)

    monkeypatch.setattr(sel, "_PickHandler", _Stub)


def _register_hold(ui, pending=True):
    """One hold's pick listener, registered the way _begin_request registers it: the handler goes on
    the event AND into its own box, which is what every detach is keyed to. pending=False leaves the
    process-wide slot alone - the shape of an ORPHAN whose hold has already ended. Returns
    (box, done)."""
    box, done = {}, threading.Event()
    handler = sel._PickHandler(box, done)
    box["handler"] = handler
    ui.activeSelectionChanged.add(handler)
    if pending:
        sel._pending["handler"] = handler
    return box, done


def _fake_pickable_design(bodies=1, sketches=0, occs=0):
    """A design just deep enough for _pickable_counts: the root's bodies and sketches, plus the
    occurrences the shared census ENUMERATES (each answering `component` as a real Occurrence
    does - one that raises is an unresolved reference)."""
    occ_rows = [FakeOccurrence(path=f"C{i}:1", component=MakeComp(name=f"C{i}"))
                for i in range(occs)]
    root = MakeComp(bodies=[f"B{i}" for i in range(bodies)],
                    sketches=[make_sketch(name=f"Sketch{i}") for i in range(sketches)],
                    occurrences=occ_rows)
    return MakeDesign(comp=root)


@pytest.fixture(autouse=True)
def _reset_pending_and_identity(monkeypatch):
    # Every test in this file gets a clean pending-request slot (module-level state _pending isn't
    # one of conftest's auto-restored seam attrs), a JSON-safe document identity (the raw adsk
    # mock's app.activeDocument.name is itself a non-serializable Mock), a PICKABLE design (the
    # nothing-to-select guard reads _active_design, and the raw adsk mock is not count-walkable),
    # and a working _PickHandler stand-in (see _install_fake_pick_handler).
    sel._pending["active"] = False
    sel._pending["handler"] = None
    sel._pending["started"] = None
    monkeypatch.setattr(sel._write_guard, "_active_identity", lambda: ("TestDoc", "urn:test:doc"))
    monkeypatch.setattr(sel, "_active_design", lambda: _fake_pickable_design())
    _install_fake_pick_handler(monkeypatch)
    yield
    sel._pending["active"] = False
    sel._pending["handler"] = None
    sel._pending["started"] = None


# ── _on_selection_changed: the notify() logic, tested directly (see _install_fake_pick_handler) ──

class TestOnSelectionChanged:
    def test_pick_captures_result_and_sets_done(self, monkeypatch):
        face = BRepFace(Plane(FakeVector3D(0, 0, 1)), centroid=FakePoint(1, 1, 1), entity_token="TOK1")
        ui = _fake_ui()
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        box, done = _register_hold(ui)

        picked = FakeSelection(entity=face, point=FakePoint(1, 1, 1))
        sel._on_selection_changed(types.SimpleNamespace(currentSelection=[picked]), box, done)

        assert done.is_set()
        assert box["result"]["selection_count"] == 1
        assert box["result"]["selections"][0]["handle"] == "TOK1|@face:1.000000,1.000000,1.000000"
        assert sel._pending["handler"] is None            # detached itself
        assert ui.activeSelectionChanged.handlers == []

    def test_empty_selection_keeps_waiting(self, monkeypatch):
        ui = _fake_ui()
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        box, done = {}, threading.Event()
        sel._on_selection_changed(types.SimpleNamespace(currentSelection=[]), box, done)
        assert not done.is_set()
        assert box == {}


# ── sys_request_selection: wait_seconds guard (<=300; the error names the value) ────────────────

class TestValidateWaitSeconds:
    def test_zero_is_valid(self):
        v, err = sel._validate_wait_seconds(0)
        assert err is None and v == 0.0

    def test_default_is_valid(self):
        v, err = sel._validate_wait_seconds(60)
        assert err is None and v == 60.0

    def test_max_bound_is_valid(self):
        v, err = sel._validate_wait_seconds(300)
        assert err is None and v == 300.0

    def test_negative_is_rejected_naming_the_value(self):
        v, err = sel._validate_wait_seconds(-5)
        assert v is None
        assert "-5" in err and "wait_seconds" in err

    def test_above_max_is_rejected_naming_the_value(self):
        v, err = sel._validate_wait_seconds(301)
        assert v is None
        assert "301" in err and "300" in err

    def test_non_numeric_is_rejected(self):
        v, err = sel._validate_wait_seconds("soon")
        assert v is None and "number" in err

    def test_handler_surfaces_the_guard_as_an_honest_error(self):
        res = sel.handler(wait_seconds=500)
        assert res["isError"] is True
        assert "500" in res["message"]


# ── sys_request_selection: wait_seconds=0 preserves the legacy fire-and-return ───────────────────

class TestRequestSelectionFireAndReturn:
    def test_wait_seconds_zero_behaves_like_legacy_fire_and_return(self, monkeypatch):
        _install_fake_task_manager(monkeypatch)
        ui = _fake_ui()
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        out = _payload(sel.handler(what="face", wait_seconds=0))
        assert out["status"] == "awaiting_selection"
        assert out["awaiting_user_selection"] is True
        assert out["cleared_previous_selection"] is True
        assert ui.activeSelections.count == 0
        assert sel._pending["handler"] is None   # no listener registered for the legacy path

    def test_clear_current_false_with_nothing_selected_still_awaits(self, monkeypatch):
        _install_fake_task_manager(monkeypatch)
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui())
        out = _payload(sel.handler(wait_seconds=0, clear_current=False))
        assert out["status"] == "awaiting_selection"
        assert out["cleared_previous_selection"] is None


# ── sys_request_selection: already-selected short-circuit (no wait needed) ───────────────────────

class TestRequestSelectionImmediatePick:
    def test_already_selected_short_circuits_without_waiting(self, monkeypatch):
        face = BRepFace(Plane(FakeVector3D(0, 0, 1)), centroid=FakePoint(1, 1, 1), entity_token="TOK1")
        _install_fake_task_manager(monkeypatch)
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui(entities=[face]))
        out = _payload(sel.handler(clear_current=False, wait_seconds=30))
        assert out["status"] == "picked"
        assert out["selections"][0]["handle"].startswith("TOK1|@face:")
        assert sel._pending["handler"] is None   # short-circuited - never registered a listener

    def test_an_unreadable_selection_in_the_immediate_pick_is_disclosed(self, monkeypatch):
        # iter_collection drops a selection whose item() raises; publishing the raw count beside
        # the shorter list would silently claim completeness, so the hole is disclosed.
        face = BRepFace(Plane(FakeVector3D(0, 0, 1)), centroid=FakePoint(1, 1, 1), entity_token="TOK1")
        _install_fake_task_manager(monkeypatch)
        ui = _fake_ui(entities=[face, face])
        sels = ui.activeSelections
        real_item = sels.item
        def item(i, _real=real_item):
            if i == 1:
                raise RuntimeError("4 : An API Object refers to a deleted Object")
            return _real(i)
        monkeypatch.setattr(sels, "item", item, raising=False)
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        out = _payload(sel.handler(clear_current=False, wait_seconds=30))
        assert out["status"] == "picked"
        assert out["selection_count"] == 2
        assert len(out["selections"]) == 1
        assert out["unread_selections"] == 1
        assert "could not be read" in out["note"]


# ── sys_request_selection: a pick DURING the wait completes the call in one shot ─────────────────

class TestRequestSelectionCompletedPick:
    def test_pick_during_wait_returns_handle_and_classification_in_one_call(self, monkeypatch):
        face = BRepFace(Plane(FakeVector3D(0, 0, 1)), centroid=FakePoint(3, 3, 3), entity_token="TOK3")
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui())

        def _post(command, callback, data):
            callback(data)   # runs _begin_request "on the main thread": registers the pick listener
            handler = sel._pending["handler"]
            assert handler is not None, "expected a pick listener while nothing is selected yet"
            picked = FakeSelection(entity=face, point=FakePoint(3, 3, 3))
            handler.notify(types.SimpleNamespace(currentSelection=[picked]))
            return "fake-task"

        monkeypatch.setattr(sel, "_task_manager", lambda: types.SimpleNamespace(
            is_running=lambda: True, start=lambda: True, post=_post, cancel=lambda t: False))

        out = _payload(sel.handler(what="face", wait_seconds=5))
        assert out["status"] == "picked"
        assert out["selection_count"] == 1
        # described the way sys_get_selection describes it (the SAME _classify()/_selection_record
        # shape) PLUS the minted handle.
        assert out["selections"][0]["kind"] == "face"
        assert out["selections"][0]["surface_type"] == "Plane"
        assert out["selections"][0]["handle"] == "TOK3|@face:3.000000,3.000000,3.000000"
        assert out["acted_on"] == {"name": "TestDoc", "document_id": "urn:test:doc"}
        assert sel._pending["handler"] is None   # detached itself after firing


# ── sys_request_selection: timeout is an honest ok(), never an error ─────────────────────────────

class TestRequestSelectionTimeout:
    def test_timeout_is_an_honest_non_error_ok(self, monkeypatch):
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui())   # nothing selected, nobody ever picks
        _install_fake_task_manager(monkeypatch)   # runs _begin_request, then _cancel_pending_wait

        res = sel.handler(wait_seconds=0.05)
        assert res["isError"] is False
        out = _payload(res)
        assert out["status"] == "timeout"
        assert out["waited_seconds"] == 0.05
        assert "not a tool defect" in out["note"]
        assert sel._pending["handler"] is None   # the timeout cleanup detached the listener
        # the cleanup reached the main thread, so there is nothing to disclose
        assert "listener_detached" not in out


# ── the two windows around an expiring wait: a late pick, and a cleanup that never ran ───────────

class TestRequestSelectionExpiryWindow:
    """The wait expiring is not proof nothing was picked. The listener fires on the MAIN thread and
    so does the cancel marshal, so a click landing between the two is captured in the box and is a
    real selection - reporting 'timeout' over it denies a pick the user made."""

    def _face(self):
        return BRepFace(Plane(FakeVector3D(0, 0, 1)), centroid=FakePoint(9, 9, 9), entity_token="TOK9")

    def test_a_pick_landing_as_the_wait_expires_wins_over_the_timeout(self, monkeypatch):
        face = self._face()
        ui = _fake_ui()
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        posts = []

        def _post(command, callback, data):
            posts.append(data)
            if len(posts) == 2:
                # the cancel marshal: the user clicked just after the wait gave up, and the pick
                # is captured before the listener is detached
                sel._pending["handler"].notify(types.SimpleNamespace(currentSelection=[
                    FakeSelection(entity=face, point=FakePoint(9, 9, 9))]))
            callback(data)
            return "fake-task"

        monkeypatch.setattr(sel, "_task_manager", lambda: types.SimpleNamespace(
            is_running=lambda: True, start=lambda: True, post=_post, cancel=lambda t: False))

        res = sel.handler(what="face", wait_seconds=0.05)
        out = _payload(res)
        assert res["isError"] is False
        assert out["status"] == "picked"
        assert out["selection_count"] == 1
        assert out["selections"][0]["handle"] == "TOK9|@face:9.000000,9.000000,9.000000"
        assert "expired" in out["note"]
        assert out["acted_on"] == {"name": "TestDoc", "document_id": "urn:test:doc"}

    def test_a_cleanup_that_never_reached_the_main_thread_is_disclosed(self, monkeypatch):
        ui = _fake_ui()
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        posts = []

        def _post(command, callback, data):
            posts.append(data)
            if len(posts) == 1:
                callback(data)          # _begin_request registers the listener
                return "fake-task"
            return None                 # the cancel never reaches Fusion's main thread

        monkeypatch.setattr(sel, "_task_manager", lambda: types.SimpleNamespace(
            is_running=lambda: True, start=lambda: True, post=_post, cancel=lambda t: False))

        out = _payload(sel.handler(wait_seconds=0.05))
        assert out["status"] == "timeout"
        assert out["listener_detached"] is False
        assert "still registered" in out["note"]
        # the claim is checkable: the listener really is still on the event, and still referenced
        assert len(ui.activeSelectionChanged.handlers) == 1
        assert sel._pending["handler"] is not None


# ── an ORPHAN listener may only ever detach ITSELF ───────────────────────────────────────────────

class TestOrphanPickListener:
    """A hold whose cleanup never ran leaves its listener registered. Detaching by whatever the
    pending slot happens to hold lets that orphan unhook the CURRENT hold's listener - a wait that
    can then never wake, only expire."""

    def test_an_orphan_firing_leaves_the_current_holds_listener_attached(self, monkeypatch):
        ui = _fake_ui()
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        orphan_box, orphan_done = _register_hold(ui, pending=False)
        current_box, current_done = _register_hold(ui)          # the live hold, registered second
        face = BRepFace(Plane(FakeVector3D(0, 0, 1)), centroid=FakePoint(1, 1, 1), entity_token="TOK1")

        orphan_box["handler"].notify(types.SimpleNamespace(currentSelection=[
            FakeSelection(entity=face, point=FakePoint(1, 1, 1))]))

        assert orphan_box["handler"] not in ui.activeSelectionChanged.handlers   # detached itself
        assert ui.activeSelectionChanged.handlers == [current_box["handler"]]    # and only itself
        assert sel._pending["handler"] is current_box["handler"]
        assert not current_done.is_set()        # the live hold can still be woken by a real pick
        assert orphan_done.is_set()             # the orphan's own dead box took the result

    def test_a_survivor_is_detached_before_the_new_hold_registers(self, monkeypatch):
        ui = _fake_ui()
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        orphan_box, _done = _register_hold(ui, pending=True)
        _install_fake_task_manager(monkeypatch)

        sel.handler(wait_seconds=0.05)

        assert orphan_box["handler"] not in ui.activeSelectionChanged.handlers
        assert ui.activeSelectionChanged.handlers == []   # nor did the new hold leave one behind


# ── sys_request_selection: only one wait may be pending at a time ────────────────────────────────

class TestRequestSelectionPendingGuard:
    def test_second_request_refused_while_one_is_pending(self):
        sel._pending["active"] = True
        sel._pending["started"] = time.monotonic()
        res = sel.handler(wait_seconds=1)
        assert res["isError"] is True
        assert "already waiting" in res["message"]

    def test_pending_flag_clears_after_a_timeout(self, monkeypatch):
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui())
        _install_fake_task_manager(monkeypatch)
        sel.handler(wait_seconds=0.05)
        assert sel._pending["active"] is False


# ── sys_request_selection: expect_document is honored (checked on the main thread) ───────────────

class TestRequestSelectionExpectDocument:
    def test_mismatched_expect_document_refuses_without_touching_selection(self, monkeypatch):
        monkeypatch.setattr(sel._write_guard, "_active_identity", lambda: ("Other.f3d", "urn:other"))
        ui = _fake_ui(entities=[BRepBody(name="X")])
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        _install_fake_task_manager(monkeypatch)
        res = sel.handler(expect_document="Expected.f3d", wait_seconds=0)
        assert res["isError"] is True
        assert "active_document_changed" in res["message"]
        assert ui.activeSelections.count == 1   # never cleared - refused before touching it

    def test_ambiguous_expect_document_name_refuses_with_candidates(self, monkeypatch):
        # A bare name shared by two OPEN docs must REFUSE here exactly like the wrapped write guard -
        # the active twin may not be the one meant. Gated through _write_guard._document_refusal, so
        # an ambiguous name lists the candidate URNs instead of proceeding against the active one.
        monkeypatch.setattr(sel._write_guard, "_active_identity", lambda: ("Twin.f3d", None))
        monkeypatch.setattr(sel._write_guard, "_open_documents", lambda: [
            {"name": "Twin.f3d", "document_id": "urn:a", "open_index": 0, "is_active": True},
            {"name": "Twin.f3d", "document_id": "urn:b", "open_index": 1, "is_active": False},
        ])
        ui = _fake_ui(entities=[BRepBody(name="X")])
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        _install_fake_task_manager(monkeypatch)
        res = sel.handler(expect_document="Twin.f3d", wait_seconds=0)
        assert res["isError"] is True
        assert "ambiguous_document_name" in res["message"]
        payload = _payload(res)
        assert {c["document_id"] for c in payload["candidates"]} == {"urn:a", "urn:b"}
        assert ui.activeSelections.count == 1   # never cleared - refused before touching it


# ── _call_on_main_thread: the marshal itself, independent of _begin_request ──────────────────────

class TestCallOnMainThread:
    def test_post_failure_reports_an_error_without_raising(self, monkeypatch):
        monkeypatch.setattr(sel, "_task_manager", lambda: types.SimpleNamespace(
            is_running=lambda: True, start=lambda: True,
            post=lambda command, callback, data: None, cancel=lambda t: False))
        out = sel._call_on_main_thread(lambda: 1, {})
        assert out.get("error")

    def test_marshal_timeout_cancels_the_task_and_reports(self, monkeypatch):
        monkeypatch.setattr(sel, "_task_manager", lambda: types.SimpleNamespace(
            is_running=lambda: True, start=lambda: True,
            post=lambda command, callback, data: "task-x",   # never invokes callback
            cancel=lambda t: True))
        out = sel._call_on_main_thread(lambda: 1, {}, timeout=0.05)
        assert "did not respond" in out.get("error", "")

    def test_a_raised_exception_propagates_to_the_caller(self, monkeypatch):
        def _boom():
            raise ValueError("kaboom")

        monkeypatch.setattr(sel, "_task_manager", lambda: types.SimpleNamespace(
            is_running=lambda: True, start=lambda: True,
            post=lambda command, callback, data: (callback(data), "task-1")[1],
            cancel=lambda t: False))
        with pytest.raises(ValueError, match="kaboom"):
            sel._call_on_main_thread(_boom, {})


# ── sys_request_selection: the nothing-to-select refusal ────────────────────────────────────────

class TestNothingToSelect:
    """_begin_request refuses BEFORE clearing the selection or registering a pick listener when the
    session has nothing pickable (no design, or an all-empty design), on the hold path and the
    wait_seconds=0 path alike - a request there could only time out."""

    def test_no_open_design_refuses_before_prompting(self, monkeypatch):
        _install_fake_task_manager(monkeypatch)
        ui = _fake_ui()
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        monkeypatch.setattr(sel, "_active_design", lambda: None)
        res = sel.handler(wait_seconds=5)
        assert res["isError"] is True
        assert "no design is open" in res["message"]
        assert ui.activeSelectionChanged.handlers == []   # refused before any listener existed

    def test_empty_design_refuses_naming_the_zero_counts(self, monkeypatch):
        _install_fake_task_manager(monkeypatch)
        ui = _fake_ui()
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        monkeypatch.setattr(sel, "_active_design", lambda: _fake_pickable_design(0, 0, 0))
        res = sel.handler(wait_seconds=5)
        assert res["isError"] is True
        assert "bodies=0" in res["message"] and "occurrences=0" in res["message"]
        assert ui.activeSelectionChanged.handlers == []

    def test_empty_design_refuses_the_fire_and_return_path_too(self, monkeypatch):
        _install_fake_task_manager(monkeypatch)
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui())
        monkeypatch.setattr(sel, "_active_design", lambda: _fake_pickable_design(0, 0, 0))
        res = sel.handler(wait_seconds=0)
        assert res["isError"] is True
        assert "bodies=0" in res["message"] and "occurrences=0" in res["message"]

    def test_sketch_only_design_is_pickable(self, monkeypatch):
        # A sketch-only doc (the unbodied parametric-plan handoff) has clickable sketch geometry -
        # the guard must not over-refuse it.
        _install_fake_task_manager(monkeypatch)
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui())
        monkeypatch.setattr(sel, "_active_design",
                            lambda: _fake_pickable_design(bodies=0, sketches=2, occs=0))
        out = _payload(sel.handler(wait_seconds=0))
        assert out["status"] == "awaiting_selection"

    def test_pickable_counts_walks_the_design_and_is_none_without_one(self, monkeypatch):
        monkeypatch.setattr(sel, "_active_design", lambda: None)
        assert sel._pickable_counts() is None
        monkeypatch.setattr(sel, "_active_design", lambda: _fake_pickable_design(2, 1, 3))
        assert sel._pickable_counts() == (2, 1, 3)


# ── sys_request_selection: what activeSelectionChanged.add()'s bool is allowed to decide ─────────

class TestListenerRegistrationAnswer:
    """activeSelectionChanged.add() returns a bool. Measured on Fusion 2705.1.4 it answered True for
    a fresh registration AND for a second add of the same handler, so True is what a working
    registration looks like and a False is worth acting on rather than discarding: only that
    listener wakes the hold, so one started on a false answer could only run out wait_seconds.

    The paired remove() answered True for a handler that was never registered - its return carries
    no information about what was removed - so no branch here reads it."""

    def _ui_answering(self, monkeypatch, answer, land_anyway=False):
        """A ui whose activeSelectionChanged.add() returns `answer`. land_anyway registers the
        handler regardless, which is how the refusal's cleanup gets something to clean up."""
        ui = _fake_ui()
        real_add = ui.activeSelectionChanged.add

        def _add(handler):
            if land_anyway:
                real_add(handler)
            return answer

        monkeypatch.setattr(ui.activeSelectionChanged, "add", _add)
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        return ui

    def test_false_refuses_instead_of_holding(self, monkeypatch):
        _install_fake_task_manager(monkeypatch)
        ui = self._ui_answering(monkeypatch, False)
        res = sel.handler(wait_seconds=5)
        assert res["isError"] is True
        assert "selection listener did not register" in res["message"]
        # the refusal says what the bool said, and does not narrate a cause for it
        assert "returned false" in res["message"]
        assert ui.activeSelectionChanged.handlers == []
        assert sel._pending["handler"] is None

    def test_the_false_refusal_returns_without_waiting_out_the_hold(self, monkeypatch):
        # The point of acting on the bool: a hold nothing can wake must not be entered at all. The
        # wait is kept short deliberately - a regression here BLOCKS for wait_seconds, so the number
        # is the cost of the red, and 4s is already far outside the refusal's own runtime.
        _install_fake_task_manager(monkeypatch)
        self._ui_answering(monkeypatch, False)
        started = time.monotonic()
        res = sel.handler(wait_seconds=4)
        assert res["isError"] is True
        assert time.monotonic() - started < 1.0

    def test_a_handler_that_landed_behind_a_false_is_detached_again(self, monkeypatch):
        # add() answering false while the handler DID land would leave a listener firing into a
        # hold nobody waits on; the refusal detaches what it registered before returning.
        _install_fake_task_manager(monkeypatch)
        ui = self._ui_answering(monkeypatch, False, land_anyway=True)
        assert sel.handler(wait_seconds=5)["isError"] is True
        assert ui.activeSelectionChanged.handlers == []

    def test_true_registers_the_listener_and_starts_the_hold(self, monkeypatch):
        # the other side of the branch: the answer Fusion actually gives, which must not refuse.
        _install_fake_task_manager(monkeypatch)
        ui = self._ui_answering(monkeypatch, True, land_anyway=True)
        res = sel.handler(wait_seconds=0.05)
        assert res["isError"] is False
        out = _payload(res)
        assert out["status"] == "timeout"          # nobody picked; the hold really was entered
        assert len(ui.activeSelectionChanged.handlers) == 0   # detached by the timeout cleanup

    def test_the_gate_keys_on_false_and_not_on_the_pending_slot(self, monkeypatch):
        # a refused registration must not leave the process-wide pending slot claimed, or the next
        # request would be refused by the one-at-a-time guard instead of trying again.
        _install_fake_task_manager(monkeypatch)
        self._ui_answering(monkeypatch, False)
        sel.handler(wait_seconds=5)
        assert sel._pending["active"] is False and sel._pending["handler"] is None
