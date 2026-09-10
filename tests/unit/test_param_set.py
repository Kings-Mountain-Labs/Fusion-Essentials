"""Unit tests for ``param_set.py`` - input validation and the set/create read-back.

Includes the subtle carve-out that an expression of ``"0"`` is NOT treated as "empty".
"""

from types import SimpleNamespace

import adsk.core

from conftest import (FakeUserParameter, FakeUserParameters, MakeDesign, load_tool, make_timeline,
                      payload as _payload)

params = load_tool("param_set")


def _design(user_params, timeline, all_params=()):
    """A design carrying the two collections the param write path walks."""
    return MakeDesign(user_parameters=user_params, timeline=timeline,
                      all_parameters=list(all_params))


def _stub_design(monkeypatch, design):
    monkeypatch.setattr(params._common, "design", lambda: design)
    # the create path uses adsk.core.ValueInput.createByString; the string it carries is what the
    # new parameter's expression reads back as.
    monkeypatch.setattr(adsk.core.ValueInput, "createByString",
                        staticmethod(lambda s: SimpleNamespace(stringValue=s)))


class TestSetValidation:
    def test_empty_name_is_error(self):
        res = params.handler(name="", expression="5")
        assert res["isError"] is True
        assert "Provide 'name'" in res["message"]

    def test_empty_expression_is_error(self):
        res = params.handler(name="StockX", expression="")
        assert res["isError"] is True
        assert "Provide 'expression'" in res["message"]

    def test_zero_expression_passes_the_empty_guard(self, monkeypatch):
        # "0" is a legitimate value and must NOT trip the empty-expression guard
        # (note the explicit `expression != "0"` carve-out in the source). Stub
        # _design to a known failure so we can prove we got PAST validation to a
        # different, later error - not the "Provide 'expression'" rejection.
        monkeypatch.setattr(params._common, "design", lambda: None)
        result = params.handler(name="StockX", expression="0")
        assert result["isError"] is True
        assert "Provide 'expression'" not in result["message"]
        assert "active design" in result["message"]   # reached the _design() check


class TestSetCreateOrUpdate:
    def test_set_existing_updates(self, monkeypatch):
        up = FakeUserParameters([FakeUserParameter(name="PartX", expression="10 mm")])
        design = _design(up, make_timeline())
        _stub_design(monkeypatch, design)
        out = _payload(params.handler(name="PartX", expression="20 mm"))
        assert out["set"] is True and out["created"] is False

    def test_silent_no_op_assignment_bites(self, monkeypatch):
        # the assignment raises nothing but the parameter still reads the same expression -> error

        class StuckParam(FakeUserParameter):
            @property
            def expression(self):
                return "10 mm"

            @expression.setter
            def expression(self, v):
                pass                                     # silently ignores the assignment

        up = FakeUserParameters([StuckParam(name="PartX")])
        design = _design(up, make_timeline())
        _stub_design(monkeypatch, design)
        res = params.handler(name="PartX", expression="20 mm")
        assert res["isError"] is True
        assert "did not take" in res["message"]

    def test_setting_the_current_expression_is_already_current(self, monkeypatch):
        up = FakeUserParameters([FakeUserParameter(name="PartX", expression="10 mm")])
        design = _design(up, make_timeline())
        _stub_design(monkeypatch, design)
        out = _payload(params.handler(name="PartX", expression="10 mm"))
        assert out["set"] is True and out["already_current"] is True

    def test_whitespace_normalized_current_expression_is_already_current(self, monkeypatch):
        class NormalizedParam(FakeUserParameter):
            @property
            def expression(self):
                return "10mm"

            @expression.setter
            def expression(self, value):
                pass

        up = FakeUserParameters([NormalizedParam(name="PartX")])
        design = _design(up, make_timeline())
        _stub_design(monkeypatch, design)
        out = _payload(params.handler(name="PartX", expression="10 mm"))
        assert out["set"] is True and out["already_current"] is True

    def test_normalization_keeps_identifier_boundaries_and_quoted_text(self):
        assert params._normalized_expression("10 mm") == params._normalized_expression("10mm")
        assert params._normalized_expression("A B") != params._normalized_expression("AB")
        assert params._normalized_expression("1 e-3") != params._normalized_expression("1e-3")
        assert params._normalized_expression("'a b'") != params._normalized_expression("'ab'")

    def test_silent_equal_value_dependency_change_is_not_already_current(self, monkeypatch):
        class StuckParam(FakeUserParameter):
            @property
            def expression(self):
                return "DriverA"

            @expression.setter
            def expression(self, value):
                pass

        up = FakeUserParameters([StuckParam(name="PartX", value=1.0)])
        _stub_design(monkeypatch, _design(up, make_timeline()))
        res = params.handler(name="PartX", expression="DriverB")
        assert res["isError"] is True and "did not take" in res["message"]

    def test_set_missing_without_create_errors(self, monkeypatch):
        design = _design(FakeUserParameters([]), make_timeline())
        _stub_design(monkeypatch, design)
        res = params.handler(name="Ghost", expression="5 mm")
        assert res["isError"] is True and "create=true" in res["message"]

    def test_set_missing_with_create_makes_user_param(self, monkeypatch):
        up = FakeUserParameters([])
        design = _design(up, make_timeline())
        _stub_design(monkeypatch, design)
        out = _payload(params.handler(name="NewP", expression="3 mm", create=True))
        assert out["set"] is True and out["created"] is True
        assert out["before"] is None
        assert up.itemByName("NewP") is not None        # it was created
