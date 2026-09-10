"""Unit tests for ``sys_get_selection.py`` - the 'require' verdict and the bounded selection echo.

The per-entity record itself (``_classify`` / ``_geometry_handle``) is pinned in test__sys_common.py,
shared with sys_request_selection. What is proved here is this tool's own logic: the nothing-selected
refusal, the cap and its ceiling, the unreadable-selection refusal, and the require flag over a
walked prefix.
"""

import json

from conftest import (
    BRepBody,
    BRepFace,
    FakePoint,
    FakeSelection,
    FakeSelections,
    FakeUserInterface,
    FakeVector3D,
    Plane,
    load_tool,
)

sel = load_tool("sys_get_selection")


def _payload(result):
    return json.loads(result["content"][0]["text"])


def _fake_ui(entities=()):
    """A UserInterface whose activeSelections holds one picked entity per argument."""
    picks = [FakeSelection(entity=e, point=FakePoint(0, 0, 0)) for e in entities]
    return FakeUserInterface(FakeSelections(picks))


# ── handler 'require' mismatch flagging ────────────────────────────────────

class TestRequireFlag:
    """sys_get_selection should flag when the selection doesn't match 'require'."""

    def test_require_face_matches_a_face(self, monkeypatch):
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui([BRepFace(Plane(FakeVector3D(0, 0, 1)))]))
        result = sel.handler(require="face")
        payload = _payload(result)
        assert payload["matches_required"] is True

    def test_require_edge_flags_mismatch_when_face_selected(self, monkeypatch):
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui([BRepFace(Plane(FakeVector3D(0, 0, 1)))]))
        result = sel.handler(require="edge")
        payload = _payload(result)
        assert payload["matches_required"] is False
        assert "note" in payload

    def test_nothing_selected_is_an_error(self, monkeypatch):
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui([]))
        result = sel.handler()
        assert result["isError"] is True
        assert "Nothing is selected in Fusion" in result["message"]

    def test_a_full_walk_that_found_nothing_states_the_absence(self, monkeypatch):
        # nothing was left unread, so "does not include" is a verdict the read can back
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui([BRepBody(name="Block")]))
        out = _payload(sel.handler(require="face"))
        assert out["truncated"] is False
        assert out["matches_required"] is False
        assert "does not include a 'face'" in out["note"]


class TestRequireOverATruncatedWalk:
    """'require' is judged over the WALKED prefix. With 60 selections and the only face past the
    cap, a flat False says the selection holds no face - which is machine-checkably wrong. Absence
    over an unwalked tail is unknown (null), and the note says how far the walk got."""

    def test_a_required_kind_one_past_the_cap_is_unknown_not_absent(self, monkeypatch):
        entities = [BRepBody(name=f"B{i}") for i in range(50)]
        entities.append(BRepFace(Plane(FakeVector3D(0, 0, 1))))     # index 50 - one past the walk
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui(entities))
        out = _payload(sel.handler(require="face", max_results=50))
        assert out["truncated"] is True
        assert out["matches_required"] is None
        assert "first 50 of 51" in out["note"] and "unknown" in out["note"]
        assert "does not include" not in out["note"]

    def test_the_last_selection_inside_the_cap_still_counts_as_found(self, monkeypatch):
        # the other side of the same boundary: index cap-1 IS walked, so the face is really found
        entities = [BRepBody(name=f"B{i}") for i in range(49)]
        entities.append(BRepFace(Plane(FakeVector3D(0, 0, 1))))     # index 49 - the last one walked
        entities.append(BRepBody(name="tail"))
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui(entities))
        out = _payload(sel.handler(require="face", max_results=50))
        assert out["truncated"] is True
        assert out["matches_required"] is True
        assert "unknown" not in out["note"]


# ── BOUNDED READS: the selection echo is capped (CLAUDE.md "Bound it") ──────────────────────────

class TestSelectionCap:
    def test_under_cap_untruncated_and_unchanged(self, monkeypatch):
        entities = [BRepFace(Plane(FakeVector3D(0, 0, 1))) for _ in range(5)]
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui(entities))
        out = _payload(sel.handler())
        assert out["truncated"] is False
        assert len(out["selections"]) == 5
        assert out["selection_count"] == 5

    def test_at_cap_truncates_and_flags(self, monkeypatch):
        entities = [BRepFace(Plane(FakeVector3D(0, 0, 1))) for _ in range(60)]
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui(entities))
        out = _payload(sel.handler(max_results=50))
        assert out["truncated"] is True
        assert len(out["selections"]) == 50
        # the full count is still honest, even though the array is capped
        assert out["selection_count"] == 60

    def test_a_caller_cannot_lift_the_cap_past_the_ceiling(self, monkeypatch):
        # every record crosses the wire: max_results is clamped into 1..200, so an oversized
        # request is held at the ceiling, not honoured.
        entities = [BRepFace(Plane(FakeVector3D(0, 0, 1))) for _ in range(210)]
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui(entities))
        out = _payload(sel.handler(max_results=999999))
        assert len(out["selections"]) == 200
        assert out["truncated"] is True and out["selection_count"] == 210

    def test_a_selection_that_will_not_read_is_refused_not_quietly_dropped(self, monkeypatch):
        # the user picked three entities. Skipping the one that will not read publishes two records
        # under a count of three and calls the shortfall 'truncated' - the caller then acts on a
        # selection list that is missing the pick between the two it can see.
        ui = _fake_ui([BRepFace(Plane(FakeVector3D(0, 0, 1))) for _ in range(3)])
        sels = ui.activeSelections
        intact = sels.item

        def item(i):
            if i == 1:
                raise RuntimeError("4 : An API Object refers to a deleted Object")
            return intact(i)
        monkeypatch.setattr(sels, "item", item)
        monkeypatch.setattr(sel, "_ui", lambda: ui)
        res = sel.handler()
        assert res["isError"] is True
        assert "Could not read the selection" in res["message"]


class TestDeclaredHandleOutput:
    def test_handler_output_includes_handle(self, monkeypatch):
        face = BRepFace(Plane(FakeVector3D(0, 0, 1)), centroid=FakePoint(2, 2, 2), entity_token="TOK2")
        monkeypatch.setattr(sel, "_ui", lambda: _fake_ui(entities=[face]))
        out = _payload(sel.handler())
        assert out["selections"][0]["handle"] == "TOK2|@face:2.000000,2.000000,2.000000"
        # RETURNS contract: the declared 'handle' output key really is present in a list item.
        assert sel.RETURNS[0].assert_present(out) == ""
        assert "Produces" in out["note"]
