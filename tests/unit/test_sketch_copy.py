"""Unit tests for sketch_copy.py - the copy's target scope and its new-curve refs."""

import math
import types
import adsk.core
import pytest
from conftest import FakeBoundingBox3D, FakePoint, error_message, install, load_tool, make_design, make_sketch, make_sketch_curve, payload


def _rebox(curve):
    """Re-derive the bounding box from the endpoints, the way Fusion's is derived."""
    a, b = curve.startSketchPoint.geometry, curve.endSketchPoint.geometry
    curve.boundingBox = FakeBoundingBox3D(
        FakePoint(min(a.x, b.x), min(a.y, b.y), 0.0), FakePoint(max(a.x, b.x), max(a.y, b.y), 0.0))


def _boxed(token, x, y, dx=1.0, dy=0.0, length=10.0):
    """A sketch LINE running (x,y) -> (x+dx, y+dy): the start/end sketch points AND the bounding box
    they imply - the two samples entity_position reads."""
    curve = make_sketch_curve(token, length=length)
    curve.startSketchPoint = types.SimpleNamespace(geometry=FakePoint(x, y, 0.0))
    curve.endSketchPoint = types.SimpleNamespace(geometry=FakePoint(x + dx, y + dy, 0.0))
    _rebox(curve)
    return curve


def _matrix():
    """A Matrix3D: identity cells plus the setters the tool drives. setToRotation writes the 2D
    rotation block, setCell/getCell address the 4x4, and `translation` is a plain assignment."""
    cells = [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]
    m = types.SimpleNamespace(cells=cells, translation=FakePoint(0.0, 0.0, 0.0), angle=None)

    def set_to_rotation(angle, axis, origin):
        m.angle = angle
        cells[0][0], cells[0][1] = math.cos(angle), -math.sin(angle)
        cells[1][0], cells[1][1] = math.sin(angle), math.cos(angle)
        return True

    def set_cell(row, col, value):
        cells[row][col] = value
        return True

    m.setToRotation = set_to_rotation
    m.setCell = set_cell
    m.getCell = lambda row, col: cells[row][col]
    return m


def _proxy_of(curve):
    """The assembly-context PROXY a component-owned sketch hands back from copy(), as measured: a
    different Python object whose assemblyContext names the occurrence and whose OWN entityToken is a
    different (248-char) token than the landed native curve's (192-char) - so neither identity nor
    token matches it. Only nativeObject bridges back to the landed curve."""
    token = getattr(curve, "entityToken", None)
    return types.SimpleNamespace(
        assemblyContext=types.SimpleNamespace(name="TokProbe:1"),
        entityToken=(token + "@occurrence-proxy") if token else None,
        nativeObject=curve, length=curve.length)


def _copier(store, target, made=(), extra_points=0, proxies=False, unreffable=()):
    """A Sketch.copy that lands `made` in `target` and returns them plus `extra_points` sketch
    points - the measured shape, where copying ONE line hands back three entities.

    proxies=True returns each landed curve as the assembly-context proxy a COMPONENT-owned sketch
    hands back (see _proxy_of); the default returns the native curve itself with nativeObject None,
    the measured root-owned shape. `unreffable` curves land in the flat sketchCurves collection only,
    in no per-kind sub-collection, so they raise the curve COUNT while no ref can name them."""
    def _copy(collection, matrix, target_sketch=None):
        store.append((collection, matrix, target_sketch))
        landed = target_sketch if target_sketch is not None else target
        result = adsk.core.ObjectCollection.create()
        for curve, reffable in [(c, True) for c in made] + [(c, False) for c in unreffable]:
            curve.nativeObject = None          # a native entity reports no nativeObject
            landed.sketchCurves._items.append(curve)
            if reffable:
                landed.sketchCurves.sketchLines._items.append(curve)
            result.add(_proxy_of(curve) if proxies else curve)
        for i in range(extra_points):
            point = make_sketch_curve(f"PT{i}")
            point.nativeObject = None
            result.add(_proxy_of(point) if proxies else point)
        return result
    return _copy


@pytest.fixture
def mod():
    return load_tool("sketch_copy")


