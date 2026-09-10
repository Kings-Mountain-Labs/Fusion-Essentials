"""Lint: every registered tool must declare a write-status annotation.

Annotations carry ``readOnlyHint`` and, for writes, ``destructiveHint``. This lint owns one check:
every tool DECLARES a hint at registration. What crosses the WIRE is test_wire_shape.py's."""

from conftest import register_all_tools


class TestWriteStatusDeclared:
    def test_every_tool_declares_read_only_hint(self):
        items = register_all_tools()
        assert items, "no tools registered"
        missing = []
        for it in items:
            ann = it.primitive.annotations
            if ann is None or ann.read_only is None:
                missing.append(it.get_name())
        assert not missing, (
            "tools missing a write-status declaration (call .reads() or .writes() on the Tool): "
            + ", ".join(sorted(missing))
        )
