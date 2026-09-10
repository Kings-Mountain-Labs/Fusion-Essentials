"""What one tool (or every tool of a family) puts on the wire as prose, against the diet allowance:
``py -3 tests/wire_prose.py model_stitch`` or ``py -3 tests/wire_prose.py model`` - each entry's tool
line and every input description with its length, the prose total, the allowance and the cut."""
import json
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
import conftest  # noqa: E402
from lints.test_wire_budget import PROSE_BASE_BYTES, PROSE_BYTES_PER_INPUT, _prose_over  # noqa: E402


def entries():
    mcp_server = conftest.load_mcp_server()
    mcp_server.BARE_WIRE_MARKER = mcp_server.BARE_WIRE_MARKER + ".never"
    srv = mcp_server.SimpleMCPServer()
    for item in conftest.register_all_tools():
        srv.register(item)
    return srv._handle_tools_list(1)["result"]["tools"]


def report(entry):
    props = (entry.get("inputSchema") or {}).get("properties") or {}
    prose, allow, _family = _prose_over(entry)
    verdict = f"OVER by {prose - allow}" if prose > allow else f"under by {allow - prose}"
    print(f"== {entry['name']}: prose {prose} against {allow} - {verdict}")
    print(f"   [{len(entry.get('description') or '')}] tool: {entry.get('description')}")
    for name, p in props.items():
        d = p.get("description") if isinstance(p, dict) else None
        if d:
            print(f"   [{len(d)}] {name}: {d}")
        items = p.get("items") if isinstance(p, dict) else None
        if isinstance(items, dict) and items.get("type") == "object":
            for sub, sp in (items.get("properties") or {}).items():
                sd = sp.get("description") if isinstance(sp, dict) else None
                if sd:
                    print(f"   [{len(sd)}] {name}[].{sub}: {sd}")


if __name__ == "__main__":
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    for e in entries():
        if e["name"] == want or e["name"].split("_")[0] == want:
            report(e)
