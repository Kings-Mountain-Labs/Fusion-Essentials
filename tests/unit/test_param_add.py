"""Unit tests for ``param_add.py`` - the single and batch adds, health-guarded.

add rolls back a parameter that introduces a NEW timeline error, and publishes the favorite flag
as the parameter reads it rather than as it was asked for. The fakes below model a tiny timeline
(items with healthState) and a userParameters collection that supports add/itemByName/deleteMe.
"""

from types import SimpleNamespace

import adsk.core

import live_api_facts
from conftest import (FakeTimeline, FakeTimelineObject, FakeUserParameter, FakeUserParameters,
                      MakeDesign, load_tool, make_timeline, payload as _payload)

params = load_tool("param_add")

_HEALTH = live_api_facts.ENUMS["fusion.FeatureHealthStates"]
_ERROR = _HEALTH["ErrorFeatureHealthState"]
_WARNING = _HEALTH["WarningFeatureHealthState"]


class BreakingUserParameters(FakeUserParameters):
    """userParameters.add that ALSO lands a broken feature in `timeline` - the downstream error the
    add's rollback guard is judged on."""

    def __init__(self, parameters=(), timeline=None):
        super().__init__(parameters)
        self._timeline = timeline

    def add(self, name, value_input, unit="", comment=""):
        param = super().add(name, value_input, unit, comment)
        self._timeline._items.append(FakeTimelineObject(name="BrokenFeature", health=_ERROR))
        return param


def _design(user_params, timeline, all_params=()):
    """A design carrying the two collections the param write path walks."""
    return MakeDesign(user_parameters=user_params, timeline=timeline,
                      all_parameters=list(all_params))


def _stub_design(monkeypatch, design):
    monkeypatch.setattr(params._common, "design", lambda: design)
    # the add path uses adsk.core.ValueInput.createByString; the string it carries is what the new
    # parameter's expression reads back as.
    monkeypatch.setattr(adsk.core.ValueInput, "createByString",
                        staticmethod(lambda s: SimpleNamespace(stringValue=s)))


class TestTimelineHealth:
    # the shared _timeline_health walk the add/delete rollback guard runs
    def test_rolls_up_errors_and_warnings(self):
        tl = FakeTimeline([FakeTimelineObject(name="A"),
                           FakeTimelineObject(name="B", health=_ERROR),
                           FakeTimelineObject(name="C", health=_WARNING),
                           FakeTimelineObject(name="D", health=_ERROR)])
        design = _design(FakeUserParameters(), tl)
        errors, warnings, total = params._timeline_health(design)
        assert total == 4
        assert errors == ["B", "D"]
        assert warnings == ["C"]


class TestAddHandler:
    def test_add_rejects_duplicate(self, monkeypatch):
        up = FakeUserParameters([FakeUserParameter(name="PartX", expression="10 mm")])
        design = _design(up, make_timeline())
        _stub_design(monkeypatch, design)
        res = params.handler(name="PartX", expression="5 mm")
        assert res["isError"] is True and "already exists" in res["message"]

    def test_add_succeeds_when_timeline_stays_healthy(self, monkeypatch):
        up = FakeUserParameters([])
        design = _design(up, make_timeline("A"))
        _stub_design(monkeypatch, design)
        out = _payload(params.handler(name="NewP", expression="3 mm"))
        assert out["added"] is True
        assert up.itemByName("NewP") is not None      # it stuck

    def test_add_rolls_back_on_new_timeline_error(self, monkeypatch):
        tl = make_timeline("A")
        up = BreakingUserParameters([], timeline=tl)   # adding will inject an error
        design = _design(up, tl)
        _stub_design(monkeypatch, design)
        res = params.handler(name="BadP", expression="oops")
        assert res["isError"] is True
        assert "rolled back" in res["message"]
        assert up.itemByName("BadP") is None          # removed again

    def test_add_requires_name_and_expression(self, monkeypatch):
        design = _design(FakeUserParameters(), FakeTimeline([]))
        _stub_design(monkeypatch, design)
        res1 = params.handler(name="", expression="5")
        assert res1["isError"] is True
        assert "Missing 'name'" in res1["message"]
        res2 = params.handler(name="X", expression="")
        assert res2["isError"] is True
        assert "missing 'expression'" in res2["message"]


class TestAddBatch:
    # Adding N parameters is ONE batch call, not N separate calls.
    def test_batch_adds_all(self, monkeypatch):
        up = FakeUserParameters([])
        design = _design(up, make_timeline("A"))
        _stub_design(monkeypatch, design)
        out = _payload(params.handler(params=[
            {"name": "WheelDia", "expression": "350 mm"},
            {"name": "AxleDia", "expression": "14 mm", "favorite": True},
            {"name": "CrankLen", "expression": "125 mm", "comment": "arm"},
        ]))
        assert out["added_count"] == 3
        assert {r["parameter"]["name"] for r in out["results"]} == {"WheelDia", "AxleDia", "CrankLen"}
        for nm in ("WheelDia", "AxleDia", "CrankLen"):
            assert up.itemByName(nm) is not None

    def test_batch_stops_and_reports_the_failing_entry(self, monkeypatch):
        up = FakeUserParameters([])
        design = _design(up, make_timeline("A"))
        _stub_design(monkeypatch, design)
        # 2nd entry is missing an expression -> that entry errors, the batch reports which index
        res = params.handler(params=[
            {"name": "Good", "expression": "1 mm"},
            {"name": "Bad", "expression": ""},
        ])
        assert res["isError"] is True
        assert "Bad" in res["message"] and "[1]" in res["message"]
        assert up.itemByName("Good") is not None        # the earlier good one is kept

    def test_single_param_path_still_works(self, monkeypatch):
        up = FakeUserParameters([])
        design = _design(up, make_timeline("A"))
        _stub_design(monkeypatch, design)
        out = _payload(params.handler(name="Solo", expression="9 mm"))
        assert out["added"] is True and up.itemByName("Solo") is not None


