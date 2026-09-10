"""Unit tests for ``drawing_export.py`` - export the ACTIVE 2D drawing to PDF, DXF or DWG on disk.

Covers: the three formats and their factories/extensions, the format-scoped option refusals
(sheet range and line weights are PDF-only, dwg_variant DWG-only, splines_as_splines DXF-only), the
DWG format member selected by NAME off the live enum, the active-doc-must-be-a-drawing guard, path
defaulting + extension, and the file-landed gate - execute() returning true is NOT proof, and the
wait POLLS because the write can finish after the call returns.
No live Fusion.
"""

import json

import pytest

import adsk  # the mock package conftest installed at import time
import live_api_facts
from conftest import FakeDrawingExportManager, load_tool, make_drawing, make_drawing_session

# The MEASURED DWGFormats family: the tool selects a member by name off the live enum (Simplified is
# 0 - falsy - so truthiness cannot carry it).
_DWG_FORMATS = live_api_facts.ENUMS["drawing.DWGFormats"]

# The factory defaults an options object reads back, and the assignments a SWIG proxy can silently
# swallow - only a value that DIFFERS from its default exposes the dropped write.
_OPTION_DEFAULTS = {"format": None, "exportSplinesAsSplines": False, "useLineWeights": True,
                    "sheetRange": "", "openPDF": False}

de = load_tool("drawing_export")

# The SHIPPED wait, read before the fixture below shortens it for the test clock.
_SHIPPED_DEADLINE_S = de._LAND_DEADLINE_S


@pytest.fixture(autouse=True)
def shortened_wait(monkeypatch):
    """Shorten the bounded file-landed wait for the test clock: its loop and its give-up branch are
    unchanged, only the clock is, and monkeypatch undoes both with the test."""
    monkeypatch.setattr(de, "_LAND_DEADLINE_S", 0.5)
    monkeypatch.setattr(de, "_LAND_POLL_SLEEP", 0.001)


@pytest.fixture
def install(monkeypatch):
    """Install an active document (a drawing unless asked otherwise) and hand back its export
    manager. The active-document seam is adsk.core.Application.get - the ONE read _drawing_common's
    cast goes through - and monkeypatch owns it, so the patch is undone with the test."""
    def _install(*, active_doc="drawing"):
        em = FakeDrawingExportManager()
        if active_doc == "drawing":
            active_doc = make_drawing(export_manager=em)
        elif active_doc == "notdrawing":
            active_doc = object()
        make_drawing_session(monkeypatch, active_doc)
        return em, active_doc
    return _install


def _ignores_every_assignment(em):
    """Turn the manager's options bag into the SWIG proxy that ACCEPTS an assignment and keeps the
    factory default - the shape the tool's read-back exists to catch."""
    em._drops = tuple(_OPTION_DEFAULTS)
    em._seeded = dict(_OPTION_DEFAULTS)


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


kernel = load_tool("_assert")


def _run(**kwargs):
    """Call the handler through the SAME FileLanded postcondition the Item declares - the file-landed
    gate (and the size_bytes evidence) live in the kernel too, not only in the handler."""
    return kernel.wrap(de.handler, [kernel.FileLanded("file_path")])(**kwargs)


# ── happy path ────────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_exports_pdf_and_verifies_file(self, install, tmp_path):
        install()
        out = _payload(_run(format="pdf", file_path=str(tmp_path / "sheet.pdf")))
        assert out["exported"] is True
        assert out["size_bytes"] > 0
        assert out["file_path"].lower().endswith(".pdf")

    def test_extension_appended_when_missing(self, install, tmp_path):
        em, _ = install()
        out = _payload(_run(format="pdf", file_path=str(tmp_path / "noext")))
        assert out["file_path"].lower().endswith(".pdf")
        assert em._opts.path.lower().endswith(".pdf")

    def test_declared_returns_are_present(self, install, tmp_path):
        install()
        out = _payload(_run(file_path=str(tmp_path / "d.pdf")))
        for spec in de.RETURNS:
            assert spec.assert_present(out) == "", spec.assert_present(out)


