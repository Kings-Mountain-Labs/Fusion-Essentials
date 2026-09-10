# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: a numeric input naming a unit in prose (mm/cm/inch) whose tool exposes no 'units' selector
fails; a read REPORTING a 'units' field whose 'units' input is not the shared _inputs.UNITS enum
kind fails; raw UNIT_TO_CM access or a local mm/cm factor table outside _common.py fails. The two
shrink-only exemption tables each need a reason, and a stale entry fails."""

import ast
import os
import re

import _corpus
from conftest import load_tool, register_all_tools, TOOLS_DIR

# a unit token, allowing a leading digit ("5mm") but not a letter ("swimming", "incoming").
_UNIT = re.compile(r"(?<![A-Za-z])(mm|cm|inch(?:es)?|millimet\w*|centimet\w*)(?![A-Za-z])", re.I)

# "tool.input" paths where a bare unit in prose is deliberate (a fixed unit the agent cannot select).
# A nested path reads "tool.object.field", and one inside an array's items "tool.array[].field".
# Shrink-only; each entry needs a reason.
_EXEMPT = {
    # "some_tool.some_input": "reason a fixed unit is correct here",
    "joint_motion_link.ratio": "a motion-link ratio's unit is a PAIR fixed by the two joints' own "
                               "DOF kinds (deg for a rotation, mm for a slide) - one 'units' "
                               "selector cannot name a deg-per-mm ratio, and _joints."
                               "link_ratio_values converts the number to Fusion's native rad/cm "
                               "before it is written",
    "assembly_edit_relations.ratio": "the re-value half of joint_motion_link.ratio - the same "
                                     "per-DOF pair of fixed units through the same codec",
}

# Tools that REPORT a 'units' field yet legitimately do NOT wire the shared UNITS kind. Shrink-only;
# each needs a reason a scaled-measurement selector is wrong here.
_REPORTS_EXEMPT = {
    "workspace_orient": "reports the design's own defaultLengthUnits as a FACT (a zero-input "
                        "orientation read), not a scaled measurement the agent picks units for",
    "cam_post": "reports NC OUTPUT units via a domain Choice (document/inch/mm) - not a CM_TO_UNIT "
                "length measurement, and it is already a typed enum, not stranded prose",
    "drawing_create": "reports DRAWING display units via a domain Choice (mm/inch) - a drawing display "
                      "setting, not a CM_TO_UNIT length measurement, and already a typed enum",
}


def _is_units_kind(schema) -> bool:
    """True if a 'units' input property is the shared UNITS enum kind - an enum covering mm/cm/in, the
    signature _inputs.UNITS / UnitField / units_property emit. A hand-rolled 'units' string (no enum)
    is NOT the shared kind."""
    if not isinstance(schema, dict):
        return False
    enum = schema.get("enum")
    return isinstance(enum, list) and {"mm", "cm", "in"} <= set(enum)


def _tools_with_report_source():
    """(tool_name, input_props, module source) per registered tool; a '"units":' occurrence in the
    source (a _slice_* helper's included) = the tool reports a units field."""
    files = [fn for fn in sorted(os.listdir(TOOLS_DIR)) if fn.endswith(".py") and not fn.startswith("_")]
    if files:
        load_tool(files[0][:-3])                     # bootstraps COMMANDS_DIR onto sys.path first
    from mcpServer.mcp_primitives import registry    # importable only after the bootstrap above
    out = []
    for fn in files:
        mod = load_tool(fn[:-3])
        rt = getattr(mod, "register_tool", None)
        if not callable(rt):
            continue
        registry.reset_registry()
        rt()
        items = list(registry.get_tools())
        module_src = _corpus.text(os.path.join(TOOLS_DIR, fn))
        for it in items:
            d = it.to_dict()
            props = (d.get("inputSchema") or {}).get("properties", {}) or {}
            out.append((d.get("name"), props, module_src))
    return out


def _is_numeric(schema):
    """True for a number/integer, or an array that bottoms out in one (array of number, ...)."""
    if not isinstance(schema, dict):
        return False
    t = schema.get("type")
    if t in ("number", "integer"):
        return True
    if t == "array":
        return _is_numeric(schema.get("items"))
    return False


def _numeric_unit_props(props, path, out):
    """Collect (path, unit, description) for every numeric input naming a unit in prose, at ANY
    nesting depth: an object's own `properties` and the `properties` of an array's `items` both get
    descended (an item path carries a `[]` segment). Same walk as test_wire_ascii's
    _walk_property_descriptions - a unit stranded in prose is no less stranded for sitting inside a
    list of objects, and the two lints police the same authored strings."""
    for name, schema in props.items():
        if not isinstance(schema, dict):
            continue
        if _is_numeric(schema):
            m = _UNIT.search(schema.get("description") or "")
            if m:
                out.append((f"{path}.{name}", m.group(0), schema.get("description") or ""))
        nested = schema.get("properties")
        if isinstance(nested, dict):
            _numeric_unit_props(nested, f"{path}.{name}", out)
        items = schema.get("items")
        if isinstance(items, dict) and isinstance(items.get("properties"), dict):
            _numeric_unit_props(items["properties"], f"{path}.{name}[]", out)


class TestUnitsAreTyped:
    def test_numeric_input_naming_a_unit_has_a_units_selector(self):
        offenders = []
        for it in register_all_tools():
            d = it.to_dict()
            name = d.get("name")
            props = (d.get("inputSchema") or {}).get("properties", {}) or {}
            if "units" in props:                       # tool already exposes a unit selector
                continue
            found = []
            _numeric_unit_props(props, name, found)
            for path, unit, desc in found:
                if path in _EXEMPT:
                    continue
                offenders.append(f"{path}: numeric input says '{unit}' in prose but the tool has no "
                                 f"'units' selector - pair it with _inputs.Distance/UnitField. "
                                 f"(desc: {desc[:70]!r})")
        assert not offenders, (
            "Unit stranded in prose - declare it with a typed kind (Distance + UnitField), or add the "
            "input path to _EXEMPT with a reason:\n  " + "\n  ".join(offenders))

    def test_units_reporting_read_wires_the_units_kind(self):
        # The read-side mirror: a tool that REPORTS a 'units' field must expose the shared UNITS enum
        # kind so the agent can SELECT the units it reads back (scaled via CM_TO_UNIT) - a hand-rolled
        # 'units' string lets the report and the request drift.
        offenders = []
        for name, props, src in _tools_with_report_source():
            if name in _REPORTS_EXEMPT:
                continue
            if '"units":' not in src:                  # this tool does not report a units field
                continue
            if not _is_units_kind(props.get("units")):
                offenders.append(
                    f"{name}: reports a 'units' field but its 'units' input is not the shared "
                    "_inputs.UNITS enum kind - wire *_inputs.UNITS.as_property() (or units_property()), "
                    "or add it to _REPORTS_EXEMPT with a reason.")
        assert not offenders, (
            "A units-reporting read must let the agent choose those units via the shared UNITS kind:\n  "
            + "\n  ".join(offenders))


# ── exemption staleness - an entry must still exist and still need its exemption ────────────────

def _stale_exempt_entries(exempt, tool_props):
    """Stale _EXEMPT entries, given {tool_name: input props}: an entry is dead weight when its tool
    is gone, its tool now exposes a 'units' selector, or no numeric input at that path names a unit
    in prose any more - in each case the main check passes without it, so the entry must go."""
    stale = []
    for path, reason in exempt.items():
        assert reason.strip(), f"{path} exemption needs a plain-English reason"
        tool_name = path.split(".", 1)[0]
        props = tool_props.get(tool_name)
        if props is None:
            stale.append(f"{path}: no such tool")
            continue
        if "units" in props:
            stale.append(f"{path}: the tool now exposes a 'units' selector - remove the entry")
            continue
        found = []
        _numeric_unit_props(props, tool_name, found)
        if path not in [p for p, _, _ in found]:
            stale.append(f"{path}: no numeric input naming a unit in prose at this path - remove the entry")
    return stale


def _stale_reports_exempt_entries(exempt, report_rows):
    """Stale _REPORTS_EXEMPT entries, given [(name, props, src)] rows: an entry is dead weight when
    its tool is gone, no longer reports a units field, or already wires the shared UNITS kind - in
    each case the read-side check passes without it, so the entry must go."""
    rows = {name: (props, src) for name, props, src in report_rows}
    stale = []
    for name, reason in exempt.items():
        assert reason.strip(), f"{name} exemption needs a plain-English reason"
        if name not in rows:
            stale.append(f"{name}: no such tool")
            continue
        props, src = rows[name]
        if '"units":' not in src:
            stale.append(f"{name}: no longer reports a units field - remove the entry")
        elif _is_units_kind(props.get("units")):
            stale.append(f"{name}: its 'units' input is already the shared UNITS kind - the read-side "
                         "check passes without the exemption")
    return stale


class TestExemptionsAreNotStale:
    def test_both_exemption_tables_still_need_every_entry(self):
        tool_props = {}
        for it in register_all_tools():
            d = it.to_dict()
            tool_props[d.get("name")] = (d.get("inputSchema") or {}).get("properties", {}) or {}
        stale = ([f"_EXEMPT {s}" for s in _stale_exempt_entries(_EXEMPT, tool_props)]
                 + [f"_REPORTS_EXEMPT {s}" for s in
                    _stale_reports_exempt_entries(_REPORTS_EXEMPT, _tools_with_report_source())])
        assert not stale, "stale exemption entries:\n  " + "\n  ".join(stale)


# ── the conversion side: one table, one scale() ────────────────────────────────

_SCALE_HOME = "_common.py"
_RAW_ACCESS = re.compile(r"\bUNIT_TO_CM\s*[.\[]")


def _files_outside_the_scale_home():
    return [fn for fn in sorted(os.listdir(TOOLS_DIR))
            if fn.endswith(".py") and fn != _SCALE_HOME]


class TestUnitsScaled:
    def test_no_raw_unit_to_cm_access_outside_common(self):
        offenders = []
        for fn in _files_outside_the_scale_home():
            src = _corpus.text(os.path.join(TOOLS_DIR, fn))
            for i, line in enumerate(src.splitlines(), 1):
                if _RAW_ACCESS.search(line):
                    offenders.append(f"{fn}:{i}: {line.strip()}")
        assert not offenders, (
            "unit conversion re-inlines _common.scale() via raw UNIT_TO_CM access - call "
            "`_common.scale(units)` (unit->cm) or `_common.CM_TO_UNIT[units]` (cm->unit) instead:\n  "
            + "\n  ".join(offenders))

    def test_no_local_unit_table_copy_outside_common(self):
        offenders = []
        for fn in _files_outside_the_scale_home():
            tree = _corpus.tree(os.path.join(TOOLS_DIR, fn))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                numeric = node.values and all(
                    isinstance(v, ast.Constant) and isinstance(v.value, (int, float)) for v in node.values)
                if "mm" in keys and "cm" in keys and numeric:
                    offenders.append(f"{fn}:{getattr(node, 'lineno', '?')}: local unit-factor table "
                                     "(keys 'mm'+'cm' -> numbers)")
        assert not offenders, (
            "a local copy of the unit-factor table diverges from _common.UNIT_TO_CM - import it (or use "
            "_common.scale/CM_TO_UNIT):\n  " + "\n  ".join(offenders))
