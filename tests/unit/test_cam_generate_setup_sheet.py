"""Unit tests for ``cam_generate_setup_sheet.py`` - setup sheets with the file-landed gate.

The fakes mirror the measured contract: generateSetupSheet/generateAllSetupSheets return True
IMMEDIATELY while the sheet file lands only later, advanced by adsk.doEvents() pumps - so a
handler that trusted the bool would report a deliverable that does not exist yet.
"""

import itertools
import json
import types

import adsk.cam
import adsk.core
import pytest

from conftest import FakeSetup, load_tool, make_cam

gs = load_tool("cam_generate_setup_sheet")

_FORMATS = adsk.cam.SetupSheetFormats

# The SHIPPED bound, read before any test shortens it.
_PUMP_SECONDS_SHIPPED = gs._PUMP_SECONDS

_PARTIAL, _FINAL = 120, 800


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """Wire a CAM product with one setup; doEvents advances the sheet's async write."""
    def _make(result=True, quiet_pumps=1, sizes=(0, _PARTIAL, _FINAL), sheet_name="Untitled.html"):
        setup = FakeSetup("SheetSetup")
        cam = make_cam(setup)
        cam.calls, cam.written = [], []
        state = {"folder": None, "quiet_left": quiet_pumps, "sizes": iter(sizes)}

        def generate(kind, target, fmt, folder, open_doc):
            cam.calls.append((kind, target, fmt, folder, open_doc))
            if result:
                state["folder"] = folder
            return result

        cam.generateSetupSheet = lambda t, f, d, o: generate("one", t, f, d, o)
        cam.generateAllSetupSheets = lambda f, d, o: generate("all", None, f, d, o)

        def pump():
            # The bool answers True at once; the FILE is written one `sizes` stage per doEvents
            # cycle. Measured: the sheet appears at 0 bytes and is written after; the non-zero
            # PARTIAL stage is a size a write still in flight can be sampled at.
            if state["folder"] is None:
                return
            if state["quiet_left"] > 0:              # generation is still working; nothing on disk
                state["quiet_left"] -= 1
                return
            size = next(state["sizes"], None)
            if size is None:                         # the write is done - the file stops changing
                return
            (tmp_path / sheet_name).write_text("x" * size)
            cam.written.append(size)

        monkeypatch.setattr(gs, "get_cam", lambda: (cam, None))

        def fake_resolve(c, want, kinds, label):
            if want == "SheetSetup":
                return types.SimpleNamespace(obj=setup, kind="setup"), None
            return None, f"No CAM setup/folder/operation named '{want}'."
        monkeypatch.setattr(gs, "resolve_cam_node", fake_resolve)
        monkeypatch.setattr(adsk, "doEvents", pump, raising=False)
        monkeypatch.setattr(gs.time, "sleep", lambda s: None)
        return {"cam": cam, "setup": setup}
    return _make


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


def _message(res):
    assert res["isError"] is True, res
    return res["content"][0]["text"]


class TestRouting:
    def test_document_scope_routes_to_all_setups(self, rig, tmp_path):
        r = rig()
        out = _payload(gs.handler(output_folder=str(tmp_path)))
        assert r["cam"].calls[0][0] == "all"
        assert out["scope"] == "document" and out["scope_kind"] == "document"

    def test_a_named_setup_routes_to_that_object(self, rig, tmp_path):
        r = rig()
        out = _payload(gs.handler(scope="SheetSetup", output_folder=str(tmp_path)))
        kind, target, _fmt, _folder, _open = r["cam"].calls[0]
        assert kind == "one" and target is r["setup"]
        assert out["scope"] == "SheetSetup" and out["scope_kind"] == "setup"

    def test_open_document_is_always_false(self, rig, tmp_path):
        # the API default True opens the sheet in the UI - a window the calling agent cannot close
        r = rig()
        _payload(gs.handler(output_folder=str(tmp_path)))
        assert r["cam"].calls[0][4] is False

    def test_excel_maps_to_the_excel_enum(self, rig, tmp_path):
        r = rig(sheet_name="Untitled.xlsx")
        out = _payload(gs.handler(format="excel", output_folder=str(tmp_path)))
        assert r["cam"].calls[0][2] == _FORMATS.ExcelFormat
        assert out["format"] == "excel" and out["file_path"].endswith(".xlsx")

    def test_the_default_format_maps_to_the_html_enum(self, rig, tmp_path):
        r = rig()
        _payload(gs.handler(output_folder=str(tmp_path)))
        assert r["cam"].calls[0][2] == _FORMATS.HTMLFormat

    def test_an_unknown_scope_is_refused_before_generating(self, rig, tmp_path):
        r = rig()
        msg = _message(gs.handler(scope="NoSuchSetup", output_folder=str(tmp_path)))
        assert "NoSuchSetup" in msg and r["cam"].calls == []


