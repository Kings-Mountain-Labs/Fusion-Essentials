"""Unit tests for ``model_thread.py`` - add a Thread feature to existing cylindrical faces.

Pinned: the bore-vs-shaft derivation from each face's own outward normal (and its refusal when the
normal is unreadable or the set mixes sides), the partial-length contract (isFullLength false plus
the cm-scaled length/offset/location), the designation/handedness/modeled settings reaching the
input, and the honesty gates - a failed compute, a designation or side that reads back different
from the one requested, and a modeled thread that left the body's volume untouched.
"""

from conftest import (load_tool, make_design, install, MakeComp, payload, error_message,
                      assert_no_active_design, assert_unknown_units,
                      BRepBody, BRepFace, Cylinder, FakePoint, FakeVector3D)

mt = load_tool("model_thread")


class _VI:
    def __init__(self, value):
        self.value = value


def _cyl_face(body=None, radial=(1.0, 0.0, 0.0), axis_origin=(0.0, 0.0, 0.0)):
    """A cylindrical face sampled at (1,0,0), on a cylinder whose axis passes through
    `axis_origin`. radial=(-1,0,0) points at the axis (a bore); (1,0,0) points away (a shaft);
    None leaves the face with no evaluator to sample."""
    return BRepFace(Cylinder(FakeVector3D(0.0, 0.0, 1.0), FakePoint(*axis_origin)),
                    body=body if body is not None else BRepBody("Body1", volume=100.0),
                    point_on_face=FakePoint(1.0, 0.0, 0.0),
                    normal=FakeVector3D(*radial) if radial else None)


class FakeThreadInfo:
    def __init__(self, internal, ttype, desig, cls):
        self.isInternal = internal
        self.threadType = ttype
        self.threadDesignation = desig
        self.threadClass = cls


class FakeThreadInput:
    def __init__(self, faces, info):
        self.inputCylindricalFaces = faces
        self.threadInfo = info
        self.isModeled = False
        self.isRightHanded = True
        self.isFullLength = True
        self.threadLength = None
        self.threadOffset = None
        self.threadLocation = None


class FakeThreadFeature:
    """A live ThreadFeature reports the partial extent back: isFullLength plus threadLength /
    threadOffset as ModelParameters carrying centimetres, and threadLocation as the enum. The
    fake echoes the input unless a test overrides it to model an extent that did not take."""
    def __init__(self, inp, name="Thread1", health=0, info=None, full_length=None,
                 length_cm=None, offset_cm=None):
        self.name = name
        self.healthState = health
        self.errorOrWarningMessage = "the thread does not fit the cylinder"
        self.threadInfo = info if info is not None else inp.threadInfo
        self.isModeled = inp.isModeled
        self.isRightHanded = inp.isRightHanded
        self.isFullLength = inp.isFullLength if full_length is None else full_length
        got_len = inp.threadLength.value if inp.threadLength is not None else None
        got_off = inp.threadOffset.value if inp.threadOffset is not None else None
        self.threadLength = _VI(got_len if length_cm is None else length_cm)
        self.threadOffset = _VI(got_off if offset_cm is None else offset_cm)
        self.threadLocation = inp.threadLocation