@pytest.fixture
def sketches(mod, monkeypatch):
    """'Plate' (line:0 at x=0, line:1 at x=5) and an empty 'Other'. Plate is created LAST, so the
    default (most recent) sketch is the one the tests drive."""
    plate = make_sketch("Plate", lines=[_boxed("L0", 0.0, 0.0), _boxed("L1", 5.0, 0.0)])
    second = make_sketch("Other", lines=[])
    design = make_design(sketches=[second, plate])
    install(mod, design)
    monkeypatch.setattr(adsk.core.Matrix3D, "create", lambda: _matrix())
    monkeypatch.setattr(adsk.core.Vector3D, "create", lambda x, y, z: FakePoint(x, y, z))
    monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
    return plate, second


def _lines(sketch):
    return sketch.sketchCurves.sketchLines


def _stale_item_at(monkeypatch, collection, index):
    """Make collection.item(index) RAISE - the stale entity proxy that yields no token."""
    intact = collection.item

    def item(i):
        if i == index:
            raise RuntimeError("4 : An API Object refers to a deleted Object")
        return intact(i)
    monkeypatch.setattr(collection, "item", item)


@pytest.fixture
def shared_name(mod, monkeypatch):
    """One name across two components, DIFFERENT geometry: Alpha's 'Plate' has two lines starting
    at x=0 and x=5, Beta's has ONE at x=20. The moved corner says which sketch answered - two
    equal-sized sketches would hide a swapped source."""
    from conftest import MakeComp
    alpha_sk = make_sketch("Plate", lines=[_boxed("A0", 0.0, 0.0), _boxed("A1", 5.0, 0.0)])
    beta_sk = make_sketch("Plate", lines=[_boxed("B0", 20.0, 0.0)])
    alpha = MakeComp(name="Alpha", sketches=[alpha_sk])
    beta = MakeComp(name="Beta", sketches=[beta_sk])
    install(mod, make_design(comp=alpha, all_components=[alpha, beta]))
    monkeypatch.setattr(adsk.core.Matrix3D, "create", lambda: _matrix())
    monkeypatch.setattr(adsk.core.Vector3D, "create", lambda x, y, z: FakePoint(x, y, z))
    monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
    return alpha_sk, beta_sk


class TestCopy:

    def test_copy_returns_the_new_curve_refs_and_the_append_note(self, mod, sketches):
        plate, _ = sketches
        calls = []
        plate.copy = _copier(calls, plate, made=[_boxed("L2", 9.0, 0.0)], extra_points=2)
        out = payload(mod.handler(entities="line:0", dx=10))
        assert out["new_curves"] == ["line:2"]
        assert out["curve_count_before"] == 2 and out["curve_count_after"] == 3
        # the returned collection carries the copied endpoints too - measured 3 for one line
        assert out["returned_entity_count"] == 3
        # an added curve APPENDS at the end of its kind's collection, so the ids in use keep their
        # entities; a note claiming a renumber sends the caller re-reading ids that never moved
        assert "APPENDS" in out["note"] and "RENUMBER" not in out["note"].upper()

    def test_a_same_sketch_copy_uses_the_two_argument_form(self, mod, sketches):
        plate, _ = sketches
        calls = []
        plate.copy = _copier(calls, plate, made=[_boxed("L2", 9.0, 0.0)])
        out = payload(mod.handler(entities="line:0", dx=10))
        assert calls[0][2] is None and out["target_sketch"] == "Plate"

    def test_a_cross_sketch_copy_verifies_the_target_not_the_source(self, mod, sketches):
        plate, second = sketches
        calls = []
        plate.copy = _copier(calls, plate, made=[_boxed("L9", 9.0, 0.0)])
        out = payload(mod.handler(entities="line:0", target_sketch="Other", dx=10))
        assert calls[0][2] is second
        assert out["target_sketch"] == "Other" and out["new_curves"] == ["line:0"]
        assert out["curve_count_before"] == 0 and out["curve_count_after"] == 1
        assert _lines(plate).count == 2       # the source is untouched

    def test_a_copy_that_lands_nothing_in_the_target_is_an_error(self, mod, sketches):
        plate, second = sketches
        plate.copy = _copier([], plate, made=[], extra_points=2)
        msg = error_message(mod.handler(entities="line:0", target_sketch="Other", dx=10))
        assert "still holds 0 curve(s)" in msg and "nothing landed" in msg

    def test_a_null_return_is_an_error_not_a_silent_success(self, mod, sketches):
        plate, _ = sketches
        plate.copy = lambda *args: None
        msg = error_message(mod.handler(entities="line:0", dx=10))
        assert "copy returned no collection" in msg and "nothing was copied" in msg

    def test_an_unknown_target_sketch_lists_the_available_ones(self, mod, sketches):
        msg = error_message(mod.handler(entities="line:0", target_sketch="Ghost", dx=10))
        assert "No sketch named 'Ghost'" in msg and "Other" in msg

    def test_a_shared_target_sketch_name_is_refused_with_its_owners(self, mod, sketches,
                                                                    monkeypatch):
        # The target_sketch lookup is its own design-wide resolve: a name SEVERAL sketches carry is
        # refused naming each owner, and nothing is copied - "No sketch named 'Other'" would state
        # the opposite of what the walk read.
        plate, _second = sketches
        calls = []
        plate.copy = _copier(calls, plate, made=[_boxed("L2", 9.0, 0.0)])
        refusal = "2 sketches are named 'Other' ('Other' in Root, 'Other' in Frame)"
        monkeypatch.setattr(mod._common, "find_sketch", lambda d, n, remedy=None: (None, refusal))
        msg = error_message(mod.handler(entities="line:0", target_sketch="Other", dx=10))
        assert msg == refusal and "No sketch named" not in msg
        assert calls == []          # nothing was copied