# ── the three formats: factory + extension ────────────────────────────────────

class TestFormats:
    def test_all_three_formats_are_on_the_wire(self):
        prop = dict(de._FORMAT.as_property()[1])
        assert prop["enum"] == ["pdf", "dxf", "dwg"]

    def test_dxf_uses_the_dxf_factory_and_extension(self, install, tmp_path):
        em, _ = install()
        out = _payload(_run(format="dxf", file_path=str(tmp_path / "plate")))
        assert em._factory_used == "createDXFExportOptions"
        assert out["file_path"].lower().endswith(".dxf")
        assert out["format"] == "dxf"

    def test_dwg_uses_the_dwg_factory_and_extension(self, install, tmp_path):
        em, _ = install()
        out = _payload(_run(format="dwg", file_path=str(tmp_path / "plate")))
        assert em._factory_used == "createDWGExportOptions"
        assert out["file_path"].lower().endswith(".dwg")
        assert out["format"] == "dwg"

    def test_unknown_format_rejected(self, install, tmp_path):
        install()
        res = _run(format="svg", file_path=str(tmp_path / "x.svg"))
        assert res["isError"] is True
        assert "svg" in res["message"] and "dxf" in res["message"]


# ── DXF options ───────────────────────────────────────────────────────────────

class TestDxfOptions:
    def test_splines_as_splines_lands_on_the_options_and_is_reported(self, install, tmp_path):
        em, _ = install()
        out = _payload(_run(format="dxf", file_path=str(tmp_path / "s.dxf"),
                            splines_as_splines=True))
        assert em._opts.exportSplinesAsSplines is True
        assert out["splines_as_splines"] is True

    def test_splines_default_is_false(self, install, tmp_path):
        em, _ = install()
        out = _payload(_run(format="dxf", file_path=str(tmp_path / "d.dxf")))
        assert em._opts.exportSplinesAsSplines is False
        assert out["splines_as_splines"] is False

    def test_an_option_that_does_not_take_is_an_error(self, install, tmp_path):
        em, _ = install()
        _ignores_every_assignment(em)
        res = _run(format="dxf", file_path=str(tmp_path / "i.dxf"), splines_as_splines=True)
        assert res["isError"] is True
        assert "did not take" in res["message"].lower()


# ── DWG variant: the member is chosen by NAME off the live enum ───────────────

class TestDwgVariant:
    def test_default_variant_is_autocad(self, install, tmp_path):
        em, _ = install()
        out = _payload(_run(format="dwg", file_path=str(tmp_path / "a.dwg")))
        assert em._opts.format == _DWG_FORMATS["AutoCADDWGFormat"]
        assert out["dwg_variant"] == "autocad"

    def test_simplified_selects_the_simplified_member(self, install, tmp_path):
        em, _ = install()
        out = _payload(_run(format="dwg", file_path=str(tmp_path / "s.dwg"),
                            dwg_variant="simplified"))
        assert em._opts.format == _DWG_FORMATS["SimplifiedDWGFormat"]
        assert out["dwg_variant"] == "simplified"

    def test_unknown_variant_rejected(self, install, tmp_path):
        install()
        res = _run(format="dwg", file_path=str(tmp_path / "x.dwg"), dwg_variant="acad2000")
        assert res["isError"] is True
        assert "acad2000" in res["message"] and "simplified" in res["message"]

    def test_missing_enum_member_is_reported_not_guessed(self, install, tmp_path, monkeypatch):
        install()
        monkeypatch.delattr(adsk.drawing, "DWGFormats")
        res = _run(format="dwg", file_path=str(tmp_path / "n.dwg"))
        assert res["isError"] is True
        assert "not available on this fusion version" in res["message"].lower()


# ── format-scoped options: refused for the wrong format, naming the value ─────