class FakeThreadFeatures:
    """createInput/add plus the thread-table query the shared resolver walks. add() shifts each
    body's volume by `volume_delta` (split evenly) only when the input is modeled - a cosmetic
    thread changes no geometry."""
    def __init__(self, bodies=(), volume_delta=-2.0, return_feature=True, health=0,
                 feature_info=None, full_length=None, length_cm=None, offset_cm=None):
        self.added = 0
        self.full_length = full_length
        self.length_cm = length_cm
        self.offset_cm = offset_cm
        self.bodies = list(bodies)
        self.volume_delta = volume_delta
        self.return_feature = return_feature
        self.health = health
        self.feature_info = feature_info
        self.threadDataQuery = FakeThreadDataQuery()
        self.last_input = None
        self.created_info = []

    def createThreadInfo(self, internal, ttype, desig, cls):
        ti = FakeThreadInfo(internal, ttype, desig, cls)
        self.created_info.append(ti)
        return ti

    def createInput(self, faces, info):
        self.last_input = FakeThreadInput(faces, info)
        return self.last_input

    def __len__(self):
        # a live ThreadFeatures collection is falsy while empty
        return self.added

    def add(self, inp):
        self.added += 1
        if not self.return_feature:
            return None
        if inp.isModeled and self.bodies:
            per_body = self.volume_delta / len(self.bodies)
            for b in self.bodies:
                # a body whose volume is unreadable stays unreadable across the mutation
                if isinstance(b.volume, (int, float)):
                    b.volume += per_body
        return FakeThreadFeature(inp, health=self.health, info=self.feature_info,
                                 full_length=self.full_length, length_cm=self.length_cm,
                                 offset_cm=self.offset_cm)


class FakeThreadDataQuery:
    """The thread library the shared resolver walks, shaped as it reads live: the metric type is
    'ANSI Metric M Profile', and a size carries several designations and several classes, the
    external list starting 4g6g."""
    @property
    def allThreadTypes(self):
        return ("ANSI Metric M Profile", "ISO Metric profile")

    def allSizes(self, t):
        return ("10.0", "30.0")

    def allDesignations(self, t, size):
        # 'M10x1.25' sits in BOTH types, as 540 of the 1510 live designations do; the rest sit in
        # exactly one
        if t == "ISO Metric profile":
            return ("M10x1.25",) if size.startswith("10") else ()
        return ("M10x1.5", "M10x1.25") if size.startswith("10") else ("M30x3.5", "M30x3")

    def allClasses(self, internal, t, desig):
        return ("6H", "6G") if internal else ("4g6g", "6g")


def _wire(monkeypatch, feats, faces):
    import adsk.core
    comp = MakeComp(name="Comp")
    comp.features = type("F", (), {"threadFeatures": feats})()
    install(mt, make_design(comp=comp))
    monkeypatch.setattr(mt._FACES, "resolve", lambda raw: (faces, None))
    adsk.core.ValueInput.createByReal = staticmethod(lambda v: _VI(v))


def _shaft(body=None):
    return _cyl_face(body, radial=(1.0, 0.0, 0.0))


def _bore(body=None):
    return _cyl_face(body, radial=(-1.0, 0.0, 0.0))


class TestSideDerivation:
    def test_normal_pointing_away_from_the_axis_is_external(self, monkeypatch):
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_shaft()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5"))
        assert out["internal"] is False
        assert feats.created_info[0].isInternal is False
        assert feats.created_info[0].threadClass == "4g6g"

    def test_normal_pointing_at_the_axis_is_internal(self, monkeypatch):
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_bore()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5"))
        assert out["internal"] is True
        assert feats.created_info[0].isInternal is True
        assert feats.created_info[0].threadClass == "6H"

    def test_offset_axis_origin_still_classifies_by_the_radial_component(self, monkeypatch):
        face = _cyl_face(radial=(-1.0, 0.0, 0.0), axis_origin=(0.0, 0.0, 50.0))
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [face])
        assert payload(mt.handler(faces=["h"], designation="M10x1.5"))["internal"] is True

    def test_unreadable_normal_is_refused_not_guessed(self, monkeypatch):
        face = _cyl_face(radial=None)
        _wire(monkeypatch, FakeThreadFeatures(), [face])
        msg = error_message(mt.handler(faces=["h"], designation="M10x1.5"))
        assert "[0]" in msg and "normal" in msg

    def test_mixed_sides_are_refused_naming_the_bores(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft(), _bore(), _shaft()])
        msg = error_message(mt.handler(faces=["a", "b", "c"], designation="M10x1.5"))
        assert "[1]" in msg and "mix" in msg

    def test_several_faces_of_one_side_thread_together(self, monkeypatch):
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_shaft(), _shaft(), _shaft()])
        out = payload(mt.handler(faces=["a", "b", "c"], designation="M10x1.5"))
        assert out["faces"] == 3
        assert feats.last_input.inputCylindricalFaces.count == 3