class TestCopyTargetComponentScope:

    """'target_component' narrows the DESTINATION. It is a separate input because 'component'
    narrows the source, and a refusal has to name the one that would actually change the answer."""

    def test_a_shared_target_name_refuses_naming_target_component_not_component(self, mod,
                                                                                shared_name):
        alpha_sk, beta_sk = shared_name
        calls = []
        alpha_sk.copy = _copier(calls, alpha_sk, made=[_boxed("A2", 9.0, 0.0)])
        msg = error_message(mod.handler(sketch_name="Plate", component="Alpha",
                                             entities="line:0", target_sketch="Plate", dx=10))
        assert "2 sketches are named 'Plate'" in msg
        assert "'target_component'" in msg and "Rename one" not in msg
        assert calls == []

    def test_target_component_selects_the_destination_sketch(self, mod, shared_name):
        alpha_sk, beta_sk = shared_name
        calls = []
        alpha_sk.copy = _copier(calls, alpha_sk, made=[_boxed("A2", 9.0, 0.0)])
        out = payload(mod.handler(sketch_name="Plate", component="Alpha", entities="line:0",
                                       target_sketch="Plate", target_component="Beta", dx=10))
        # the destination handed to Sketch.copy is BETA's sketch, and the curve landed there
        assert calls[0][2] is beta_sk
        assert out["curve_count_after"] == 2 and len(_lines(alpha_sk)._items) == 2

    def test_an_unknown_target_component_is_refused_before_the_copy(self, mod, shared_name):
        alpha_sk, _beta_sk = shared_name
        calls = []
        alpha_sk.copy = _copier(calls, alpha_sk, made=[_boxed("A2", 9.0, 0.0)])
        msg = error_message(mod.handler(sketch_name="Plate", component="Alpha",
                                             entities="line:0", target_sketch="Plate",
                                             target_component="Gamma", dx=10))
        assert "No component named 'Gamma'" in msg and calls == []

    def test_a_scoped_MISS_names_target_component_and_never_the_bare_component(self, mod,
                                                                               monkeypatch):
        # The scoped-miss refusal ("Retry with one of those as ...") has to name the input that
        # narrows THIS reference. 'component' narrows the SOURCE, so quoting it sends the caller to
        # an input that cannot change which destination was found.
        from conftest import MakeComp
        src = make_sketch("Source", lines=[_boxed("S0", 0.0, 0.0)])
        alpha_sk = make_sketch("Plate", lines=[_boxed("A0", 0.0, 0.0)])
        alpha = MakeComp(name="Alpha", sketches=[src, alpha_sk])
        beta = MakeComp(name="Beta", sketches=[make_sketch("Other", lines=[])])
        install(mod, make_design(comp=alpha, all_components=[alpha, beta]))
        monkeypatch.setattr(adsk.core.Matrix3D, "create", lambda: _matrix())
        monkeypatch.setattr(adsk.core.Vector3D, "create", lambda x, y, z: FakePoint(x, y, z))
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        calls = []
        src.copy = _copier(calls, src, made=[_boxed("S2", 9.0, 0.0)])
        msg = error_message(mod.handler(sketch_name="Source", entities="line:0",
                                             target_sketch="Plate", target_component="Beta",
                                             dx=10))
        assert "holds no sketch named 'Plate'" in msg
        assert "'target_component'" in msg
        assert "'component'" not in msg
        assert calls == []

    def test_an_AMBIGUOUS_target_component_names_target_component(self, mod, monkeypatch):
        # Component names are not unique either, and that refusal offers the occurrence-path
        # spelling - of the input that actually narrows the destination.
        from conftest import MakeComp, make_occurrence
        src = make_sketch("Source", lines=[_boxed("S0", 0.0, 0.0)])
        root = MakeComp(name="Root", sketches=[src])
        a = MakeComp(name="Frame", sketches=[make_sketch("Plate", lines=[])])
        b = MakeComp(name="Frame", sketches=[make_sketch("Plate", lines=[])])
        root.allOccurrences = [make_occurrence("P2-Gimbal:1+Frame:1", a),
                               make_occurrence("P3-Gimbal:1+Frame:1", b)]
        install(mod, make_design(comp=root, all_components=[root, a, b]))
        monkeypatch.setattr(adsk.core.Matrix3D, "create", lambda: _matrix())
        monkeypatch.setattr(adsk.core.Vector3D, "create", lambda x, y, z: FakePoint(x, y, z))
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        calls = []
        src.copy = _copier(calls, src, made=[_boxed("S2", 9.0, 0.0)])
        msg = error_message(mod.handler(sketch_name="Source", entities="line:0",
                                             target_sketch="Plate", target_component="Frame",
                                             dx=10))
        assert "2 components match 'Frame'" in msg
        assert "'target_component' also takes an occurrence fullPathName" in msg
        assert "'component'" not in msg
        assert calls == []

    def test_a_wrong_target_component_is_refused_even_when_the_target_name_is_UNIQUE(
            self, mod, monkeypatch):
        # The validation decision, at the ALTERNATE scope: a target_component quietly dropped
        # because 'Plate' happened to resolve copies into Alpha on a call that named Beta.
        from conftest import MakeComp
        src = make_sketch("Source", lines=[_boxed("S0", 0.0, 0.0)])
        alpha_sk = make_sketch("Plate", lines=[])
        alpha = MakeComp(name="Alpha", sketches=[src, alpha_sk])
        beta = MakeComp(name="Beta", sketches=[])
        install(mod, make_design(comp=alpha, all_components=[alpha, beta]))
        monkeypatch.setattr(adsk.core.Matrix3D, "create", lambda: _matrix())
        monkeypatch.setattr(adsk.core.Vector3D, "create", lambda x, y, z: FakePoint(x, y, z))
        monkeypatch.setattr(adsk.core.Point3D, "create", lambda x, y, z: FakePoint(x, y, z))
        calls = []
        src.copy = _copier(calls, src, made=[_boxed("S2", 9.0, 0.0)])
        msg = error_message(mod.handler(sketch_name="Source", entities="line:0",
                                             target_sketch="Plate", target_component="Beta",
                                             dx=10))
        assert "'Beta'" in msg and calls == []