class TestFormatScoping:
    def test_sheet_range_refused_for_dxf(self, install, tmp_path):
        install()
        res = _run(format="dxf", file_path=str(tmp_path / "r.dxf"), sheet_range="1-2")
        assert res["isError"] is True
        assert "1-2" in res["message"] and "format=pdf" in res["message"]

    def test_sheet_range_refused_for_dwg(self, install, tmp_path):
        install()
        res = _run(format="dwg", file_path=str(tmp_path / "r.dwg"), sheet_range="3")
        assert res["isError"] is True
        assert "'sheet_range=3'" in res["message"]

    def test_line_weights_refused_for_dxf(self, install, tmp_path):
        install()
        res = _run(format="dxf", file_path=str(tmp_path / "l.dxf"), line_weights=False)
        assert res["isError"] is True
        assert "line_weights" in res["message"] and "format=pdf" in res["message"]

    def test_dwg_variant_refused_for_pdf(self, install, tmp_path):
        install()
        res = _run(format="pdf", file_path=str(tmp_path / "v.pdf"), dwg_variant="simplified")
        assert res["isError"] is True
        assert "dwg_variant=simplified" in res["message"] and "format=dwg" in res["message"]

    def test_splines_refused_for_pdf(self, install, tmp_path):
        install()
        res = _run(format="pdf", file_path=str(tmp_path / "p.pdf"), splines_as_splines=True)
        assert res["isError"] is True
        assert "splines_as_splines" in res["message"] and "format=dxf" in res["message"]

    def test_each_option_is_accepted_by_its_own_format(self, install, tmp_path):
        install()
        assert _payload(_run(format="pdf", file_path=str(tmp_path / "ok.pdf"),
                             sheet_range="1", line_weights=False))["line_weights"] is False
        install()
        assert _payload(_run(format="dxf", file_path=str(tmp_path / "ok.dxf"),
                             splines_as_splines=True))["splines_as_splines"] is True
        install()
        assert _payload(_run(format="dwg", file_path=str(tmp_path / "ok.dwg"),
                             dwg_variant="simplified"))["dwg_variant"] == "simplified"


# ── sheet selection + line weights (PDF) ──────────────────────────────────────

class TestSheetSelection:
    def test_sheet_range_is_passed_to_options_and_echoed(self, install, tmp_path):
        em, _ = install()
        out = _payload(_run(file_path=str(tmp_path / "r.pdf"), sheet_range="1-2,5"))
        assert em._opts.sheetRange == "1-2,5"      # range set on the export options
        assert out["sheet_range"] == "1-2,5"

    def test_no_range_defaults_to_all_and_sets_no_range(self, install, tmp_path):
        em, _ = install()
        out = _payload(_run(file_path=str(tmp_path / "a.pdf")))
        assert out["sheet_range"] == "all"
        assert not hasattr(em._opts, "sheetRange")  # empty range leaves the all-sheets default

    def test_line_weights_default_true_and_togglable(self, install, tmp_path):
        em, _ = install()
        out = _payload(_run(file_path=str(tmp_path / "lw.pdf")))
        assert em._opts.useLineWeights is True
        assert out["line_weights"] is True
        em2, _ = install()
        out2 = _payload(_run(file_path=str(tmp_path / "lw2.pdf"), line_weights=False))
        assert em2._opts.useLineWeights is False
        assert out2["line_weights"] is False

    def test_openpdf_is_forced_false(self, install, tmp_path):
        em, _ = install()
        _payload(_run(file_path=str(tmp_path / "o.pdf")))
        assert em._opts.openPDF is False           # never auto-open (would drive UI we can't dismiss)

    def test_a_pdf_option_that_does_not_take_is_an_error(self, install, tmp_path):
        # The PDF options read back (measured), so a dropped assignment is caught here too - not
        # only on the DXF/DWG branches.
        em, _ = install()
        _ignores_every_assignment(em)
        res = _run(file_path=str(tmp_path / "i.pdf"), line_weights=False)
        assert res["isError"] is True
        assert "did not take" in res["message"].lower()

    def test_a_sheet_range_that_does_not_take_is_an_error(self, install, tmp_path):
        em, _ = install()
        _ignores_every_assignment(em)
        res = _run(file_path=str(tmp_path / "i2.pdf"), sheet_range="2")
        assert res["isError"] is True
        assert "sheet_range" in res["message"]