class TestThreadSettings:
    def test_cosmetic_and_right_handed_by_default(self, monkeypatch):
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_shaft()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5"))
        assert feats.last_input.isModeled is False
        assert feats.last_input.isRightHanded is True
        assert feats.last_input.isFullLength is True
        assert out["modeled"] is False and out["right_handed"] is True

    def test_left_handed_flips_the_input_flag(self, monkeypatch):
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_shaft()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5", left_handed=True))
        assert feats.last_input.isRightHanded is False
        assert out["right_handed"] is False

    def test_length_clears_full_length_and_scales_to_cm(self, monkeypatch):
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_shaft()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5", length=12, offset=2))
        assert feats.last_input.isFullLength is False
        assert round(feats.last_input.threadLength.value, 6) == 1.2
        assert round(feats.last_input.threadOffset.value, 6) == 0.2
        assert out["length"] == 12 and out["offset"] == 2 and out["location"] == "high"

    def test_no_length_leaves_full_length_and_reports_no_length(self, monkeypatch):
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_shaft()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5"))
        assert feats.last_input.isFullLength is True
        assert feats.last_input.threadLength is None
        assert "length" not in out

    def test_inch_units_scale_the_length(self, monkeypatch):
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_shaft()])
        payload(mt.handler(faces=["h"], designation="M10x1.5", length=1, units="in"))
        assert round(feats.last_input.threadLength.value, 6) == 2.54

    def test_each_location_reaches_the_input_as_its_own_enum(self, monkeypatch):
        # live-verified: HighEndThreadLocation threads the end the cylinder axis points toward,
        # LowEndThreadLocation the opposite end
        import adsk.fusion
        wanted = {"high": adsk.fusion.ThreadLocations.HighEndThreadLocation,
                  "low": adsk.fusion.ThreadLocations.LowEndThreadLocation}
        assert wanted["high"] != wanted["low"]
        for key, want in wanted.items():
            feats = FakeThreadFeatures()
            _wire(monkeypatch, feats, [_shaft()])
            out = payload(mt.handler(faces=["h"], designation="M10x1.5", length=5, location=key))
            assert out["location"] == key
            assert feats.last_input.threadLocation == want


