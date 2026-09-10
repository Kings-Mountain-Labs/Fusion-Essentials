# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Every property assigned onto a FeatureInput must exist on that input's real class.

A SWIG proxy ACCEPTS an assignment to a name it does not define: the value lands on a dead Python
attribute, the object keeps its API default, and set_verified's read-back reads that back."""

import ast

import pytest

import _corpus
import api_surface
from _input_resolution import (_factory_target, _iter_tool_files, _named_assignments_in,
                               _offenders_in, input_scopes)

class TestInputPropertyNamesAreReal:
    def test_every_assigned_input_property_exists_on_its_class(self):
        offenders = []
        for name, path in _iter_tool_files():
            for lineno, var, prop, cls in _offenders_in(path):
                members = api_surface.PROPERTIES[cls]
                near = [m for m in members if m.lower() == prop.lower()] or \
                       [m for m in members if prop.lower() in m.lower()]
                hint = f" Did you mean {near[0]}?" if near else \
                       f" {cls} has: {', '.join(members[:12])}."
                offenders.append(f"{name}:{lineno}: {var}.{prop} is not a member of {cls}.{hint}")
        assert not offenders, (
            "a SWIG proxy accepts an unknown property name silently - the operation runs on its "
            "DEFAULT while the tool reports what was requested:\n  " + "\n  ".join(offenders))

    def test_the_gate_actually_resolves_inputs(self):
        """A resolver that silently matches nothing would pass the check above forever."""
        resolved = 0
        for _name, path in _iter_tool_files():
            for node in ast.walk(_corpus.tree(path)):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                        and isinstance(node.targets[0], ast.Name) \
                        and _factory_target(node.value):
                    resolved += 1
        assert resolved >= 120, (
            f"only {resolved} FeatureInput bindings resolved to a class - the factory/collection "
            "resolution has broken, so the property check is inspecting almost nothing.")

    def test_the_gate_checks_a_real_number_of_assignments(self):
        """Resolving inputs is not the same as CHECKING properties on them: a scope walk that lost
        the assignments would still resolve every binding and verify nothing. Counted over
        `input_scopes`, the same resolution the gate itself runs on."""
        checked = 0
        for _name, path in _iter_tool_files():
            for nodes, bound in input_scopes(_corpus.tree(path)):
                checked += sum(1 for _l, var, _p in _named_assignments_in(nodes) if var in bound)
        assert checked >= 110, (
            f"only {checked} input property assignments are being checked - the scope walk or the "
            "assignment collector has broken.")

    @pytest.mark.parametrize("cls,member", [
        ("fusion.MeshCombineFeatureInput", "meshCombineOperationType"),
        ("fusion.MeshRepairFeatureInput", "meshRepairType"),
        ("fusion.MeshPlaneCutFeatureInput", "meshPlaneCutType"),
    ])
    def test_the_surface_carries_the_names_these_tools_depend_on(self, cls, member):
        """The generated table is the gate's whole authority; an empty entry would pass everything."""
        assert member in api_surface.PROPERTIES.get(cls, ())
