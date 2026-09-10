# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

import ast
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).parents[2] / "commands" / "updateTools" / "entry.py"


class JsonTool:
    """Owned JSON and a named preset for matching control-flow tests."""
    def __init__(self, description, product_id, diameter):
        self.payload = {"description": description, "product-id": product_id,
                        "geometry": {"diameter": diameter},
                        "start-values": {"presets": [{"name": "finish"}]}}
        self.preset = SimpleNamespace(name="finish")
        self.presets = SimpleNamespace(itemsByName=lambda name: [self.preset])

    def toJson(self):
        return json.dumps(self.payload)


class OperationRecord:
    """An owned operation record that stores the selected tool and preset."""
    def __init__(self, name, tool):
        self.name = name
        self.tool = tool
        self.toolPreset = SimpleNamespace(name="finish")


class ToolRecords:
    """An owned tool list with the accessors used by the matching function."""
    def __init__(self, tools=()):
        self.tools = list(tools)

    @property
    def count(self):
        return len(self.tools)

    def add(self, tool):
        self.tools.append(tool)

    def item(self, index):
        return self.tools[index]


@pytest.fixture
def match_tools():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {"remove_tip_keys", "LibraryTool", "replace_with_library_tool"}
    nodes = [node for node in tree.body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    assert len(nodes) == len(names)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    code = compile(ast.fix_missing_locations(ast.Module(body=[future] + nodes, type_ignores=[])),
                   str(SOURCE), "exec")

    def run(operations, library, correlation):
        document_tools, logs, messages = ToolRecords(), [], []
        namespace = {
            "json": json, "sha256": sha256,
            "app": SimpleNamespace(activeProduct=object()),
            "adsk": SimpleNamespace(cam=SimpleNamespace(CAM=SimpleNamespace(
                cast=lambda _product: SimpleNamespace(documentToolLibrary=document_tools)))),
            "timer": SimpleNamespace(mark=lambda _name: None),
            "futil": SimpleNamespace(log=logs.append),
            "ui": SimpleNamespace(messageBox=messages.append),
        }
        exec(code, namespace)
        namespace["replace_with_library_tool"](operations, library, correlation)
        return document_tools, logs, messages

    return run


@pytest.mark.parametrize("correlation", ["Description", "Product ID", "Geometry"])
def test_index_zero_match_preserves_the_selected_preset(match_tools, correlation):
    operation = OperationRecord("IndexZero", JsonTool("same", "same", 1.0))
    target = JsonTool("same", "same", 1.0)
    document_tools, _, messages = match_tools([operation], ToolRecords([target]), correlation)
    assert operation.tool is target
    assert document_tools.tools == [target]
    assert operation.toolPreset is target.preset
    assert messages == []


@pytest.mark.parametrize("correlation,field", [("Description", "description"),
                                                ("Product ID", "product-id")])
@pytest.mark.parametrize("preceding_match", [False, True])
def test_blank_correlation_keeps_its_tool_and_processing_continues(
        match_tools, correlation, field, preceding_match):
    target = JsonTool("match", "match", 2.0)
    library = ToolRecords([JsonTool("other", "other", 1.0), target])
    blank_tool = JsonTool("blank-description", "blank-id", 3.0)
    blank_tool.payload[field] = ""
    blank = OperationRecord("Blank", blank_tool)
    following = OperationRecord("Following", JsonTool("match", "match", 2.0))
    operations = [blank, following]
    if preceding_match:
        operations.insert(0, OperationRecord("Preceding", JsonTool("match", "match", 2.0)))
    document_tools, logs, messages = match_tools(operations, library, correlation)
    assert blank.tool is blank_tool
    assert all(op.tool is target for op in operations if op is not blank)
    assert document_tools.tools == [target]
    assert any("Blank" in log and "No Match Found" in log for log in logs)
    assert len(messages) == 1 and "could not be correlated" in messages[0]


def test_toolless_operation_is_reported_and_next_match_is_applied(match_tools):
    target = JsonTool("match", "match", 2.0)
    empty = OperationRecord("NoTool", None)
    following = OperationRecord("Following", JsonTool("match", "match", 2.0))
    document_tools, logs, messages = match_tools(
        [empty, following], ToolRecords([JsonTool("other", "other", 1.0), target]), "Description")
    assert empty.tool is None
    assert following.tool is target and document_tools.tools == [target]
    assert any("NoTool" in log and "Skipped: no tool assigned" in log for log in logs)
    assert len(messages) == 1 and "could not be correlated" in messages[0]


def test_an_unmatched_operation_keeps_its_tool(match_tools):
    original = JsonTool("absent", "absent", 9.0)
    operation = OperationRecord("Unmatched", original)
    document_tools, logs, messages = match_tools(
        [operation], ToolRecords([JsonTool("other", "other", 1.0)]), "Description")
    assert operation.tool is original and document_tools.tools == []
    assert any("Unmatched" in log and "No Match Found" in log for log in logs)
    assert len(messages) == 1 and "could not be correlated" in messages[0]
