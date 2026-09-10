"""Unit tests for surface_reverse_normal - flip open-surface normals with an isParamReversed read-back.

Pins: ALL input bodies are handed to reverseNormalFeatures.add; reversed_confirmed is TRUE only when
the isParamReversed read-back shows every face toggled and FALSE (an honest ok, not an error) when it
doesn't; a solid body is rejected; missing bodies is rejected; a null feature is an error.

Uses the shared install()/make_design() plumbing (both design seams patched) per tests/CLAUDE.md; the
shared BRepBody/BRepFace fakes are monkeypatched onto adsk.fusion so the input-kind isinstance
checks fire.
"""

import types

import adsk.fusion
import pytest

from conftest import (load_tool, make_design, install, payload, error_message, MakeComp,
                      BRepBody, BRepFace, FakeFeature)

srn = load_tool("surface_reverse_normal")


def _face(reversed_=False, body=None):
    """One face carrying the isParamReversed flag a normal flip toggles."""
    return BRepFace(None, param_reversed=reversed_, body=body)


def _body(name, is_solid=False, faces=()):
    """An open surface body whose faces point back at it, the way a live body's do."""
    body = BRepBody(name, is_solid=is_solid, faces=faces)
    for f in faces:
        if f.body is None:
            f.body = body
    return body


class _Feature(FakeFeature):
    """A ReverseNormalFeature - the shared feature under this tool's result name."""
    def __init__(self, name="ReverseNormal1", bodies=(), faces=()):
        super().__init__(name=name, bodies=bodies, faces=faces)


class _RevFeatures:
    def __init__(self, result):
        self._result = result
        self.calls = []          # each entry is the ObjectCollection add() received
    def add(self, coll):
        self.calls.append(coll)
        r = self._result
        return r(coll) if callable(r) else r


@pytest.fixture(autouse=True)
def _types(monkeypatch):
    monkeypatch.setattr(adsk.fusion, "BRepBody", BRepBody, raising=False)
    monkeypatch.setattr(adsk.fusion, "BRepFace", BRepFace, raising=False)


def _wire(rev_result, tokens):
    comp = MakeComp()
    comp.features = types.SimpleNamespace(reverseNormalFeatures=_RevFeatures(rev_result))
    design = make_design(comp=comp, tokens=tokens)
    install(srn, design)
    return comp.features.reverseNormalFeatures


def _flip_result(coll):
    """A realistic feature: every input body's faces come back with isParamReversed toggled."""
    result_bodies, flipped = [], []
    for b in coll:
        ff = [_face(reversed_=not f.isParamReversed) for f in b.faces]
        flipped.extend(ff)
        result_bodies.append(_body(b.name, faces=ff))
    return _Feature(bodies=result_bodies, faces=flipped)


def _noop_result(coll):
    """A feature that changed nothing - the read-back must NOT confirm a flip."""
    result_bodies, same = [], []
    for b in coll:
        ff = [_face(reversed_=f.isParamReversed) for f in b.faces]
        same.extend(ff)
        result_bodies.append(_body(b.name, faces=ff))
    return _Feature(bodies=result_bodies, faces=same)


def test_confirms_flip_via_isparamreversed_readback():
    body = _body("Srf1", faces=[_face(False), _face(False)])
    _wire(_flip_result, {"H1": body})
    out = payload(srn.reverse_normal_handler(bodies=["H1"]))
    assert out["reversed"] is True
    assert out["reversed_confirmed"] is True
    assert out["faces_total"] == 2
    assert out["reversed_before"] == 0 and out["reversed_after"] == 2


def test_noop_reported_honestly_not_confirmed():
    body = _body("Srf1", faces=[_face(False), _face(False)])
    _wire(_noop_result, {"H1": body})
    res = srn.reverse_normal_handler(bodies=["H1"])
    assert res["isError"] is False            # honest ok, not an error
    out = payload(res)
    assert out["reversed_confirmed"] is False
    assert "did NOT confirm" in out["note"]


def test_all_input_bodies_handed_to_add():
    b1 = _body("Srf1", faces=[_face(False)])
    b2 = _body("Srf2", faces=[_face(True)])
    rev = _wire(_flip_result, {"H1": b1, "H2": b2})
    out = payload(srn.reverse_normal_handler(bodies=["H1", "H2"]))
    assert out["body_count"] == 2
    assert rev.calls and rev.calls[0].count == 2      # both bodies in the collection

def test_solid_body_rejected():
    solid = _body("Block", is_solid=True, faces=[_face(False)])
    _wire(_flip_result, {"H1": solid})
    res = srn.reverse_normal_handler(bodies=["H1"])
    msg = error_message(res)
    assert "SURFACE" in msg or "SOLID" in msg


def test_missing_bodies_rejected():
    _wire(_flip_result, {})
    res = srn.reverse_normal_handler(bodies=None)
    assert res["isError"] is True
    assert "needs at least one body" in res["message"]


def test_null_feature_is_error():
    body = _body("Srf1", faces=[_face(False)])
    _wire(lambda coll: None, {"H1": body})
    res = srn.reverse_normal_handler(bodies=["H1"])
    assert error_message(res)
    assert "no feature" in error_message(res)
