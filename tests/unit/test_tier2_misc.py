"""Unit tests for assorted Tier-2 helpers: doc_update_xref, cam_generate.

Each tool has one or two pure helpers worth pinning:
  - doc_update_xref._ref_name      — safe name extraction with a fallback.
  - cam_generate._live_op_tally — the operation-state tally that drives the
    progress signal (valid / out_of_date / generating counts).
"""

import json as _json
from types import SimpleNamespace

from conftest import load_tool

uxref = load_tool("doc_update_xref")
# _cam_common is loaded inside each TestLiveReadiness test rather than at module top, so this file's
# import does not decide which module object the cam tests find already cached.


# ── doc_update_xref._ref_name ──────────────────────────────────────────────────

class TestRefName:
    def test_reads_datafile_name(self):
        ref = SimpleNamespace(dataFile=SimpleNamespace(name="Vise.f3d"))
        assert uxref._ref_name(ref) == "Vise.f3d"

    def test_missing_datafile_falls_back(self):
        # dataFile access raises -> the helper must not crash, returns "(unknown)".
        class Ref:
            @property
            def dataFile(self):
                raise RuntimeError("no data file")
        assert uxref._ref_name(Ref()) == "(unknown)"


# ── doc_update_xref.handler ────────────────────────────────────────────────

class _XRef:
    def __init__(self, name, ood=False, version=1, latest_returns=True, after_version=None):
        self.dataFile = SimpleNamespace(name=name)
        self.isOutOfDate = ood
        self.version = version
        self._latest_returns = latest_returns
        self._after = after_version if after_version is not None else version + 1

    def getLatestVersion(self):
        if self._latest_returns:
            self.version = self._after
            self.isOutOfDate = False
        return self._latest_returns


class _XRefColl:
    def __init__(self, refs):
        self._refs = list(refs)

    @property
    def count(self):
        return len(self._refs)

    def item(self, i):
        return self._refs[i]


def _install_xref(monkeypatch, refs):
    doc = SimpleNamespace(documentReferences=_XRefColl(refs))
    monkeypatch.setattr(uxref, "app", SimpleNamespace(activeDocument=doc))
    return doc


def _xref_payload(res):
    assert res["isError"] is False, res
    return _json.loads(res["content"][0]["text"])


class TestUpdateXrefHandler:
    def test_no_active_document(self, monkeypatch):
        monkeypatch.setattr(uxref, "app", SimpleNamespace(activeDocument=None))
        res = uxref.handler()
        assert res["isError"] is True and "No active document" in res["message"]

    def test_no_references_is_a_clean_noop(self, monkeypatch):
        _install_xref(monkeypatch, [])
        out = _xref_payload(uxref.handler())
        assert out["updated_count"] == 0
        assert "no external references" in out["note"].lower()

    def test_updates_out_of_date_ref(self, monkeypatch):
        _install_xref(monkeypatch, [_XRef("Vise.f3d", ood=True, version=2, after_version=5)])
        out = _xref_payload(uxref.handler())
        assert out["updated_count"] == 1
        u = out["updated"][0]
        assert u["name"] == "Vise.f3d"
        assert u["version_before"] == 2 and u["version_after"] == 5
        assert u["was_out_of_date"] is True

    def test_up_to_date_ref_is_skipped(self, monkeypatch):
        _install_xref(monkeypatch, [_XRef("Fresh.f3d", ood=False)])
        out = _xref_payload(uxref.handler())   # only_out_of_date default true
        assert out["updated_count"] == 0
        assert out["skipped"][0]["name"] == "Fresh.f3d"
        assert "up to date" in out["skipped"][0]["reason"]

    def test_force_refresh_when_only_out_of_date_false(self, monkeypatch):
        _install_xref(monkeypatch, [_XRef("Fresh.f3d", ood=False, version=3, after_version=4)])
        out = _xref_payload(uxref.handler(only_out_of_date=False))
        assert out["updated_count"] == 1
        assert out["updated"][0]["version_after"] == 4

    def test_name_filter_targets_one_ref(self, monkeypatch):
        _install_xref(monkeypatch, [
            _XRef("A.f3d", ood=True), _XRef("B.f3d", ood=True)])
        out = _xref_payload(uxref.handler(name="B.f3d"))
        assert {u["name"] for u in out["updated"]} == {"B.f3d"}

    def test_unknown_name_lists_available(self, monkeypatch):
        _install_xref(monkeypatch, [_XRef("A.f3d", ood=True)])
        res = uxref.handler(name="Ghost.f3d")
        assert res["isError"] is True
        assert "Ghost.f3d" in res["message"] and "A.f3d" in res["message"]

    def test_get_latest_false_is_an_error(self, monkeypatch):
        _install_xref(monkeypatch, [_XRef("A.f3d", ood=True, latest_returns=False)])
        res = uxref.handler()
        assert res["isError"] is True
        assert "failed to update" in res["message"]


# ── cam_generate._live_op_tally ──────────────────────────────────────

def _op(state, generating=False, progress=None, name="op", error=None):
    return SimpleNamespace(
        operationState=state, isGenerating=generating,
        generatingProgress=progress, name=name,
        hasError=error is not None, error=error or "",
    )


def _coll(items):
    class _C:
        count = len(items)

        def item(self, i):
            return items[i]
    return _C()


