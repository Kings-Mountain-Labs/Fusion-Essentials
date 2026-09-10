"""Wire format: the JSON-RPC tools/list response includes annotations and strict schemas.

SimpleMCPServer._handle_tools_list emits every tool entry via to_dict(), so each carries
annotations (readOnlyHint/destructiveHint) and strict-schema additionalProperties=false."""

import pytest

from conftest import load_mcp_server, register_all_tools


@pytest.fixture
def server():
    """The REAL SimpleMCPServer with every tool registered, so the asserts bite the
    production _handle_tools_list, not a re-implementation of it."""
    mcp_server = load_mcp_server()
    # The FULL wire: the bare-wire marker is an experiment switch, not a shape to lint.
    mcp_server.BARE_WIRE_MARKER = mcp_server.BARE_WIRE_MARKER + ".never"
    srv = mcp_server.SimpleMCPServer()
    for item in register_all_tools():
        srv.register(item)
    return srv


def _original_handler(item):
    """The handler as the tool module wrote it, under the guard and kernel wrappers."""
    h = item.handler
    seen = set()
    while getattr(h, "__wrapped__", None) is not None and id(h) not in seen:
        seen.add(id(h))
        h = h.__wrapped__
    return h


def _signature_defaults(handler):
    """{parameter: default} for every parameter whose default belongs on the wire."""
    import inspect
    from mcpServer.tools._inputs import schema_default
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return {}
    return {n: p.default for n, p in params.items()
            if p.default is not inspect.Parameter.empty and schema_default(p.default)}


class TestSchemaDefaults:
    """An omitted input's value is STRUCTURE on the wire: the schema's `default` agrees with the
    handler's own signature default, both ways, so an agent never guesses a value the prose
    stopped spelling and the schema never promises a value the handler does not take."""

    def test_every_signature_default_is_on_the_wire_and_agrees(self):
        drift = []
        for item in register_all_tools():
            props = (item.primitive.input_schema.get("properties") or {})
            wanted = _signature_defaults(_original_handler(item))
            for name, value in wanted.items():
                prop = props.get(name)
                if not isinstance(prop, dict):
                    continue
                if prop.get("default") != value:
                    drift.append(f"{item.get_name()}.{name}: handler default {value!r}, "
                                 f"schema default {prop.get('default')!r}")
        assert not drift, ("a signature default is not what the schema promises - set the kind's "
                           "default= or the handler's default so the two agree:\n  "
                           + "\n  ".join(sorted(drift)))

    def test_every_schema_default_is_a_default_the_handler_takes(self):
        stray = []
        for item in register_all_tools():
            props = (item.primitive.input_schema.get("properties") or {})
            import inspect
            try:
                params = inspect.signature(_original_handler(item)).parameters
            except (TypeError, ValueError):
                continue
            for name, prop in props.items():
                if not isinstance(prop, dict) or "default" not in prop:
                    continue
                p = params.get(name)
                if p is None:
                    continue                       # a guard input the wrapper consumes
                if p.default is inspect.Parameter.empty or p.default != prop["default"]:
                    stray.append(f"{item.get_name()}.{name}: schema default {prop['default']!r}, "
                                 f"handler default {p.default!r}")
        assert not stray, ("the schema promises a default the handler does not take:\n  "
                           + "\n  ".join(sorted(stray)))


class TestToolsListWireFormat:
    def test_tools_list_sends_annotations_on_the_wire(self, server):
        result = server._handle_tools_list(request_id="test-id")
        assert result["jsonrpc"] == "2.0"
        assert result["id"] == "test-id"
        tools = result["result"]["tools"]
        assert tools, "No tools registered"
        for entry in tools:
            assert "annotations" in entry, f"{entry.get('name')} missing annotations on wire"

    def test_all_entries_have_required_keys(self, server):
        result = server._handle_tools_list(request_id="test-1")
        tools = result["result"]["tools"]
        for entry in tools:
            assert "name" in entry, f"Entry missing name: {entry}"
            assert "description" in entry, f"Entry {entry.get('name')} missing description"
            assert "inputSchema" in entry, f"Entry {entry.get('name')} missing inputSchema"
            assert "annotations" in entry, f"Entry {entry.get('name')} missing annotations"

    def test_read_only_tools_have_correct_annotations(self, server):
        result = server._handle_tools_list(request_id="test-2")
        tools = result["result"]["tools"]
        for entry in tools:
            ann = entry.get("annotations", {})
            if ann.get("readOnlyHint") is True:
                destructive = ann.get("destructiveHint", False)
                assert not destructive, (
                    f"Read-only tool {entry['name']} has destructiveHint={destructive}"
                )

    def test_no_audience_priority_lastmodified_in_annotations(self, server):
        result = server._handle_tools_list(request_id="test-4")
        tools = result["result"]["tools"]
        for entry in tools:
            ann = entry.get("annotations", {})
            assert "audience" not in ann, f"{entry['name']}: audience should not be in annotations"
            assert "priority" not in ann, f"{entry['name']}: priority should not be in annotations"
            assert "lastModified" not in ann, f"{entry['name']}: lastModified should not be in annotations"

    def test_every_tool_schema_is_strict_on_the_wire(self, server):
        result = server._handle_tools_list(request_id="test-5")
        tools = result["result"]["tools"]
        assert tools, "No tools registered"
        for entry in tools:
            assert entry.get("inputSchema", {}).get("additionalProperties") is False, (
                f"{entry['name']}: inputSchema must carry additionalProperties=false"
            )

    def test_destructive_tools_marked_on_the_wire(self, server):
        result = server._handle_tools_list(request_id="test-9")
        tools = result["result"]["tools"]
        destructive = [
            e for e in tools if e.get("annotations", {}).get("destructiveHint") is True
        ]
        assert destructive, "Expected at least one destructive tool (e.g. cam_delete)"
        for entry in destructive:
            assert entry["annotations"].get("readOnlyHint") is False, (
                f"{entry['name']}: destructiveHint=true requires readOnlyHint=false"
            )

    def test_all_descriptions_non_empty(self, server):
        result = server._handle_tools_list(request_id="test-6")
        tools = result["result"]["tools"]
        for entry in tools:
            desc = entry.get("description", "").strip()
            assert desc, f"Tool {entry['name']} has empty description"

    def test_read_only_hint_is_bool(self, server):
        result = server._handle_tools_list(request_id="test-7")
        tools = result["result"]["tools"]
        for entry in tools:
            ann = entry.get("annotations", {})
            assert isinstance(ann.get("readOnlyHint"), bool), (
                f"{entry['name']}: readOnlyHint must be present and bool, "
                f"got {ann.get('readOnlyHint')!r}"
            )

    def test_destructive_hint_when_present_is_bool(self, server):
        result = server._handle_tools_list(request_id="test-8")
        tools = result["result"]["tools"]
        for entry in tools:
            ann = entry.get("annotations", {})
            if "destructiveHint" in ann:
                assert isinstance(ann["destructiveHint"], bool), (
                    f"{entry['name']}: destructiveHint should be bool, got {type(ann['destructiveHint'])}"
                )
