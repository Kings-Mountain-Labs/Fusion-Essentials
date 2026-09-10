"""Unit tests for ``param_set_favorite.py`` - the flag is published as the parameter reads it back."""

from conftest import (FakeUserParameter, FakeUserParameters, MakeDesign, load_tool, make_timeline,
                      payload as _payload)

params = load_tool("param_set_favorite")


def _design(user_params, timeline, all_params=()):
    """A design carrying the two collections the param write path walks."""
    return MakeDesign(user_parameters=user_params, timeline=timeline,
                      all_parameters=list(all_params))


def _stub_design(monkeypatch, design):
    monkeypatch.setattr(params._common, "design", lambda: design)


class TestFavoriteHandler:
    def test_sets_favorite_flag(self, monkeypatch):
        p = FakeUserParameter(name="PartX", expression="10 mm")
        design = _design(FakeUserParameters([p]), make_timeline())
        _stub_design(monkeypatch, design)
        out = _payload(params.handler(name="PartX", favorite=True))
        assert out["favorite"] is True
        assert p.isFavorite is True

    def test_unknown_param_errors(self, monkeypatch):
        design = _design(FakeUserParameters([]), make_timeline())
        _stub_design(monkeypatch, design)
        res = params.handler(name="Ghost")
        assert res["isError"] is True and "No USER parameter" in res["message"]

    def test_favorite_set_failure_surfaces(self, monkeypatch):
        class Stubborn(FakeUserParameter):
            def __setattr__(self, k, v):
                if k == "isFavorite" and getattr(self, "_built", False):
                    raise RuntimeError("read-only")
                object.__setattr__(self, k, v)
        p = Stubborn(name="PartX", expression="10 mm")
        p._built = True
        design = _design(FakeUserParameters([p]), make_timeline())
        _stub_design(monkeypatch, design)
        res = params.handler(name="PartX", favorite=True)
        assert res["isError"] is True and "Could not set favorite" in res["message"]

    def test_empty_name_errors(self, monkeypatch):
        design = _design(FakeUserParameters([]), make_timeline())
        _stub_design(monkeypatch, design)
        res = params.handler(name="")
        assert res["isError"] is True
        assert "Provide 'name'" in res["message"]

    def test_a_stuck_flag_is_published_as_it_reads_not_as_asked(self, monkeypatch):
        # The payload's 'favorite' IS the post-write re-read, so an assignment the platform
        # swallows shows up as the flag that is actually there. A payload echoing the REQUEST
        # would report true over a parameter nothing was set on.
        class StuckFavorite(FakeUserParameter):
            @property
            def isFavorite(self):
                return False

            @isFavorite.setter
            def isFavorite(self, v):
                pass                                    # silently ignores the assignment

        p = StuckFavorite(name="PartX", expression="10 mm")
        design = _design(FakeUserParameters([p]), make_timeline())
        _stub_design(monkeypatch, design)
        out = _payload(params.handler(name="PartX", favorite=True))
        assert out["favorite"] is False                 # the no-op is visible in the payload
        assert out["name"] == "PartX"
