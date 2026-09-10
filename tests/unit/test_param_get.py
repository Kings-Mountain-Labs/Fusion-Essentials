"""Unit tests for ``param_get.py`` - the parameter read path and its bounded walk.

The row itself (``_param_summary`` / ``_owner_facts``) is pinned in test__param_common.py, shared
with the param write tools; what is proved here is this tool's own logic: the default user-only
listing, the model-parameter de-dup, the single-name lookup, the _MAX_PARAMS clamp, and the note
that is only published when a row actually carries owner keys.
"""

from types import SimpleNamespace

from conftest import (FakeFeature, FakeModelParameter, FakeUserParameter, FakeUserParameters,
                      MakeDesign, load_tool, payload as _payload)

params = load_tool("param_get")


def _p(name, expression="1 mm", value=1.0, unit="mm"):
    """One user parameter row, at the defaults this file's listing assertions read past."""
    return FakeUserParameter(name=name, expression=expression, value=value, unit=unit)


def _design(user_parameters, all_params):
    return MakeDesign(user_parameters=user_parameters, all_parameters=list(all_params))


class _DimOwner:
    """A sketch dimension owner: no name of its own, but it knows its sketch."""

    parentSketch = SimpleNamespace(name="Sketch2")


class TestGetHandler:
    def test_no_active_design(self, monkeypatch):
        monkeypatch.setattr(params._common, "design", lambda: None)
        res = params.handler()
        assert res["isError"] is True and "No active design" in res["message"]

    def test_lists_user_parameters_only_by_default(self, monkeypatch):
        u1, u2 = _p("PartX"), _p("PartY")
        model_only = _p("d1")
        design = _design(FakeUserParameters([u1, u2]), [u1, u2, model_only])
        monkeypatch.setattr(params._common, "design", lambda: design)
        out = _payload(params.handler())
        assert out["user_parameter_count"] == 2
        assert {p["name"] for p in out["user_parameters"]} == {"PartX", "PartY"}
        assert "model_parameters" not in out

    def test_the_user_parameter_walk_is_clamped_at_max_params(self, monkeypatch):
        # the listing is bounded so a pathological design cannot flood the wire; the clamp is read
        # off the collection's own count, so it holds no matter how many items the walk yields
        monkeypatch.setattr(params, "_MAX_PARAMS", 3)
        ups = FakeUserParameters([_p("P%d" % i) for i in range(10)])
        monkeypatch.setattr(params._common, "design", lambda: _design(ups, []))
        out = _payload(params.handler())
        # the design's own total stays honest; 'returned' is what the walk actually read
        assert out["user_parameter_count"] == 10 and out["returned"] == 3
        assert [p["name"] for p in out["user_parameters"]] == ["P0", "P1", "P2"]

    def test_a_clamped_walk_says_the_counts_cover_only_what_it_reached(self, monkeypatch):
        # 'matched' and 'generated_skipped' are computed over the WALK, not the table, so a design
        # past the clamp reports tallies about a subset with nothing saying they are one.
        monkeypatch.setattr(params, "_MAX_PARAMS", 3)
        ups = FakeUserParameters([_p("P%d" % i) for i in range(10)])
        monkeypatch.setattr(params._common, "design", lambda: _design(ups, []))
        out = _payload(params.handler())
        assert out["walk_truncated"] is True
        assert "stopped at 3 of 10" in out["note"]

    def test_a_walk_that_reached_every_row_claims_no_gap(self, monkeypatch):
        monkeypatch.setattr(params, "_MAX_PARAMS", 3)
        ups = FakeUserParameters([_p("P%d" % i) for i in range(3)])
        monkeypatch.setattr(params._common, "design", lambda: _design(ups, []))
        out = _payload(params.handler())
        assert "walk_truncated" not in out and "note" not in out

    def test_the_row_page_is_capped_and_the_note_names_the_narrowing(self, monkeypatch):
        # MEASURED: 74 KB of parameter rows on one assembly. A capped page is only usable if the
        # payload says how to ask for less rather than handing back a place to read the rest.
        monkeypatch.setattr(params, "_ROWS_CAP", 2)
        ups = FakeUserParameters([_p("P%d" % i) for i in range(5)])
        monkeypatch.setattr(params._common, "design", lambda: _design(ups, []))
        out = _payload(params.handler())
        assert out["returned"] == 2 and out["matched"] == 5 and out["truncated"] is True
        assert "favorites_only" in out["note"] and "name=" in out["note"]

    def test_a_page_exactly_at_the_cap_is_not_flagged_truncated(self, monkeypatch):
        # the boundary: cap-many matching rows is a COMPLETE answer, and flagging it sends the
        # caller narrowing a list that was never cut.
        monkeypatch.setattr(params, "_ROWS_CAP", 2)
        ups = FakeUserParameters([_p("P0"), _p("P1")])
        monkeypatch.setattr(params._common, "design", lambda: _design(ups, []))
        out = _payload(params.handler())
        assert out["returned"] == 2 and "truncated" not in out


