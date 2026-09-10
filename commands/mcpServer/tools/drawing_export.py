# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Export the ACTIVE 2D drawing document to a local PDF, DXF or DWG file; the caller opens the
drawing first (doc_open / the Fusion UI). DrawingExportManager.execute returning true is NOT proof
the file is on disk yet - a Simplified DWG can stay absent for seconds - so the file-landed gate
WAITS for the write rather than stat-ing once. WRITES a file."""

import os

from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register
from ._common import ok, error, safe, set_verified
from . import _assert
from . import _drawing_common
from . import _export
from . import _inputs
from . import _outputs

# What this tool RETURNS (declared once; drives the PRODUCES: prose + the assert-present contract test).
RETURNS = [
    _outputs.ReturnsValue("file_path", "the local path of the exported drawing file"),
    _outputs.ReturnsValue("size_bytes", "the exported file size in bytes (proof it landed on disk)"),
]

# format -> (file extension, DrawingExportManager factory, carries Autodesk's preview banner). The
# DXF and DWG option creators are banner-marked; Drawing/Sheet/PDF export are not.
_FORMATS = {
    "pdf": (".pdf", "createPDFExportOptions", False),
    "dxf": (".dxf", "createDXFExportOptions", True),
    "dwg": (".dwg", "createDWGExportOptions", True),
}

_FORMAT = _inputs.Choice("format", list(_FORMATS), default="pdf")

# dwg_variant -> the DWGFormats member name. The member is read off the live enum by NAME so no
# integer value is written here.
_DWG_MEMBERS = {"simplified": "SimplifiedDWGFormat", "autocad": "AutoCADDWGFormat"}

_DWG_VARIANT = _inputs.Choice("dwg_variant", list(_DWG_MEMBERS), required=False)

# input name -> the ONE format whose export options carry that setting: PDFExportOptions holds
# sheetRange/sheetsToExport/useLineWeights, DXFExportOptions exportSplinesAsSplines, DWGExportOptions
# format. An option aimed at another format has nothing to set, and is refused, never dropped.
_SCOPED = (("sheet_range", "pdf"), ("line_weights", "pdf"),
           ("dwg_variant", "dwg"), ("splines_as_splines", "dxf"))

_PREVIEW_NOTE = ("The DXF and DWG export option creators are marked a preview feature by Autodesk "
                 "and may change in a future release.")

# DXF and DWG write ONE sheet, measured: the DXF of an 8-sheet drawing carried only sheet index 0's
# name, and the ACTIVE sheet was absent. PDF is the multi-sheet channel (sheetRange selects).
_SINGLE_SHEET_NOTE = ("DXF and DWG cover a SINGLE sheet - measured as the first sheet, not the "
                      "active one. Export PDF for other sheets (sheet_range selects them).")

# The write is still in flight when execute() returns, so the file-landed check polls to this
# deadline instead of reporting a false failure on the first miss.
_LAND_DEADLINE_S = 20.0
_LAND_POLL_SLEEP = 0.25


def _scope_error(fmt, given):
    """The refusal for a format-scoped option supplied alongside a different format. `given` maps an
    input name to its raw value; None or '' means the caller did not supply it."""
    for name, owner in _SCOPED:
        value = given.get(name)
        if value is None or value == "" or fmt == owner:
            continue
        return (f"'{name}={value}' is a format={owner} option, but this call asked for "
                f"format={fmt} - the {fmt.upper()} export options carry no such setting. Drop "
                f"'{name}', or export with format={owner}.")
    return ""


def _dwg_format_member(variant):
    """The DWGFormats member for a dwg_variant key, or None when this Fusion build does not carry
    it (set_verified reports the absence rather than exporting a different flavour)."""
    member_name = _DWG_MEMBERS.get(variant)
    return _drawing_common.enum_value("DWGFormats", member_name) if member_name else None


def _apply_options(fmt, opts, rng, line_weights, variant, splines):
    """Set the chosen format's options on a freshly-created *ExportOptions, each one read back:
    (payload_fields, error). An option that does not read back is an error, not a silent drop - the
    file would carry settings the call did not ask for."""
    if fmt == "pdf":
        weights = True if line_weights is None else bool(line_weights)
        # openPDF is forced off: opening the file drives UI we cannot dismiss headlessly.
        sets = [("useLineWeights", weights, "line_weights"), ("openPDF", False, "openPDF")]
        if rng:
            # sheetRange auto-switches sheetsToExport to Range; leaving it unset keeps all sheets.
            sets.insert(0, ("sheetRange", rng, "sheet_range"))
        fields = {"sheet_range": rng or "all", "line_weights": weights}
    elif fmt == "dxf":
        want = bool(splines)
        sets = [("exportSplinesAsSplines", want, "splines_as_splines")]
        fields = {"splines_as_splines": want}
    else:
        sets = [("format", _dwg_format_member(variant), f"the {variant} DWG format")]
        fields = {"dwg_variant": variant}
    owner = fmt.upper() + "ExportOptions"
    for prop, value, label in sets:
        serr = set_verified(opts, prop, value, label, owner)
        if serr:
            return None, serr
    return fields, ""


def _wait_for_file(path, before=None):
    """Poll until a non-empty file at `path` that THIS call wrote reports the SAME size on two
    consecutive samples, bounded by _LAND_DEADLINE_S: (size_bytes, error_or_None). execute() returns
    true while the file is still absent, and it appears once the main thread is pumped. `before` (the
    path's snapshot()) belongs INSIDE the loop, or a stale file settles the wait on its own size."""
    prev = None

    def probe():
        nonlocal prev
        size, verr = _export.verify_written(path, before)
        stable = not verr and size == prev
        prev = None if verr else size
        return stable, (size, verr)

    stable, (size, verr) = _export.pump_until(probe, _LAND_DEADLINE_S, _LAND_POLL_SLEEP)
    if stable:
        return size, None
    return 0, verr or f"the file at '{path}' was still growing (size_bytes={size})"


def handler(format: str = "pdf", file_path: str = "", sheet_range: str = "",
            line_weights: bool = None, dwg_variant: str = "",
            splines_as_splines: bool = None) -> dict:
    """See TOOL_DESCRIPTION."""
    fmt, e = _FORMAT.resolve(format)
    if e:
        return error(e)
    ext, factory_name, is_preview = _FORMATS[fmt]

    rng = (sheet_range or "").strip()
    variant_raw = (dwg_variant or "").strip()
    scope_err = _scope_error(fmt, {"sheet_range": rng, "line_weights": line_weights,
                                   "dwg_variant": variant_raw,
                                   "splines_as_splines": splines_as_splines})
    if scope_err:
        return error(scope_err)
    # 'autocad' when omitted, which is NOT a schema default: _scope_error refuses a dwg_variant on
    # another format, so sending 'autocad' is not the same call as omitting it.
    variant, e = _DWG_VARIANT.resolve(variant_raw or "autocad")
    if e:
        return error(e)

    path = (file_path or "").strip().strip('"')
    if not path:
        return error(f"Provide 'file_path' - the local output path for the drawing file. The {ext} "
                     "extension is appended if missing.")
    if not path.lower().endswith(ext):
        path = path + ext

    dwg = _drawing_common.active_drawing()
    if dwg is None:
        return error("No drawing to export: the active document is not a drawing. Open a drawing first "
                     "(drawing_create makes one; doc_open opens it by file_id), then export it as "
                     "the active document.")

    out_dir = os.path.dirname(path)
    if out_dir and not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as ex:
            return error(f"Could not create output directory '{out_dir}': {ex}")

    em = safe(lambda: dwg.exportManager)
    if em is None:
        return error("The drawing has no export manager - cannot export.")

    factory = safe(lambda: getattr(em, factory_name))
    if factory is None:
        return error(f"This Fusion build's drawing export manager has no {factory_name} - "
                     f"format={fmt} is not available here.")
    try:
        opts = factory(path)
    except Exception as ex:
        return error(f"{fmt.upper()} export options could not be created: {ex}")

    applied, aerr = _apply_options(fmt, opts, rng, line_weights, variant, splines_as_splines)
    if aerr:
        return error(f"{fmt.upper()} export refused: {aerr}")

    # The pre-export state of the target: an unchanged file at this path when the wait ends is a
    # file this export did not write, not a landing.
    before = _export.snapshot(path)
    try:
        did = em.execute(opts)
    except Exception as ex:
        return error(f"{fmt.upper()} export failed: {ex}")
    if not did:
        return error(f"{fmt.upper()} export returned false - Fusion wrote nothing. Treating this "
                     "as a failure.")

    size, werr = _wait_for_file(path, before)
    if werr:
        return error(f"{fmt.upper()} export reported success but {werr} within "
                     f"{_LAND_DEADLINE_S:.0f}s of the export call returning. Treating this as a "
                     "failure, not a false success.")

    note = f"Active drawing exported to local disk as {fmt.upper()}."
    if fmt == "pdf":
        note += " Sheet selection: " + (f"range '{rng}'." if rng else "all sheets.")
    else:
        note += " " + _SINGLE_SHEET_NOTE
    if is_preview:
        note += " " + _PREVIEW_NOTE
    payload = {"exported": True, "format": fmt, "file_path": path, "size_bytes": size, "note": note}
    payload.update(applied)
    return ok(payload)


TOOL_DESCRIPTION = (
    "Export the active 2D drawing to a PDF, DXF or DWG file on local disk - open the drawing "
    "first (doc_open by file_id)."
)

FULL_DESCRIPTION = TOOL_DESCRIPTION + "\n" + _outputs.produces_block(RETURNS)

tool = (
    Tool.create_simple(name="drawing_export", description=FULL_DESCRIPTION)
    .add_input_property(*_FORMAT.as_property())
    .add_input_property("file_path", {"type": "string",
            "description": "Local output path."})
    .add_input_property("sheet_range", {"type": "string",
            "description": "Sheets, e.g. '1-3'; omit for all. Run it FIRST: after another export "
                           "of the same drawing it can block the main thread and the file never "
                           "lands."})
    .add_input_property("line_weights", {"type": "boolean", "description": "Default true."})
    .add_input_property(*_DWG_VARIANT.as_property())
    .add_input_property("splines_as_splines", {"type": "boolean"})
    .strict_schema()
)

# enforce_timeout=False: writing a drawing's export file is a blocking, uninterruptible main-thread
# operation that can run past the server's call timeout; the file-landed gate is the real proof of
# success, so we wait for it rather than false-failing on a timeout.
item = Item.create_tool_item(tool=tool, write="write", handler=handler, run_on_main_thread=True,
                             enforce_timeout=False, deferred_capable=True,
                             postconditions=[_assert.FileLanded("file_path")])


def register_tool():
    register(item)