class TestFileLandedGate:
    def test_the_async_landing_is_pumped_and_the_file_reported(self, rig, tmp_path):
        r = rig(quiet_pumps=3)
        out = _payload(gs.handler(output_folder=str(tmp_path)))
        assert out["generated"] is True
        assert out["file_path"].endswith("Untitled.html")
        assert out["overwrote_existing"] is False
        assert r["cam"].written == [0, _PARTIAL, _FINAL]      # every stage was pumped through

    def test_a_partial_write_is_never_reported_as_the_deliverable(self, rig, tmp_path):
        # a sheet sampled while its write is in flight can read a non-zero size that is not the
        # final one, so a gate that reports the first non-empty sample can publish a half-written
        # sheet as the deliverable.
        rig()
        out = _payload(gs.handler(output_folder=str(tmp_path)))
        assert out["size_bytes"] == _FINAL
        assert (tmp_path / "Untitled.html").stat().st_size == _FINAL

    def test_a_sheet_that_keeps_growing_is_refused_at_the_bound(self, rig, tmp_path, monkeypatch):
        # non-empty on every sample but never the same size twice: the write is still in flight, so
        # there is no settled deliverable to report.
        rig(sizes=itertools.count(100, 10))
        monkeypatch.setattr(gs, "_PUMP_SECONDS", 0.05)
        msg = _message(gs.handler(output_folder=str(tmp_path)))
        assert "no html sheet landed" in msg and "no deliverable" in msg

    def test_a_sheet_stuck_at_zero_bytes_is_refused_not_reported(self, rig, tmp_path, monkeypatch):
        # the file APPEARS and then never grows: a stable size is not enough, an empty sheet is not
        # a deliverable.
        rig(sizes=(0,))
        monkeypatch.setattr(gs, "_PUMP_SECONDS", 0.05)
        msg = _message(gs.handler(output_folder=str(tmp_path)))
        assert "no html sheet landed" in msg and "no deliverable" in msg
        assert (tmp_path / "Untitled.html").stat().st_size == 0

    def test_true_with_nothing_landing_is_an_error_not_a_success(self, rig, tmp_path, monkeypatch):
        # the bool is True but the async write never completes - trusting it reports a deliverable
        # that does not exist
        rig(sizes=())
        monkeypatch.setattr(gs, "_PUMP_SECONDS", 0.0)
        msg = _message(gs.handler(output_folder=str(tmp_path)))
        assert "no html sheet landed" in msg and "no deliverable" in msg

    def test_a_generation_that_ate_the_budget_leaves_no_wait_for_the_landing(self, rig, tmp_path,
                                                                             monkeypatch):
        # _PUMP_SECONDS bounds generation AND landing together: a generation that already spent the
        # budget must not then get the full budget again to wait in.
        rig()
        clock = types.SimpleNamespace(calls=[], sleep=lambda s: None)

        def spent():
            clock.calls.append(1)
            return 0.0 if len(clock.calls) == 1 else _PUMP_SECONDS_SHIPPED + 2.0
        clock.time = spent
        monkeypatch.setattr(gs, "time", clock)
        msg = _message(gs.handler(output_folder=str(tmp_path)))
        assert "no html sheet landed" in msg

    def test_a_declined_generation_is_an_error_naming_the_scope(self, rig, tmp_path):
        rig(result=False)
        msg = _message(gs.handler(scope="SheetSetup", output_folder=str(tmp_path)))
        assert "returned false" in msg and "SheetSetup" in msg

    def test_overwriting_a_previous_sheet_is_disclosed(self, rig, tmp_path):
        # the file is named after the DOCUMENT, so a second call into the folder clobbers the
        # first (measured live: 108,660 -> 103,739 bytes) - the payload must say so
        rig()
        (tmp_path / "Untitled.html").write_text("old sheet")
        out = _payload(gs.handler(output_folder=str(tmp_path)))
        assert out["overwrote_existing"] is True
        assert "OVER an existing sheet" in out["note"]

    def test_missing_output_folder_is_refused(self, rig):
        rig()
        msg = _message(gs.handler())
        assert "output_folder" in msg

    def test_a_document_with_no_cam_product_is_an_isError_result(self, monkeypatch, tmp_path):
        # get_cam hands back a bare reason STRING; returning it unwrapped puts a raw string on the
        # wire with no content block and no isError, so the caller reads a failure as a success.
        monkeypatch.setattr(gs, "get_cam",
                            lambda: (None, "This document has no CAM (Manufacture) product yet - "
                                           "call view_switch_workspace('manufacture') once."))
        res = gs.handler(output_folder=str(tmp_path))
        assert res["isError"] is True, res
        assert "no CAM (Manufacture) product" in res["content"][0]["text"]