class TestGeneratedParameters:
    """MEASURED on the Airport Seating assembly: 258 of 311 user parameters were adsk_* rows minted
    by inserted standard screws. The authored set is what the modeller drives."""

    def _design_with(self, monkeypatch, names, favorites=()):
        ups = FakeUserParameters([FakeUserParameter(name=n, expression="1 mm", value=1.0,
                                                    unit="mm", favorite=(n in favorites))
                                  for n in names])
        design = _design(ups, [])
        monkeypatch.setattr(params._common, "design", lambda: design)
        return design

    def test_generated_rows_are_counted_not_listed(self, monkeypatch):
        self._design_with(monkeypatch, ["PartLen", "adsk_M6x20_Length", "adsk_M6x20_Pitch"])
        out = _payload(params.handler())
        assert [p["name"] for p in out["user_parameters"]] == ["PartLen"]
        assert out["generated_skipped"] == 2 and out["user_parameter_count"] == 3
        assert "include_generated=true" in out["note"]

    def test_include_generated_lists_them(self, monkeypatch):
        self._design_with(monkeypatch, ["PartLen", "adsk_M6x20_Length"])
        out = _payload(params.handler(include_generated=True))
        assert {p["name"] for p in out["user_parameters"]} == {"PartLen", "adsk_M6x20_Length"}
        assert "generated_skipped" not in out

    def test_the_prefix_match_is_case_insensitive_and_anchored(self, monkeypatch):
        # anchored, not contained: a parameter the modeller named 'my_adsk_ref' is authored, and
        # dropping it would hide a knob nothing else lists.
        self._design_with(monkeypatch, ["ADSK_Bolt_L", "my_adsk_ref"])
        out = _payload(params.handler())
        assert [p["name"] for p in out["user_parameters"]] == ["my_adsk_ref"]
        assert out["generated_skipped"] == 1

    def test_a_design_with_no_generated_rows_says_nothing_about_them(self, monkeypatch):
        self._design_with(monkeypatch, ["PartLen"])
        out = _payload(params.handler())
        assert "generated_skipped" not in out and "note" not in out

    def test_favorites_only_keeps_the_flagged_rows(self, monkeypatch):
        self._design_with(monkeypatch, ["PartLen", "PartWid"], favorites=["PartLen"])
        out = _payload(params.handler(favorites_only=True))
        assert [p["name"] for p in out["user_parameters"]] == ["PartLen"]
        assert out["matched"] == 1 and out["user_parameter_count"] == 2

    def test_favorites_only_off_keeps_every_row(self, monkeypatch):
        self._design_with(monkeypatch, ["PartLen", "PartWid"], favorites=["PartLen"])
        out = _payload(params.handler())
        assert len(out["user_parameters"]) == 2

    def test_the_authored_read_is_a_fraction_of_the_whole_table(self, monkeypatch):
        # MEASURED on the Airport Seating Primary Assembly sample (311 parameters: 53 authored, 258
        # adsk_*): 10.1 KB authored against 49.8 KB for every row, favorites_only 2.2 KB. On the
        # Bench sample all 350 read adsk_*, so the authored read is 239 bytes against 47.4 KB.
        import json
        self._design_with(monkeypatch, [f"Part{i}" for i in range(53)]
                          + [f"adsk_Screw{i}_Len" for i in range(258)])
        size = lambda p: len(json.dumps(p, separators=(",", ":")))
        authored = size(_payload(params.handler()))
        whole = size(_payload(params.handler(include_generated=True)))
        assert authored < 20_000 < whole

    def test_an_uncountable_collection_refuses_instead_of_reporting_zero(self, monkeypatch):
        # userParameters.count raising means the parameters could not be read AT ALL. Reporting
        # "user_parameter_count: 0" would read as "this design has no parameters" - a false answer.
        class _Uncountable(FakeUserParameters):
            @property
            def count(self):
                raise RuntimeError("parameter table is locked")
        monkeypatch.setattr(params._common, "design",
                            lambda: _design(_Uncountable([_p("PartX")]), []))
        res = params.handler()
        assert res["isError"] is True
        assert "could not read user parameters" in res["message"].lower()
        assert "locked" in res["message"]

    def test_include_model_parameters_dedups_user_names(self, monkeypatch):
        u1 = _p("PartX")
        model_only = _p("d1")
        design = _design(FakeUserParameters([u1]), [u1, model_only])
        monkeypatch.setattr(params._common, "design", lambda: design)
        out = _payload(params.handler(include_model_parameters=True))
        # PartX is already a user param -> not duplicated into model_parameters
        assert out["model_parameter_count"] == 1
        assert out["model_parameters"][0]["name"] == "d1"

    def test_model_rows_classify_a_user_omitted_by_filters_and_the_user_page(self, monkeypatch):
        monkeypatch.setattr(params, "_MAX_PARAMS", 2)
        generated = _p("adsk_Generated")
        filtered = _p("NotFavorite")
        capped = _p("AfterCap")
        model = _p("d1")
        ups = FakeUserParameters([generated, filtered, capped])
        design = _design(ups, [capped, model])
        monkeypatch.setattr(params._common, "design", lambda: design)
        out = _payload(params.handler(include_model_parameters=True, favorites_only=True))
        assert out["model_parameter_count"] == 1
        assert [row["name"] for row in out["model_parameters"]] == ["d1"]

    def test_a_capped_model_walk_discloses_the_observed_subset(self, monkeypatch):
        monkeypatch.setattr(params, "_MAX_PARAMS", 1)
        first, second = _p("d1"), _p("d2")
        design = _design(FakeUserParameters([]), [first, second])
        monkeypatch.setattr(params._common, "design", lambda: design)
        out = _payload(params.handler(include_model_parameters=True))
        assert out["model_parameter_count"] == 1 and out["model_walk_truncated"] is True
        assert "observed subset" in out["note"]

    def test_an_unreadable_user_lookup_does_not_label_the_parameter_model(self, monkeypatch):
        class Unclassifiable(FakeUserParameters):
            def itemByName(self, name):
                raise RuntimeError("user parameter table is locked")

        design = _design(Unclassifiable([]), [_p("d1")])
        monkeypatch.setattr(params._common, "design", lambda: design)
        res = params.handler(include_model_parameters=True)
        assert res["isError"] is True and "Could not classify parameter" in res["message"]

    def test_single_named_user_param(self, monkeypatch):
        u1 = _p("PartX", expression="50 mm", value=5.0)
        design = _design(FakeUserParameters([u1]), [u1])
        monkeypatch.setattr(params._common, "design", lambda: design)
        out = _payload(params.handler(name="PartX"))
        assert out["parameter"]["name"] == "PartX"
        assert out["parameter"]["value"] == 5.0

    def test_single_named_model_param_falls_through_to_all(self, monkeypatch):
        model_only = _p("d1", value=2.0)
        design = _design(FakeUserParameters([]), [model_only])
        monkeypatch.setattr(params._common, "design", lambda: design)
        out = _payload(params.handler(name="d1"))
        assert out["parameter"]["name"] == "d1"

    def test_single_named_missing_errors(self, monkeypatch):
        design = _design(FakeUserParameters([]), [])
        monkeypatch.setattr(params._common, "design", lambda: design)
        res = params.handler(name="Ghost")
        assert res["isError"] is True and "not found" in res["message"].lower()