def _cam_with(ops, setup_error=None, programs=(), machine=SimpleNamespace(description="Haas VF-2")):
    """Fake CAM with a single setup holding the given operations. setup_error makes the SETUP itself
    hasError; programs is a list of NC programs (each a SimpleNamespace with hasError/error/name).
    allOperations is the count/item collection shape (_coll) - the measured live protocol
    _cam_common.walk_operations reads (live_readiness's op walk), not a plain Python list.
    The setup carries an assigned machine by default: setup_blockers reads Setup.machine, so a
    machine-less fake is a setup blocked by no_machine_selected, not a clean job."""
    setup = SimpleNamespace(allOperations=_coll(list(ops)), name="Setup1",
                            hasError=setup_error is not None, error=setup_error or "",
                            machine=machine)
    return SimpleNamespace(setups=_coll([setup]), ncPrograms=_coll(list(programs)))


def _ncp(name, error=None):
    return SimpleNamespace(name=name, hasError=error is not None, error=error or "")


class TestLiveReadiness:
    """_cam_common.live_readiness - the SINGLE CAM health/readiness signal (was cam_generate._live_op_tally,
    folded into _cam_common so cam_get and cam_get_status share ONE source instead of re-deriving it)."""

    def test_counts_states(self, monkeypatch):
        ccom = load_tool("_cam_common")
        # states: 0=valid, 1/3=out_of_date, 2=suppressed
        ops = [_op(0), _op(0), _op(1), _op(3), _op(2)]
        monkeypatch.setattr(ccom, "get_cam", lambda: (_cam_with(ops), None))
        sig, err = ccom.live_readiness()
        assert err is None
        assert sig["valid"] == 2
        assert sig["out_of_date"] == 2     # one invalid + one no_toolpath
        assert sig["suppressed"] == 1
        assert sig["total"] == 5

    def test_active_op_captured_with_real_progress(self, monkeypatch):
        ccom = load_tool("_cam_common")
        ops = [_op(1, generating=True, progress="Pending", name="queued"),
               _op(1, generating=True, progress="42.0%", name="running")]
        monkeypatch.setattr(ccom, "get_cam", lambda: (_cam_with(ops), None))
        sig, _ = ccom.live_readiness()
        assert sig["generating"] == 2
        # The op with real progress wins over the "Pending" one.
        assert sig["active"]["op"] == "running"
        assert sig["active"]["progress"] == "42.0%"

    def test_errored_op_bucketed_separately_not_as_ood(self, monkeypatch):
        ccom = load_tool("_cam_common")
        # an errored op (hasError) is its OWN bucket - NOT counted as out_of_date/generating (it will
        # never finish); samples.op carries name + first error line, and readiness is a BLOCKER.
        ops = [_op(0), _op(1, error="Top height must not be below the bottom height\nOn the Heights tab...",
                       name="Rough to Model Top"), _op(1)]
        monkeypatch.setattr(ccom, "get_cam", lambda: (_cam_with(ops), None))
        sig, _ = ccom.live_readiness()
        assert sig["errored"] == 1
        assert sig["out_of_date"] == 1          # only the non-errored state-1 op
        assert sig["valid"] == 1
        assert sig["samples"]["op"]["name"] == "Rough to Model Top"
        # only the FIRST line of the error is carried (the signal; full text is the per-op record)
        assert sig["samples"]["op"]["error"] == "Top height must not be below the bottom height"
        assert sig["readiness"].startswith("BLOCKER:")

    def test_setup_level_error_tallied(self, monkeypatch):
        ccom = load_tool("_cam_common")
        # a faulted SETUP blocks the job even with clean ops - surfaced as setups_errored + a sample.
        ops = [_op(0)]
        monkeypatch.setattr(ccom, "get_cam",
                            lambda: (_cam_with(ops, setup_error="WCS orientation is invalid"), None))
        sig, _ = ccom.live_readiness()
        assert sig["setups_errored"] == 1
        assert sig["programs_errored"] == 0
        assert sig["samples"]["setup"]["error"] == "WCS orientation is invalid"
        assert "setup" in sig["readiness"].lower() and sig["readiness"].startswith("BLOCKER:")

    def test_nc_program_level_error_tallied(self, monkeypatch):
        ccom = load_tool("_cam_common")
        # a faulted NC PROGRAM (no post config / no ops) blocks posting - surfaced as programs_errored.
        ops = [_op(0)]
        progs = [_ncp("Main", error="No post configuration selected")]
        monkeypatch.setattr(ccom, "get_cam", lambda: (_cam_with(ops, programs=progs), None))
        sig, _ = ccom.live_readiness()
        assert sig["programs_errored"] == 1
        assert sig["samples"]["program"]["name"] == "Main"
        assert "NC program" in sig["readiness"]

    def test_clean_job_is_ready(self, monkeypatch):
        ccom = load_tool("_cam_common")
        monkeypatch.setattr(ccom, "get_cam",
                            lambda: (_cam_with([_op(0), _op(0)], programs=[_ncp("Main")]), None))
        sig, _ = ccom.live_readiness()
        assert sig["setups_errored"] == 0 and sig["programs_errored"] == 0
        assert sig["samples"]["op"] is None
        assert sig["setups_blocked"] == []
        # the whole sentence, not a substring: the blocked verdict CONTAINS "ready to post" inside
        # "'ready to post' is NOT established", so a substring check passes on a blocked job too.
        assert sig["readiness"] == "2 of 2 active ops valid - ready to post."

    def test_cam_unavailable_returns_error(self, monkeypatch):
        ccom = load_tool("_cam_common")
        monkeypatch.setattr(ccom, "get_cam", lambda: (None, "no CAM"))
        sig, err = ccom.live_readiness()
        assert sig is None and err == "no CAM"