# ── the file-landed gate (the load-bearing honesty check) ─────────────────────

class TestFileLandedGate:
    def test_execute_true_but_no_file_is_a_failure(self, install, tmp_path):
        em, _ = install()
        em._writes = False           # execute() lies: returns true, writes nothing
        res = _run(format="pdf", file_path=str(tmp_path / "ghost.pdf"))
        assert res["isError"] is True
        assert "no file was written" in res["message"].lower()

    def test_a_file_that_lands_late_is_not_a_false_failure(self, install, tmp_path, monkeypatch):
        # Measured: the write can finish AFTER execute() returns, so the gate polls. verify_written
        # reports nothing on the first two looks and the file appears on the third.
        em, _ = install()
        em._writes = False
        real = de._export.verify_written
        state = {"calls": 0}

        def late(path, before=None):
            state["calls"] += 1
            if state["calls"] == 3:
                with open(path, "w") as f:
                    f.write("LATE")
            return real(path, before)

        monkeypatch.setattr(de._export, "verify_written", late)
        out = _payload(_run(format="dwg", file_path=str(tmp_path / "late.dwg")))
        assert state["calls"] >= 3                # it kept looking instead of stat-ing once
        assert out["size_bytes"] > 0

    def test_the_wait_gives_up_and_reports_the_miss(self, install, tmp_path, monkeypatch):
        em, _ = install()
        em._writes = False
        real = de._export.verify_written
        state = {"calls": 0}

        def counted(path, before=None):
            state["calls"] += 1
            return real(path, before)

        monkeypatch.setattr(de._export, "verify_written", counted)
        res = _run(format="dwg", file_path=str(tmp_path / "never.dwg"))
        assert res["isError"] is True
        assert state["calls"] > 1                 # bounded polling, not a single stat
        assert "no file was written" in res["message"].lower()
        # The premise of the wait: the write had not finished when the call returned.
        assert "of the export call returning" in res["message"]

    def test_a_stale_pre_existing_file_is_not_this_exports_landing(self, install, tmp_path):
        # A non-empty file of the right name from an EARLIER export reports a stable size on the
        # first two samples, so the settle gate alone would report it as this call's deliverable.
        # The wait carries the pre-export snapshot, so an unchanged file never settles.
        em, _ = install()
        em._writes = False                 # execute() lies: returns true, writes nothing
        path = tmp_path / "stale.pdf"
        path.write_text("a PDF from an earlier export")
        res = _run(format="pdf", file_path=str(path))
        assert res["isError"] is True
        assert "already there before this call" in res["message"]

    def test_a_pre_existing_zero_byte_file_is_not_a_landing(self, install, tmp_path):
        em, _ = install()
        em._writes = False
        path = tmp_path / "empty.dwg"
        path.write_bytes(b"")            # the file EXISTS but carries no export
        res = _run(format="dwg", file_path=str(path))
        assert res["isError"] is True
        assert "size_bytes=0" in res["message"]

    def test_a_size_still_climbing_is_not_reported_as_landed(self, install, tmp_path, monkeypatch):
        # Two consecutive EQUAL samples are the gate, so a size read mid-write is never the one
        # reported: the sizes below climb 10 -> 20 before holding.
        install()
        sizes = [10, 20]
        monkeypatch.setattr(de._export, "verify_written",
                            lambda path, before=None: (sizes.pop(0) if sizes else 20, None))
        out = _payload(_run(format="dwg", file_path=str(tmp_path / "grow.dwg")))
        assert out["size_bytes"] == 20            # the settled size, not the first non-zero one

    def test_a_file_that_never_settles_is_refused(self, install, tmp_path, monkeypatch):
        install()
        state = {"size": 0}

        def growing(path, before=None):
            state["size"] += 10       # never two equal samples in a row
            return state["size"], None

        monkeypatch.setattr(de._export, "verify_written", growing)
        res = _run(format="dwg", file_path=str(tmp_path / "forever.dwg"))
        assert res["isError"] is True
        assert "still growing" in res["message"]

    def test_execute_false_is_a_failure(self, install, tmp_path):
        em, _ = install()
        em._execute_ok = False
        em._writes = False
        res = _run(format="pdf", file_path=str(tmp_path / "x.pdf"))
        assert res["isError"] is True
        assert "returned false" in res["message"].lower()

    def test_execute_false_is_a_failure_even_when_a_file_did_land(self, install, tmp_path):
        # the two gates are SEPARATE: execute() answering false is a failure on its own, whatever
        # the disk says. A gate that consulted the file would settle on the landed bytes here and
        # publish exported=true over a write Fusion said it did not make.
        em, _ = install()
        em._execute_ok = False            # execute() writes the file AND answers false
        path = tmp_path / "landed.pdf"
        res = _run(format="pdf", file_path=str(path))
        assert path.exists() and path.stat().st_size > 0     # the bytes DID land
        assert res["isError"] is True
        assert "returned false" in res["message"].lower()
        # the file-landed wait's own wordings are what a disk-consulting gate would answer instead
        assert "no file was written" not in res["message"]
        assert "still growing" not in res["message"]

    def test_export_exception_is_reported(self, install, tmp_path):
        em, _ = install()
        em._raises = RuntimeError("disk full")
        res = _run(format="pdf", file_path=str(tmp_path / "x.pdf"))
        assert res["isError"] is True
        assert "disk full" in res["message"]