class TestOwnerOnTheReadPath:
    def test_model_parameters_carry_their_owner_and_the_note_explains_the_keys(self, monkeypatch):
        u1 = _p("PartX")
        d1 = FakeModelParameter(name="d1", owner=FakeFeature("Extrude1"), role="Distance")
        design = _design(FakeUserParameters([u1]), [u1, d1])
        monkeypatch.setattr(params._common, "design", lambda: design)
        out = _payload(params.handler(include_model_parameters=True))
        row = out["model_parameters"][0]
        assert (row["owner"], row["role"]) == ("Extrude1", "Distance")
        assert "owner_type" in out["note"] and "role" in out["note"]

    def test_user_parameters_gain_no_owner_keys_so_the_default_read_stays_light(self, monkeypatch):
        u1 = _p("PartX")
        design = _design(FakeUserParameters([u1]), [u1])
        monkeypatch.setattr(params._common, "design", lambda: design)
        out = _payload(params.handler())
        assert not [k for k in out["user_parameters"][0] if k.startswith("owner")]
        assert "note" not in out

    def test_no_note_when_not_one_model_row_has_an_owner(self, monkeypatch):
        # a note describing owner keys that are not in the payload sends a caller looking for them.
        # Every model parameter answers a maker (measured), so the payload with no owner key at all
        # is the one carrying no model row: allParameters holding only the user parameter above it.
        u1 = _p("PartX")
        design = _design(FakeUserParameters([u1]), [u1])
        monkeypatch.setattr(params._common, "design", lambda: design)
        out = _payload(params.handler(include_model_parameters=True))
        assert out["model_parameter_count"] == 0
        assert "note" not in out

    def test_a_single_named_model_parameter_is_answered_with_its_owner(self, monkeypatch):
        # the cheapest form of the read: one parameter, one owner, no list to page through
        d195 = FakeModelParameter(name="d195", owner=_DimOwner(), role="Dimension")
        design = _design(FakeUserParameters([]), [d195])
        monkeypatch.setattr(params._common, "design", lambda: design)
        out = _payload(params.handler(name="d195"))
        assert out["parameter"]["owner_sketch"] == "Sketch2"
        assert out["parameter"]["role"] == "Dimension"
