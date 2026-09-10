"""Wire budget: the tools/list payload is bounded per entry and across the fleet.

Two units, and no number here is comparable to another without naming which unit it is in:

- TRANSPORT bytes - ``json.dumps(entry, indent=2)``, what mcp_server._send_json writes.
  Pretty-printing costs a newline plus indentation per key, so it lands hardest on the entries with
  the most keys - which is why the per-entry ceiling is measured here and not on the description
  alone (test_prose_budget never sees inputSchema at all).
- COMPACT bytes - ``json.dumps(entry, separators=(",", ":"))``. Every MCP client re-serializes each
  definition into its own prompt format, so what a model pays for tracks the compact form. Both
  FLEET limits are in this unit.

The fleet limits are structural, so no tool is named in this file. The total is an allowance PER
REGISTERED TOOL, keeping "the fleet grew" and "the tools got fatter" separate failures; the P90
catches a heavy tail the mean absorbs. A failed limit prints the total, the P90, the entry the P90
rank reads with those just above it, and the ten heaviest - all computed at failure time, so a
slimmed or reworded description leaves nothing to re-pin here."""

import json
import math

import pytest

from conftest import load_mcp_server, register_all_tools

# The per-entry hard ceiling, in TRANSPORT bytes: the fleet's heaviest entry as measured, plus ~2%.
# It bites on whichever entry is nearest it, so no single tool can fatten unnoticed.
PER_TOOL_BUDGET_BYTES = 5_360

# The fleet limits, in COMPACT bytes, each its own measured statistic plus ~2%: the MEAN per
# registered tool, and the NEAREST-RANK P90 (_rank_index - a size some entry really measures, never
# an interpolation between two, so the ceiling and the number it is compared against are the same
# statistic). Recalibrating either is the owner's decision - a red bar is answered by slimming the
# entries the failure names, not by raising the number.
FLEET_BYTES_PER_TOOL = 890
FLEET_P90_BUDGET_BYTES = 1_560

# The PROSE allowance per entry, in COMPACT bytes: what its descriptions - the tool's own and every
# input's - may add beyond the entry's bare structure, as a base for the tool line plus a share per
# input. An input's description states only what its type and enum cannot; teaching lives in the
# error an agent meets when it matters.
PROSE_BASE_BYTES = 200
PROSE_BYTES_PER_INPUT = 40


@pytest.fixture(scope="module")
def wire_tools():
    """The tools/list entries exactly as the REAL server sends them, all tools registered - the
    FULL wire: the bare-wire marker is an experiment switch, and a budget measured with it on
    would pass anything."""
    mcp_server = load_mcp_server()
    mcp_server.BARE_WIRE_MARKER = mcp_server.BARE_WIRE_MARKER + ".never"
    srv = mcp_server.SimpleMCPServer()
    for item in register_all_tools():
        srv.register(item)
    return srv._handle_tools_list(1)["result"]["tools"]


def _wire_bytes(entry):
    """One entry's TRANSPORT size: json.dumps(..., indent=2), what the server sends. The ONE place
    that unit is chosen, so the per-entry ceiling cannot drift onto the other one."""
    return len(json.dumps(entry, indent=2))


def _compact_bytes(entry):
    """One entry's COMPACT size: what a client's own re-serialization tracks. The ONE place that
    unit is chosen, so the fleet limits cannot drift onto the transport's."""
    return len(json.dumps(entry, separators=(",", ":")))


def _sizes(entries):
    """(name, COMPACT size) for every entry, ascending by size."""
    return sorted(((e["name"], _compact_bytes(e)) for e in entries), key=lambda kv: kv[1])


def _rank_index(count, pct):
    """The 0-based index the NEAREST-RANK percentile reads over `count` values: the
    ceil(pct/100 * count)-th smallest, 1-indexed. -1 when there are no values at all."""
    return max(1, math.ceil(pct / 100 * count)) - 1 if count else -1


def _fleet(entries):
    """(total, nearest-rank P90, the ten heaviest as (name, size)) over every COMPACT size."""
    ordered = _sizes(entries)
    rank = _rank_index(len(ordered), 90)
    p90 = ordered[rank][1] if rank >= 0 else 0
    return sum(size for _, size in ordered), p90, list(reversed(ordered[-10:]))