class TestWaitForFile:
    """_wait_for_file pinned directly - the handler's gate is only as honest as this returns."""

    def test_a_missing_file_returns_zero_and_the_reason(self, tmp_path):
        path = str(tmp_path / "absent.dxf")
        size, err = de._wait_for_file(path)
        assert size == 0
        assert path in err and "no file was written" in err

    def test_a_settled_file_returns_its_size_and_no_error(self, tmp_path):
        path = tmp_path / "there.dxf"
        path.write_text("SETTLED")
        size, err = de._wait_for_file(str(path))
        assert err is None
        assert size == len("SETTLED")

    def test_a_file_that_keeps_growing_times_out_in_this_tools_own_wording(self, tmp_path,
                                                                          monkeypatch):
        # the size never settles across two samples, so the bounded wait gives up - and the give-up
        # sentence is drawing_export's own, not the shared pump's.
        path = tmp_path / "growing.pdf"
        path.write_text("x")
        monkeypatch.setattr(de, "_LAND_DEADLINE_S", 0.05)
        monkeypatch.setattr(adsk, "doEvents",
                            lambda: path.write_text(path.read_text() + "xx"), raising=False)
        size, err = de._wait_for_file(str(path))
        assert size == 0
        assert "still growing" in err and str(path) in err

    def test_the_shipped_wait_is_long_enough_for_the_measured_landing(self):
        # The measured in-call landing is ~3s; a wait that short would false-fail a real export.
        assert _SHIPPED_DEADLINE_S >= 10


# ── the preview-feature disclosure (DXF/DWG option creators only) ─────────────

class TestPreviewNote:
    def test_dxf_and_dwg_notes_disclose_the_preview_banner(self, install, tmp_path):
        install()
        assert "preview" in _payload(_run(format="dxf",
                                          file_path=str(tmp_path / "p.dxf")))["note"].lower()
        install()
        assert "preview" in _payload(_run(format="dwg",
                                          file_path=str(tmp_path / "p.dwg")))["note"].lower()

    def test_pdf_note_carries_no_preview_claim(self, install, tmp_path):
        install()
        assert "preview" not in _payload(_run(format="pdf",
                                              file_path=str(tmp_path / "p.pdf")))["note"].lower()


# ── sheet coverage: DXF/DWG write ONE sheet, PDF is the multi-sheet channel ───