class TestCopyRefsCrossTheProxySeam:

    """A sketch owned by a COMPONENT hands the copy back as assembly-context PROXIES while its own
    collections hold the NATIVE curves. Measured: neither identity nor entityToken crosses that seam -
    the proxy's own 248-char token is a different token from the landed curve's 192-char one - so the
    refs are resolved through nativeObject, which matches the landed curve by identity AND token and
    reads None for an already-native entity."""

    def test_a_component_sketch_copy_still_reports_its_new_refs(self, mod, sketches):
        plate, _ = sketches
        plate.copy = _copier([], plate, made=[_boxed("L2", 9.0, 0.0)], extra_points=2, proxies=True)
        out = payload(mod.handler(entities="line:0", dx=10))
        assert out["new_curves"] == ["line:2"]
        assert out["curve_count_before"] == 2 and out["curve_count_after"] == 3
        assert "new_curves_complete" not in out

    def test_the_proxys_own_token_is_not_what_matched(self, mod, sketches):
        # the seam itself: the returned proxy's token is absent from the target's token map, so a
        # token-only read-back finds nothing - the ref can only have come through nativeObject
        plate, _ = sketches
        landed = _boxed("L2", 9.0, 0.0)
        plate.copy = _copier([], plate, made=[landed], proxies=True)
        out = payload(mod.handler(entities="line:0", dx=10))
        proxy = _proxy_of(landed)
        assert proxy.entityToken != landed.entityToken
        assert proxy.entityToken not in mod._curve_ref_by_token(plate)
        assert out["new_curves"] == ["line:2"]

    def test_a_cross_sketch_component_copy_reads_the_target_back(self, mod, sketches):
        plate, second = sketches
        plate.copy = _copier([], plate, made=[_boxed("L8", 9.0, 0.0), _boxed("L9", 9.0, 2.0)],
                             proxies=True)
        out = payload(mod.handler(entities="line:0", target_sketch="Other", dx=10))
        assert out["new_curves"] == ["line:0", "line:1"] and out["target_sketch"] == "Other"

    def test_a_root_sketch_copy_matches_on_the_entity_itself(self, mod, sketches):
        # nativeObject reads None for an already-native entity, so the same expression must fall
        # through to the returned entity - the root-owned case, where its token IS in the map
        plate, _ = sketches
        plate.copy = _copier([], plate, made=[_boxed("L2", 9.0, 0.0)], extra_points=2)
        out = payload(mod.handler(entities="line:0", dx=10))
        assert out["new_curves"] == ["line:2"] and "new_curves_complete" not in out

    def test_an_ambiguous_token_is_resolved_by_native_identity(self, mod, sketches):
        # the two pieces a split returns share ONE token, so the token map drops it - identity on
        # the NATIVE entity still names the exact curve rather than guessing between them
        plate, _ = sketches
        plate.copy = _copier([], plate, made=[_boxed("L0", 9.0, 0.0)], proxies=True)
        out = payload(mod.handler(entities="line:0", dx=10))
        assert out["new_curves"] == ["line:2"] and "new_curves_complete" not in out

    def test_a_curve_no_ref_addresses_is_reported_not_silently_dropped(self, mod, sketches):
        # a curve landing in no per-kind sub-collection raises the curve count with no ref to name
        # it - the count delta is then the whole truth, and saying so beats handing back a bare []
        plate, _ = sketches
        plate.copy = _copier([], plate, made=[], unreffable=[_boxed("C0", 9.0, 0.0)], proxies=True)
        out = payload(mod.handler(entities="line:0", dx=10))
        assert out["new_curves"] == [] and out["new_curves_complete"] is False
        assert "1 curve(s) landed in 'Plate'" in out["note"]
        assert "NONE could be identified" in out["note"]
        assert out["curve_count_before"] == 2 and out["curve_count_after"] == 3

    def test_a_partial_read_back_names_what_it_found_and_admits_the_rest(self, mod, sketches):
        plate, _ = sketches
        plate.copy = _copier([], plate, made=[_boxed("L2", 9.0, 0.0)],
                             unreffable=[_boxed("C0", 9.0, 2.0)], proxies=True)
        out = payload(mod.handler(entities="line:0", dx=10))
        assert out["new_curves"] == ["line:2"] and out["new_curves_complete"] is False
        assert "2 curve(s) landed" in out["note"] and "only 1 could be identified" in out["note"]


