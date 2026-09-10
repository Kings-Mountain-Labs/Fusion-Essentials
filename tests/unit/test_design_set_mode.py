"""Unit tests for ``design_set_mode.py`` - the parametric <-> direct conversion.

Pinned: it REFUSES parametric->direct without confirm, SUCCEEDS with confirm, direct->parametric is
free (no confirm), it is an idempotent no-op when already in target, and BOTH published flags ride
on the post-assignment READ-BACK rather than on the request.
"""

import live_api_facts as _api_facts
from conftest import MakeDesign, error_message, install, load_tool, make_timeline, payload

dm = load_tool("design_set_mode")

_DESIGN_TYPES = _api_facts.ENUMS["fusion.DesignTypes"]
_PARAMETRIC = _DESIGN_TYPES["ParametricDesignType"]
_DIRECT = _DESIGN_TYPES["DirectDesignType"]


class _ModeDesign(MakeDesign):
    """A design whose designType assignment can refuse: `raise_on_set` models a real failure,
    `ignore_set` the platform lie - accepted, and nothing changes."""

    def __init__(self, design_type=_PARAMETRIC, timeline=None, raise_on_set=False,
                 ignore_set=False):
        self._raise_on_set = self._ignore_set = False
        super().__init__(design_type=design_type, timeline=timeline)
        self._raise_on_set, self._ignore_set = raise_on_set, ignore_set

    def __setattr__(self, name, value):
        if name == "designType":
            if getattr(self, "_raise_on_set", False):
                raise RuntimeError("designType assignment blew up")
            if getattr(self, "_ignore_set", False):
                return
        super().__setattr__(name, value)


class TestSetMode:
    def test_no_active_design(self):
        install(dm, None)
        res = dm.handler(target="direct", confirm_history_loss=True)
        assert "No active design" in error_message(res)

    def test_bad_target(self):
        install(dm, _ModeDesign())
        assert "must be one of" in error_message(dm.handler(target="hologram"))

    def test_parametric_to_direct_refused_without_confirm(self):
        des = install(dm, _ModeDesign())
        msg = error_message(dm.handler(target="direct"))            # no confirm
        assert "confirm_history_loss=true" in msg
        assert "DIRECT" in msg and "irreversible" in msg.lower()
        # and it must NOT have mutated the design
        assert des.designType == _PARAMETRIC

    def test_parametric_to_direct_succeeds_with_confirm(self):
        des = install(dm, _ModeDesign())
        out = payload(dm.handler(target="direct", confirm_history_loss=True))
        assert out["converted"] is True
        assert out["from"] == "parametric" and out["to"] == "direct"
        assert out["history_discarded"] is True
        assert des.designType == _DIRECT          # actually flipped to DirectDesignType

    def test_direct_to_parametric_is_free(self):
        # the asymmetry: no confirm needed, no history discarded
        des = install(dm, _ModeDesign(design_type=_DIRECT))
        out = payload(dm.handler(target="parametric"))
        assert out["converted"] is True
        assert out["from"] == "direct" and out["to"] == "parametric"
        assert out["history_discarded"] is False
        assert des.designType == _PARAMETRIC

    def test_idempotent_noop_when_already_target(self):
        des = install(dm, _ModeDesign(timeline=make_timeline("Extrude1")))
        out = payload(dm.handler(target="parametric"))
        assert out["converted"] is False and "Already" in out["note"]
        assert des.designType == _PARAMETRIC

    def test_assignment_exception_surfaces_not_swallowed(self):
        # a real failure on the mutation is surfaced as an error (never safe()-swallowed to a false ok)
        install(dm, _ModeDesign(design_type=_DIRECT, raise_on_set=True))
        assert "Could not convert" in error_message(dm.handler(target="parametric"))

    def test_history_discarded_rides_on_the_read_back_not_the_request(self):
        # the assignment is accepted and changes nothing (the measured platform lie). A conversion
        # that did not happen discarded no timeline - so BOTH flags read false, agreeing with the
        # note. history_discarded=true here would be the request talking, not the design.
        des = install(dm, _ModeDesign(ignore_set=True))
        out = payload(dm.handler(target="direct", confirm_history_loss=True))
        assert out["converted"] is False
        assert out["history_discarded"] is False
        assert out["now"] == "parametric" and des.designType == _PARAMETRIC
        assert "did not take" in out["note"]

    def test_an_unreadable_mode_publishes_null_flags_not_a_verdict(self):
        # designType does not decode to either mode after the assignment: whether the conversion
        # took, and so whether the timeline went with it, is UNKNOWN - null, never false or true.
        install(dm, _ModeDesign(design_type="?", ignore_set=True))
        out = payload(dm.handler(target="direct", confirm_history_loss=True))
        assert out["converted"] is None
        assert out["history_discarded"] is None
        assert out["now"] == "unknown"
        assert "UNCONFIRMED" in out["note"]

    def test_a_conversion_that_took_still_reports_the_discard(self):
        # the other side of the same gate: a PROVEN parametric->direct did discard the timeline.
        install(dm, _ModeDesign())
        out = payload(dm.handler(target="direct", confirm_history_loss=True))
        assert out["converted"] is True and out["history_discarded"] is True


class TestTargetEnum:
    """The schema's target vocabulary is _TARGETS itself, and each member converts to that mode."""

    def test_the_target_enum_matches_the_target_tuple(self):
        # Catches a HAND-EDITED schema drifting from the tuple the handler guards on.
        assert dm.tool.input_schema["properties"]["target"]["enum"] == list(dm._TARGETS)

    def test_every_advertised_target_converts_to_that_mode(self):
        # A member the guard accepts but no conversion arm resolves would assign the OTHER mode:
        # going_to_direct is a two-way branch, so an unrecognised third value reads as parametric.
        for name in dm._TARGETS:
            start = _DIRECT if name == "parametric" else _PARAMETRIC
            install(dm, _ModeDesign(design_type=start))
            out = payload(dm.handler(target=name, confirm_history_loss=True))
            assert out["converted"] is True and out["now"] == name, name