class TestSingleSheetCoverage:
    def test_dxf_and_dwg_notes_state_single_sheet_coverage(self, install, tmp_path):
        install()
        assert "SINGLE sheet" in _payload(_run(format="dxf",
                                               file_path=str(tmp_path / "s.dxf")))["note"]
        install()
        assert "SINGLE sheet" in _payload(_run(format="dwg",
                                               file_path=str(tmp_path / "s.dwg")))["note"]

    def test_the_note_names_the_first_sheet_and_denies_the_active_one(self, install, tmp_path):
        # WHICH sheet is the load-bearing half: the measured DXF of an 8-sheet drawing carried
        # sheet index 0 and NOT the active sheet, so a caller who reads "the active sheet" here
        # exports the wrong one and cannot tell from the payload.
        install()
        note = _payload(_run(format="dxf", file_path=str(tmp_path / "which.dxf")))["note"]
        assert "first sheet" in note
        assert "not the active one" in note

    def test_pdf_note_claims_no_single_sheet_limit(self, install, tmp_path):
        install()
        note = _payload(_run(format="pdf", file_path=str(tmp_path / "s.pdf")))["note"]
        assert "SINGLE sheet" not in note and "all sheets" in note


# ── guards ────────────────────────────────────────────────────────────────────

class TestGuards:
    def test_active_doc_not_a_drawing_errors(self, install, tmp_path):
        install(active_doc="notdrawing")
        res = _run(format="pdf", file_path=str(tmp_path / "x.pdf"))
        assert res["isError"] is True
        assert "not a drawing" in res["message"].lower()

    def test_the_not_a_drawing_error_routes_through_doc_open_not_a_human(self, install, tmp_path):
        # a drawing never reviewed in the Fusion UI opens and exports through the API (measured on
        # 2705.0.87), so the refusal names the tool that opens it, not an operator
        install(active_doc="notdrawing")
        msg = _run(format="pdf", file_path=str(tmp_path / "x.pdf"))["message"]
        assert "doc_open opens it by file_id" in msg
        assert "reviewed" not in msg and "Fusion UI" not in msg

    def test_missing_path_errors(self, install):
        install()
        res = _run(format="pdf")
        assert res["isError"] is True
        assert "file_path" in res["message"]

    def test_missing_path_names_the_formats_extension(self, install):
        install()
        res = _run(format="dwg")
        assert res["isError"] is True
        assert ".dwg" in res["message"]


class TestTheDescriptionAsksForNoHumanStep:
    """Measured on 2705.0.87: a drawing never reviewed in the Fusion UI opens headlessly and exports,
    so the description states the API route instead of parking the agent on an operator."""

    def _description(self):
        return de.tool.to_dict()["description"]

    def test_it_names_doc_open_as_the_route(self):
        desc = self._description()
        assert "doc_open by file_id" in desc
        # no human step is asked for: the UI is not on the wire at all
        assert "Fusion UI" not in desc

    def test_it_makes_no_never_reviewed_blocking_claim(self):
        desc = self._description()
        assert "never-reviewed" not in desc
        assert "reviewed drawing" not in desc

    def test_it_takes_no_drawing_id(self):
        # the tool still does not open anything - a scope fact the schema itself carries
        props = de.tool.to_dict()["inputSchema"]["properties"]
        assert "file_id" not in props and "drawing" not in props


class TestSheetRangeCarriesTheWedgeFact:
    """A sheet_range export issued soon after another export of the same drawing blocks the main
    thread and never lands its file, while one run FIRST completes clean. The input carries that
    ordering rule - a caller cannot recover the lost time or the missing file from the result."""

    def _sheet_range_description(self):
        return de.tool.to_dict()["inputSchema"]["properties"]["sheet_range"]["description"]

    def test_the_input_states_the_block(self):
        desc = self._sheet_range_description()
        assert "block the main thread" in desc

    def test_the_input_states_the_trigger_ordering(self):
        # the block is a follow-up-export collision, not an inherent single-sheet defect
        desc = self._sheet_range_description()
        assert "after another export" in desc

    def test_the_input_states_the_honest_severity(self):
        # the deliverable never lands - the half a caller cannot recover from the result
        desc = self._sheet_range_description()
        assert "never lands" in desc

    def test_the_input_teaches_run_first(self):
        desc = self._sheet_range_description()
        assert "FIRST" in desc