class TestCurveRefIndexAlignment:

    """'<type>:<index>' is the ref the whole tool trades in - _common.resolve_entity_ref reads it
    back with coll.item(index) - so a curve the map cannot read must burn its index, not renumber
    the ones after it onto entities they do not name."""

    def test_an_unreadable_curve_leaves_the_later_refs_at_their_own_index(self, mod, sketches,
                                                                          monkeypatch):
        plate, _ = sketches
        tail = _boxed("L2", 9.0, 0.0)
        _lines(plate)._items.append(tail)
        plate.sketchCurves._items.append(tail)
        _stale_item_at(monkeypatch, _lines(plate), 1)
        refs = mod._curve_ref_by_token(plate)
        assert refs["L2"] == "line:2"                 # NOT line:1 - index 1 belongs to the bad curve
        assert refs == {"L0": "line:0", "L2": "line:2"}

    def test_the_copy_payload_names_the_new_curve_by_its_true_index(self, mod, sketches,
                                                                     monkeypatch):
        # the ref reaches the wire through copy's new_curves, where a renumbered index sends the
        # caller's next sketch_move at the wrong line
        plate, _ = sketches
        landed = _boxed("L2", 9.0, 0.0)
        plate.copy = _copier([], plate, made=[landed])
        _stale_item_at(monkeypatch, _lines(plate), 1)
        out = payload(mod.handler(entities="line:0", dx=10))
        assert out["new_curves"] == ["line:2"] and "new_curves_complete" not in out


