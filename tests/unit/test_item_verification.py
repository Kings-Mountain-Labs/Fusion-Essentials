# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Tests for the Verification classification and how Item.create_tool_item wires it.

Verification says how a WRITE tool with no kernel postcondition proves its effect. It is a CLOSED
kind whose fields are structured references, so a malformed or self-contradicting declaration is a
registration-time ValueError - the add-in refuses to load rather than shipping a tool whose
accounting nobody can resolve. And it is registry metadata: nothing here reaches to_dict(), so a
tool gains a classification without its wire schema moving a byte.
"""

import pytest

from conftest import load_tool

load_tool("_common")                      # bootstraps the mcpServer package path

from mcpServer.mcp_primitives.item import Item, Verification      # noqa: E402
from mcpServer.mcp_primitives.tool import Tool                    # noqa: E402


def _tool(name="probe_edit"):
    return Tool.create_simple(name=name, description="A probe registration.")


def _handler(**kwargs):
    return {"content": [], "isError": False}


NODE = "tests/unit/test_item_verification.py::TestFieldRules::test_a_kind_refuses_a_field_it_does_not_take"


class TestFieldRules:
    def test_an_unknown_kind_is_refused_naming_the_closed_set(self):
        with pytest.raises(ValueError) as e:
            Verification(kind="probably_fine", evidence_test=NODE)
        assert "inline" in str(e.value) and "probably_fine" in str(e.value)

    def test_a_kind_that_requires_evidence_refuses_a_bare_declaration(self):
        # inline/effect/deferred each rest on a named test; without one the kind is a bare claim.
        for kind in ("inline", "effect"):
            with pytest.raises(ValueError, match="evidence_test"):
                Verification(kind=kind)
        with pytest.raises(ValueError, match="poller"):
            Verification(kind="deferred", evidence_test=NODE)
        with pytest.raises(ValueError, match="defect_id"):
            Verification(kind="gap")

    def test_a_kind_refuses_a_field_it_does_not_take(self):
        # a poller on an inline write, or an evidence test on the script hatch, would read as an
        # obligation the kind does not carry.
        with pytest.raises(ValueError, match="poller"):
            Verification(kind="inline", evidence_test=NODE, poller="doc_get")
        with pytest.raises(ValueError, match="evidence_test"):
            Verification(kind="dynamic", evidence_test=NODE)
        with pytest.raises(ValueError, match="defect_id"):
            Verification(kind="effect", evidence_test=NODE, defect_id="DRAW-1")

    def test_a_rung_is_taken_only_by_a_kind_that_reads_an_effect_back(self):
        # inline / effect / deferred read something back and say how much; the others read nothing.
        assert Verification(kind="inline", evidence_test=NODE, rung="geometry").rung == "geometry"
        assert Verification(kind="inline", evidence_test=NODE).rung is None
        with pytest.raises(ValueError, match="rung"):
            Verification(kind="inline", evidence_test=NODE, rung="perfect")
        with pytest.raises(ValueError, match="no rung"):
            Verification(kind="gap", defect_id="DRAW-1", rung="value")
        assert "rung='value'" in repr(Verification(kind="effect", evidence_test=NODE, rung="value"))

    def test_beside_kernel_kinds_only_a_rung_declaring_inline_or_effect_stands(self):
        kernel = load_tool("_assert")
        posts = [kernel.FeatureHealthy()]
        tool = _tool().add_input_property("a", {"type": "string"})
        item = Item.create_tool_item(tool=tool, write="write", handler=_handler, postconditions=posts,
                                     verification=Verification(kind="inline", evidence_test=NODE,
                                                               rung="geometry"))
        assert item.verification.rung == "geometry"
        with pytest.raises(ValueError, match="declares a rung"):
            Item.create_tool_item(tool=_tool(), write="write", handler=_handler, postconditions=posts,
                                  verification=Verification(kind="inline", evidence_test=NODE))
        with pytest.raises(ValueError, match="declares a rung"):
            Item.create_tool_item(tool=_tool(), write="write", handler=_handler, postconditions=posts,
                                  verification=Verification(kind="gap", defect_id="DRAW-1"))

    def test_external_declares_exactly_one_channel(self):
        # both channels is two accounts that can disagree; neither is an external effect with no
        # observable completion proof, which is a gap.
        with pytest.raises(ValueError, match="exactly one"):
            Verification(kind="external")
        with pytest.raises(ValueError, match="exactly one"):
            Verification(kind="external", evidence_test=NODE,
                         evidence_receipt="tests/live/VERIFIED_TOOLS.md#sys_reload_addin")
        assert Verification(kind="external", evidence_test=NODE).evidence_receipt is None
        assert Verification(
            kind="external",
            evidence_receipt="tests/live/VERIFIED_TOOLS.md#sys_reload_addin").evidence_test is None

    def test_an_empty_reference_is_refused_rather_than_stored(self):
        with pytest.raises(ValueError, match="non-empty string"):
            Verification(kind="inline", evidence_test="   ")
        with pytest.raises(ValueError, match="non-empty string"):
            Verification(kind="gap", defect_id=object())


class TestRegistrationWiring:
    def test_a_read_cannot_declare_write_verification(self):
        with pytest.raises(ValueError, match="non-write tool"):
            Item.create_tool_item(tool=_tool("probe_get"), write="read", handler=_handler,
                                  verification=Verification(kind="inline", evidence_test=NODE))


    def test_a_loose_object_is_not_a_classification(self):
        with pytest.raises(ValueError, match="Verification kind"):
            Item.create_tool_item(tool=_tool(), write="write", handler=_handler,
                                  verification={"kind": "inline", "evidence_test": NODE})

    def test_a_write_that_wires_its_own_guard_may_still_declare(self):
        # sys_request_selection passes write=None and declares .writes() on the Tool itself, so the
        # read/write test reads the ANNOTATION rather than the write= argument.
        item = Item.create_tool_item(tool=_tool().writes(), write=None, handler=_handler,
                                     verification=Verification(kind="external", evidence_test=NODE))
        assert item.verification.kind == "external"

    def test_the_declaration_stays_off_the_wire(self):
        plain = Item.create_tool_item(tool=_tool(), write="write", handler=_handler)
        declared = Item.create_tool_item(
            tool=_tool(), write="write", handler=_handler,
            verification=Verification(kind="inline", evidence_test=NODE))
        assert declared.verification is not None and plain.verification is None
        assert declared.to_dict() == plain.to_dict()      # same schema, same annotations, same bytes

    def test_an_undeclared_write_reads_none_rather_than_raising(self):
        assert Item.create_tool_item(tool=_tool(), write="write",
                                     handler=_handler).verification is None