class TestGuards:
    def test_no_active_design(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        assert_no_active_design(mt, mt.handler, faces=["h"], designation="M10x1.5")

    def test_bad_units(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        assert_unknown_units(mt.handler, faces=["h"], designation="M10x1.5")

    def test_missing_designation(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        msg = error_message(mt.handler(faces=["h"], designation=""))
        assert "designation" in msg

    def test_unknown_designation_lists_the_call_out_shape(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        msg = error_message(mt.handler(faces=["h"], designation="M99x9"))
        assert "M99x9" in msg and "thread library" in msg

    def test_offset_without_length_is_refused(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        msg = error_message(mt.handler(faces=["h"], designation="M10x1.5", offset=2))
        assert "offset" in msg and "length" in msg

    def test_location_without_length_is_refused(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        msg = error_message(mt.handler(faces=["h"], designation="M10x1.5", location="low"))
        assert "location" in msg and "length" in msg

    def test_zero_length_is_refused(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        msg = error_message(mt.handler(faces=["h"], designation="M10x1.5", length=0))
        assert "length" in msg and "non-zero" in msg

    def test_negative_length_is_refused(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        msg = error_message(mt.handler(faces=["h"], designation="M10x1.5", length=-5))
        assert "length" in msg and "positive" in msg

    def test_unknown_location_is_refused(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        msg = error_message(mt.handler(faces=["h"], designation="M10x1.5", length=5,
                                       location="middle"))
        assert "location" in msg

    def test_face_resolution_error_propagates(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        monkeypatch.setattr(mt._FACES, "resolve",
                            lambda raw: (None, "'faces' must be a CYLINDRICAL face, but the handle "
                                               "points at a BRepEdge."))
        msg = error_message(mt.handler(faces=["flat"], designation="M10x1.5"))
        assert "CYLINDRICAL" in msg


class TestHonesty:
    def test_health_error_reported_not_false_ok(self, monkeypatch):
        feats = FakeThreadFeatures(health=2)
        _wire(monkeypatch, feats, [_shaft()])
        res = mt.handler(faces=["h"], designation="M10x1.5")
        assert res["isError"] is True
        assert "failed to compute" in res["message"] and "does not fit" in res["message"]

    def test_no_feature_returned_is_error(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(return_feature=False), [_shaft()])
        res = mt.handler(faces=["h"], designation="M10x1.5")
        assert res["isError"] is True and "no feature" in res["message"]

    def test_designation_read_back_mismatch_is_error(self, monkeypatch):
        other = FakeThreadInfo(False, "ANSI Metric M Profile", "M8x1.25", "6g")
        _wire(monkeypatch, FakeThreadFeatures(feature_info=other), [_shaft()])
        res = mt.handler(faces=["h"], designation="M10x1.5")
        assert res["isError"] is True and "M8x1.25" in res["message"]

    def test_side_read_back_mismatch_is_error(self, monkeypatch):
        other = FakeThreadInfo(True, "ANSI Metric M Profile", "M10x1.5", "6H")
        _wire(monkeypatch, FakeThreadFeatures(feature_info=other), [_shaft()])
        res = mt.handler(faces=["h"], designation="M10x1.5")
        assert res["isError"] is True and "internal" in res["message"]

    def test_add_raising_surfaces_as_error(self, monkeypatch):
        feats = FakeThreadFeatures()
        feats.add = lambda inp: (_ for _ in ()).throw(RuntimeError("thread pitch too coarse"))
        _wire(monkeypatch, feats, [_shaft()])
        res = mt.handler(faces=["h"], designation="M10x1.5")
        assert res["isError"] is True and "thread pitch too coarse" in res["message"]

    def test_modeled_thread_reports_the_volume_it_cut(self, monkeypatch):
        body = BRepBody("Post", volume=100.0)
        feats = FakeThreadFeatures([body], volume_delta=-2.5)
        _wire(monkeypatch, feats, [_shaft(body)])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5", modeled=True))
        assert out["modeled"] is True
        assert out["volume_delta_cm3"] == -2.5

    def test_modeled_thread_that_cut_nothing_is_error(self, monkeypatch):
        body = BRepBody("Post", volume=100.0)
        feats = FakeThreadFeatures([body], volume_delta=0.0)
        _wire(monkeypatch, feats, [_shaft(body)])
        res = mt.handler(faces=["h"], designation="M10x1.5", modeled=True)
        assert res["isError"] is True and "unchanged" in res["message"]

    def test_cosmetic_thread_is_not_gated_on_volume(self, monkeypatch):
        body = BRepBody("Post", volume=100.0)
        feats = FakeThreadFeatures([body], volume_delta=0.0)
        _wire(monkeypatch, feats, [_shaft(body)])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5"))
        assert out["threaded"] is True
        assert "volume_delta_cm3" not in out


class TestOutputContract:
    def test_feature_output_is_minted(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5"))
        for o in mt.RETURNS:
            assert o.assert_present(out) == "", o.key


class TestEmptyCollectionIsNotAbsence:
    def test_the_first_thread_in_a_component_still_resolves(self, monkeypatch):
        feats = FakeThreadFeatures()
        assert not feats, "the fake must read falsy while empty, as the live collection does"
        _wire(monkeypatch, feats, [_shaft()])
        assert payload(mt.handler(faces=["h"], designation="M10x1.5"))["faces"] == 1


class TestMisSizedModeledThread:
    def test_a_modeled_thread_that_grew_the_body_is_error(self, monkeypatch):
        # a designation too large for a shaft builds the form outside the cylinder and ADDS
        # material; every designation that fits removes it
        body = BRepBody("Post", volume=18.85)
        feats = FakeThreadFeatures([body], volume_delta=17.3)
        _wire(monkeypatch, feats, [_shaft(body)])
        res = mt.handler(faces=["h"], designation="M30x3.5", modeled=True)
        assert res["isError"] is True
        assert "GREW" in res["message"] and "17.3" in res["message"]

    def test_a_fitting_modeled_thread_still_passes(self, monkeypatch):
        body = BRepBody("Post", volume=18.85)
        feats = FakeThreadFeatures([body], volume_delta=-2.99)
        _wire(monkeypatch, feats, [_shaft(body)])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5", modeled=True))
        assert out["volume_delta_cm3"] == -2.99


class TestPartialExtentIsReadBack:
    def test_a_partial_thread_that_stayed_full_length_is_error(self, monkeypatch):
        feats = FakeThreadFeatures(full_length=True)
        _wire(monkeypatch, feats, [_shaft()])
        res = mt.handler(faces=["h"], designation="M10x1.5", length=12)
        assert res["isError"] is True
        assert "full length" in res["message"] and "Thread1" in res["message"]

    def test_a_length_that_read_back_different_is_error(self, monkeypatch):
        feats = FakeThreadFeatures(length_cm=0.9)
        _wire(monkeypatch, feats, [_shaft()])
        res = mt.handler(faces=["h"], designation="M10x1.5", length=12)
        assert res["isError"] is True
        assert "length reads back" in res["message"] and "Thread1" in res["message"]

    def test_the_reported_length_comes_from_the_feature(self, monkeypatch):
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_shaft()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5", length=12, offset=2))
        assert out["length"] == 12 and out["offset"] == 2

    def test_a_modeled_thread_with_no_readable_volume_is_error(self, monkeypatch):
        body = BRepBody("Post", volume=None)
        feats = FakeThreadFeatures([body], volume_delta=-2.0)
        _wire(monkeypatch, feats, [_shaft(body)])
        res = mt.handler(faces=["h"], designation="M10x1.5", modeled=True)
        assert res["isError"] is True and "no proof" in res["message"]

    def test_a_modeled_thread_on_a_face_with_no_owning_body_is_error(self, monkeypatch):
        face = _cyl_face(radial=(1.0, 0.0, 0.0))
        face.body = None
        _wire(monkeypatch, FakeThreadFeatures(), [face])
        res = mt.handler(faces=["h"], designation="M10x1.5", modeled=True)
        assert res["isError"] is True and "find_geometry" in res["message"]


class TestInternalFlagIsReadOffTheCreatedFeature:
    """The bore/shaft classification's real gate is the PLATFORM: measured, add() validates the
    ThreadInfo's internal flag against the face and raises "input face's externality is different
    from what's in the ThreadInfo" on a mismatch, so a wrong classification is a loud failure and
    never a wrong thread. The read-back below is a cheap second witness over the same fact - these
    pin that the classification reaches the ThreadInfo, and that the witness still fires."""

    def test_a_cosmetic_bore_thread_reports_the_internal_flag_the_feature_carries(self, monkeypatch):
        # the default is COSMETIC (no geometry changes), so this flag is the only evidence the
        # bore classification reached the feature at all
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_bore()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5"))
        assert out["internal"] is True
        assert out["modeled"] is False
        assert feats.created_info[0].isInternal is True

    def test_a_feature_reading_back_external_on_a_bore_is_an_error_not_a_silent_pass(
            self, monkeypatch):
        # The second witness, forced to fire: a feature answering EXTERNAL on bore faces. Live the
        # platform refuses that pairing at add() before any feature exists, so this state is not
        # reachable there - the check costs one read and must stay an error if it ever is.
        external = FakeThreadInfo(False, "ANSI Metric M Profile", "M10x1.5", "6g")
        _wire(monkeypatch, FakeThreadFeatures(feature_info=external), [_bore()])
        res = mt.handler(faces=["h"], designation="M10x1.5")
        assert res["isError"] is True
        assert "external" in res["message"] and "bores" in res["message"]


class TestPartialExtentFailsClosed:
    def test_an_unreadable_extent_is_an_error_not_an_assumed_success(self, monkeypatch):
        # isFullLength unreadable: the partial extent cannot be shown to have taken, so the call
        # fails CLOSED and names the feature to remove - never an ok() carrying the requested length
        feats = FakeThreadFeatures()
        feats.full_length = "unreadable"          # not a bool - what a proxy that stops answering gives
        _wire(monkeypatch, feats, [_shaft()])
        res = mt.handler(faces=["h"], designation="M10x1.5", length=12)
        assert res["isError"] is True
        assert "no proof it took" in res["message"] and "Thread1" in res["message"]

    def test_an_unreadable_length_parameter_is_an_error(self, monkeypatch):
        feats = FakeThreadFeatures(length_cm="unreadable")
        _wire(monkeypatch, feats, [_shaft()])
        res = mt.handler(faces=["h"], designation="M10x1.5", length=12)
        assert res["isError"] is True and "no proof it took" in res["message"]

    def test_a_thread_that_landed_at_the_wrong_end_is_an_error(self, monkeypatch):
        # threadLocation reads back the OTHER end: length and offset both check out, so this gate is
        # the only thing between a thread on the wrong end of the cylinder and a clean ok()
        import adsk.fusion
        feats = FakeThreadFeatures()
        real_add = feats.add

        def _add(inp):
            feature = real_add(inp)
            feature.threadLocation = adsk.fusion.ThreadLocations.LowEndThreadLocation
            return feature

        feats.add = _add
        _wire(monkeypatch, feats, [_shaft()])
        res = mt.handler(faces=["h"], designation="M10x1.5", length=12, location="high")
        assert res["isError"] is True
        assert "wrong end" in res["message"] and "high" in res["message"]

    def test_the_requested_end_that_took_is_reported(self, monkeypatch):
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_shaft()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5", length=12, location="low"))
        assert out["location"] == "low"


class TestAmbiguousDesignation:
    def test_a_designation_carried_by_several_types_discloses_the_others(self, monkeypatch):
        # the thread namespace is NOT unique, and the measured profiles that share a designation
        # build an identical ThreadInfo - so the pick stands and the alternatives are reported
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.25"))
        assert out["thread_type"] == "ANSI Metric M Profile"
        assert out["thread_type_alternatives"] == ["ANSI Metric M Profile", "ISO Metric profile"]

    def test_an_unambiguous_designation_reports_no_alternatives(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5"))
        assert out.get("thread_type_alternatives") is None

    def test_thread_type_picks_the_standard(self, monkeypatch):
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_shaft()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.25",
                                 thread_type="ISO Metric profile"))
        assert out["designation"] == "M10x1.25"
        assert feats.created_info[0].threadType == "ISO Metric profile"

    def test_a_thread_type_that_lacks_the_designation_is_refused(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        msg = error_message(mt.handler(faces=["h"], designation="M30x3.5",
                                       thread_type="ISO Metric profile"))
        assert "does not carry" in msg and "ANSI Metric M Profile" in msg

    def test_thread_class_picks_the_fit(self, monkeypatch):
        # 4g6g and 6g are different external FITS of the same thread - not interchangeable the way
        # the standards sharing a designation are, so the caller can name one
        feats = FakeThreadFeatures()
        _wire(monkeypatch, feats, [_shaft()])
        out = payload(mt.handler(faces=["h"], designation="M10x1.5", thread_class="6g"))
        assert out["thread_class"] == "6g"
        assert feats.created_info[0].threadClass == "6g"

    def test_an_unoffered_thread_class_is_refused(self, monkeypatch):
        _wire(monkeypatch, FakeThreadFeatures(), [_shaft()])
        msg = error_message(mt.handler(faces=["h"], designation="M10x1.5", thread_class="9z"))
        assert "not offered" in msg and "4g6g" in msg
