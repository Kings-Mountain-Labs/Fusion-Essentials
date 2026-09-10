"""Lint: every registered tool's inputSchema carries additionalProperties:false.

A tool without ``.strict_schema()`` silently DROPS a misspelled optional argument and runs on its
default. The walk is over the LIVE registry, so it names the offender whatever its wiring shape."""

from conftest import register_all_tools


class TestStrictSchema:
    def test_every_tool_schema_refuses_unknown_arguments(self):
        offenders = []
        for it in register_all_tools():
            prim = getattr(it, "primitive", None)
            name = getattr(prim, "name", None) or "<unnamed>"
            if getattr(prim, "additional_properties", None) is not False:
                offenders.append(name)
        assert not offenders, (
            "tool(s) whose inputSchema does not refuse unknown arguments - append "
            ".strict_schema() to each tool's builder chain:\n  " + "\n  ".join(sorted(offenders)))