class TestPostconditionWiring:

    def test_copy_verifies_the_target_sketch_before_the_source(self, mod):
        assert [p.describe() for p in mod.item.handler.__wrapped__.__assert_postconditions__] \
            == ["sketch_curves_changed(target_sketch|sketch_name)"]

    def test_both_copy_scopes_are_declared_from_the_one_shared_factory(self, mod):
        # sketch_copy carries TWO component inputs. Both come from _sketch_detail, so they accept
        # the same three forms and are described in the same words - and the second names the
        # reference it narrows, which is the only thing telling a caller them apart.
        sd = load_tool("_sketch_detail")
        props = mod.tool.input_schema["properties"]
        assert props["component"] == sd.COMPONENT_SCOPE[1]
        assert props["target_component"] == sd.component_scope("target_component",
                                                               narrows="target_sketch")[1]
        assert "'target_sketch'" in props["target_component"]["description"]

    def test_each_sketch_key_is_paired_with_the_scope_that_narrows_it(self, mod):
        # 'target_sketch' is narrowed by 'target_component' and 'sketch_name' by 'component'; the
        # pairing is POSITIONAL, so swapping the two would fingerprint the target sketch inside the
        # SOURCE's component and disclose a false unconfirmed on every copy across two components
        # that share a sketch name.
        move, = load_tool("sketch_move").item.handler.__wrapped__.__assert_postconditions__
        copy, = mod.item.handler.__wrapped__.__assert_postconditions__
        assert move.keys == ("sketch_name",) and move.scope_keys == ("component",)
        assert copy.keys == ("target_sketch", "sketch_name")
        assert copy.scope_keys == ("target_component", "component")

    def test_the_target_key_selects_which_sketch_is_fingerprinted(self, mod, sketches):
        plate, second = sketches
        kind = load_tool("_assert").SketchCurvesChanged(keys=("target_sketch", "sketch_name"))
        before = kind.capture({"sketch_name": "Plate", "target_sketch": "Other"})
        assert before["marks"] == {}                          # 'Other' is empty, 'Plate' is not
        second.sketchCurves._items.append(_boxed("N0", 0.0, 0.0))
        reason, evidence = kind.verify({"sketch_name": "Plate", "target_sketch": "Other"},
                                       {}, before)
        assert reason == "" and evidence == {"curve_count_after": 1}


class TestPostconditionKeysAreChecked:

    def test_a_key_the_handler_does_not_take_is_refused_at_wiring_time(self, mod):
        # an unknown key reads None out of kwargs and the kind silently falls back to the
        # most-recent sketch, verifying the wrong one while reporting success
        _assert = load_tool("_assert")
        with pytest.raises(ValueError) as excinfo:
            _assert.wrap(mod.handler,
                         [_assert.SketchCurvesChanged(keys=("target_sketch_name",))])
        assert "target_sketch_name" in str(excinfo.value)
        assert "handler does not take" in str(excinfo.value)

    def test_the_real_keys_wire_cleanly(self, mod):
        _assert = load_tool("_assert")
        wrapped = _assert.wrap(mod.handler,
                               [_assert.SketchCurvesChanged(keys=("target_sketch", "sketch_name"))])
        assert wrapped.__wrapped__ is mod.handler