def _fleet_report(entries):
    """What a failed fleet limit prints. The P90 is one entry's own size, so slimming that entry
    lifts the number to the NEXT one above it - naming the rank band is what makes the cheap fix
    findable; the heaviest ten are the expensive fix."""
    ordered = _sizes(entries)
    total, p90, heaviest = _fleet(entries)
    allowance = len(entries) * FLEET_BYTES_PER_TOOL
    rank = _rank_index(len(ordered), 90)
    band = ordered[rank:rank + 4] if rank >= 0 else []
    return (f"fleet compact wire over {len(entries)} tools: total {total:,} bytes against an "
            f"allowance of {allowance:,} ({len(entries)} x {FLEET_BYTES_PER_TOOL:,} per tool), "
            f"P90 {p90:,} against a ceiling of {FLEET_P90_BUDGET_BYTES:,}.\n"
            "  at the P90 rank, then the entries above it (slimming one lifts the P90 to the "
            "next): " + ", ".join(f"{name}: {size:,}" for name, size in band) + "\n"
            "  heaviest ten: " + ", ".join(f"{name}: {size:,}" for name, size in heaviest)
            + "\n  slim these - move workflow prose to a result note, or type the input.")


def _over_ceiling(entries):
    """name -> TRANSPORT size, for every entry over the per-entry ceiling."""
    return {e["name"]: n for e in entries if (n := _wire_bytes(e)) > PER_TOOL_BUDGET_BYTES}


def test_no_single_tool_exceeds_wire_ceiling(wire_tools):
    over = _over_ceiling(wire_tools)
    assert not over, (f"tool entries over {PER_TOOL_BUDGET_BYTES:,} transport bytes: {over} - "
                      "slim the description, or type the input that carries the prose.")


def test_the_fleet_total_stays_within_its_per_tool_allowance(wire_tools):
    total, _, _ = _fleet(wire_tools)
    assert total <= len(wire_tools) * FLEET_BYTES_PER_TOOL, _fleet_report(wire_tools)


def test_the_fleet_p90_stays_under_its_ceiling(wire_tools):
    _, p90, _ = _fleet(wire_tools)
    assert p90 <= FLEET_P90_BUDGET_BYTES, _fleet_report(wire_tools)


def _bare(node):
    """`node` with every prose 'description' removed - the entry's structure alone. A schema
    PROPERTY named 'description' (its value a dict) is structure and stays."""
    if isinstance(node, dict):
        return {k: _bare(v) for k, v in node.items()
                if not (k == "description" and isinstance(v, str))}
    if isinstance(node, list):
        return [_bare(v) for v in node]
    return node


def _without_produces(entry):
    """`entry` with the trailing 'Produces:' line cut from its description - that line is the
    RETURNS declaration rendered, structure like an enum, not prose."""
    desc = entry.get("description") or ""
    cut = desc.rfind("\nProduces: ")
    if cut < 0:
        return entry
    return {**entry, "description": desc[:cut]}


def _input_count(schema):
    """How many inputs a schema declares: its properties, plus the fields of every list entry
    (an array whose items are an object) - each is a field an agent fills in."""
    props = (schema or {}).get("properties") or {}
    n = len(props)
    for p in props.values():
        items = p.get("items") if isinstance(p, dict) else None
        if isinstance(items, dict) and items.get("type") == "object":
            n += _input_count(items)
    return n


def _prose_over(entry):
    """(prose bytes, allowance, family) for one entry: its COMPACT size less its bare structure,
    against PROSE_BASE_BYTES plus PROSE_BYTES_PER_INPUT per declared input."""
    inputs = _input_count(entry.get("inputSchema"))
    entry = _without_produces(entry)
    prose = _compact_bytes(entry) - _compact_bytes(_bare(entry))
    return prose, PROSE_BASE_BYTES + PROSE_BYTES_PER_INPUT * inputs, entry["name"].split("_")[0]


def test_each_tools_prose_stays_within_its_input_allowance(wire_tools):
    over = []
    for e in wire_tools:
        prose, allow, _family = _prose_over(e)
        if prose > allow:
            over.append(f"{e['name']}: {prose:,} prose bytes against {allow:,}")
    assert not over, ("description prose over the per-input allowance ("
                      f"{PROSE_BASE_BYTES} + {PROSE_BYTES_PER_INPUT} per input, compact bytes):\n  "
                      + "\n  ".join(sorted(over))
                      + "\n  cut the input descriptions to what their type and enum cannot say; "
                      "move teaching to the error an agent meets when it matters.")