class TextRefusingUserParameters(FakeUserParameters):
    """userParameters.add as a TEXT parameter measures (measure_api.py row parameter-favorite-maker-
    text-value-and-fresh-appearances): under units 'Text' the expression must be a QUOTED literal,
    and the same string unquoted is refused right at the add with "3 : Invalid expression". A name
    Fusion will not take is refused by the SAME call in its own words - "3 : param name is not
    valid" - which is what makes the two failures tellable apart only by the message."""

    def add(self, name, value_input, unit="", comment=""):
        if " " in name or "!" in name:
            raise RuntimeError("3 : param name is not valid")
        expression = getattr(value_input, "stringValue", "") or ""
        if unit == "Text" and "'" not in expression:
            raise RuntimeError("3 : Invalid expression")
        return super().add(name, value_input, unit, comment)


class UnitRefusingUserParameters(FakeUserParameters):
    """userParameters.add refusing outright whatever it was handed as a unit - the seam that decides
    what the tool's except path has to survive. Its wording stands for a refusal, not for Fusion's."""

    def add(self, name, value_input, unit="", comment=""):
        if not isinstance(unit, str):
            raise RuntimeError("3 : the unit was refused")
        return super().add(name, value_input, unit, comment)


class TestTextParameter:
    def test_an_unquoted_text_expression_is_refused_with_the_quoting_rule(self, monkeypatch):
        # Fusion's own refusal says "Invalid expression" and nothing else, which leaves a caller
        # guessing between the unit, the name and the expression - so the add carries the one
        # correction that fixes it, spelled with the quotes.
        up = TextRefusingUserParameters([])
        _stub_design(monkeypatch, _design(up, make_timeline("A")))
        res = params.handler(name="Strategy", expression="Roughing", unit="Text")
        assert res["isError"] is True
        assert "Invalid expression" in res["message"]        # what Fusion said
        assert "\"'Roughing'\"" in res["message"]            # the expression that would have worked
        assert up.itemByName("Strategy") is None

    def test_a_quoted_text_expression_lands_under_the_text_unit(self, monkeypatch):
        # The other half: 'Text' is not translated or defaulted away on the road to Fusion, so the
        # quoted literal the caller wrote is what the parameter ends up holding.
        up = TextRefusingUserParameters([])
        _stub_design(monkeypatch, _design(up, make_timeline("A")))
        out = _payload(params.handler(name="Strategy", expression="'Roughing'", unit="Text"))
        assert out["added"] is True
        assert up._added[0][2] == "Text"
        assert up.itemByName("Strategy").expression == "'Roughing'"

    def test_a_name_refusal_under_the_text_unit_carries_no_quoting_remedy(self, monkeypatch):
        # Same unit, same unquoted expression - and a DIFFERENT failure. The remedy answers the
        # refusal Fusion actually returned, so it reads that message rather than re-deriving a
        # cause from the inputs, which are equally consistent with the wrong explanation.
        up = TextRefusingUserParameters([])
        _stub_design(monkeypatch, _design(up, make_timeline("A")))
        res = params.handler(name="bad name!", expression="Roughing", unit="Text")
        assert res["isError"] is True
        assert "param name is not valid" in res["message"]
        assert "QUOTED" not in res["message"]

    def test_a_non_string_unit_in_a_batch_errors_instead_of_raising(self, monkeypatch):
        # 'params' items are plain objects in the schema, so a unit reaches the add as any JSON
        # value a caller sends. Whatever the add then does, building the failure message is the
        # last step and may not itself raise - an exception out of the handler is not a payload.
        up = UnitRefusingUserParameters([])
        _stub_design(monkeypatch, _design(up, make_timeline("A")))
        res = params.handler(params=[{"name": "Loose", "expression": "1", "unit": 5}])
        assert res["isError"] is True
        assert "Loose" in res["message"]


class TestAddFavorite:
    def test_favorite_reported_from_param_state(self, monkeypatch):
        up = FakeUserParameters([])
        design = _design(up, make_timeline("A"))
        _stub_design(monkeypatch, design)
        out = _payload(params.handler(name="P", expression="5 mm", favorite=True))
        assert out["favorite"] is True
        assert up.itemByName("P").isFavorite is True

    def test_a_stuck_favorite_is_published_as_the_parameter_reads_it(self, monkeypatch):
        # The add payload is a READ of the parameter that landed, never an echo of the request: the
        # isFavorite assignment here is accepted and changes nothing, so both the flag and the
        # parameter row report the state the parameter actually carries. Echoing the request would
        # report favorite:true over a parameter nothing was set on.
        class StuckFavoriteParam(FakeUserParameter):
            @property
            def isFavorite(self):
                return False

            @isFavorite.setter
            def isFavorite(self, value):
                pass                                  # silently ignores the assignment

        class StuckFavoriteParams(FakeUserParameters):
            def add(self, name, value_input, unit="", comment=""):
                p = StuckFavoriteParam(name=name, unit=unit, comment=comment)
                self._parameters.append(p)
                return p

        up = StuckFavoriteParams([])
        design = _design(up, make_timeline("A"))
        _stub_design(monkeypatch, design)
        out = _payload(params.handler(name="NewP", expression="3 mm", favorite=True))
        assert out["added"] is True
        assert out["favorite"] is False                # as it READS, not as it was asked for
        assert out["parameter"]["favorite"] is False   # the same read, in the parameter row
        assert out["parameter"]["name"] == "NewP"
        assert up.itemByName("NewP").isFavorite is False
