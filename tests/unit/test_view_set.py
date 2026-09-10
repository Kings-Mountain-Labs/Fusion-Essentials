"""Unit tests for ``view_set.py`` — the agent's view verbs.

Camera math (eye/target/up per orientation) is a live-viewport side-effect best
left to a real session, but the surrounding LOGIC is pure and worth pinning:
action validation, occurrence resolution via the shared typed kinds (exact fullPathName/name beats
substring; an ambiguous substring is REFUSED, never grabbed first-match; isolate needs exactly one
resolved match and stays occurrence-only), the visibility verbs (isolate flag, hide bulb,
clear_isolation, show lighting the whole ancestor chain, body-level hide/show with a per-body
bulb read-back), style validation, the
named-view library (save overwrites same name, apply errors with a list, list
reports built-ins), and the snapshot -> mutate -> restore round-trip that puts
bulbs/isolation/style back. Fakes expose the real attributes the tool sets so we
can assert on them.
"""

import json
import types

import pytest

from conftest import (load_tool, make_source_document, make_bbox, BRepBody, Camera,
                      FakeApplication, FakeDataFile, FakeFusionDocument, FakeOccurrence, FakePoint,
                      FakeVector3D, MakeComp, MakeDesign, Viewport)

iv = load_tool("view_set")


# ── fakes ───────────────────────────────────────────────────────────────────
#
# Points and bounding boxes come from conftest's shared fakes (FakePoint / make_bbox); only the
# occurrence/viewport/named-view graph below is local.

def _occ_args(name, full_path=None, bbox=None, parent=None, bulb=True, isolated=False):
    """The shared-fake arguments one occurrence of this rig is built from. A real Occurrence always
    answers `component`; one whose read RAISES is an unresolved external reference, which the shared
    census keeps out of this walk."""
    return dict(path=full_path or name, component=MakeComp(name=name.split(":")[0]),
                bounding_box=bbox, assembly_context=parent, light_bulb_on=bulb, isolated=isolated)


def FakeOcc(*args, **kwargs):
    """One occurrence of this rig on the shared occurrence fake."""
    return FakeOccurrence(**_occ_args(*args, **kwargs))


def FakeRoot(occurrences, bodies=(), bbox=None):
    """The root component: the design-wide occurrence walk, the root-level bodies a body hide/show
    targets, and the WHOLE-DESIGN box a fit frames - the denominator of the framing ratio."""
    root = MakeComp(name="Root", all_occurrences=list(occurrences), bodies=list(bodies))
    root.boundingBox = bbox if bbox is not None else make_bbox((0, 0, 0), (100, 100, 100))
    return root


class FakeNamedView:
    def __init__(self, name, built_in=False):
        self.name = name
        self.isBuiltIn = built_in
        self._deleted = False
        self.applied = False
        self._owner = None          # set when added to a FakeNamedViews

    def deleteMe(self):
        self._deleted = True
        # Mirror Fusion: deleting a named view removes it from its collection.
        if self._owner is not None and self in self._owner._views:
            self._owner._views.remove(self)
        return True

    def apply(self):
        self.applied = True


class FakeNamedViews:
    def __init__(self, views=()):
        self._views = list(views)
        for v in self._views:
            v._owner = self

    @property
    def count(self):
        return len(self._views)

    def item(self, i):
        return self._views[i]

    def itemByName(self, name):
        for v in self._views:
            if v.name == name:
                return v
        raise RuntimeError("not found")   # Fusion throws when absent

    def add(self, _camera, name):
        nv = FakeNamedView(name)
        nv._owner = self
        self._views.append(nv)
        return nv


def FakeDesign(occurrences, named_views=None, bodies=(), bbox=None, all_components=None):
    """The design behind a view write, on the shared design fake plus the namedViews collection
    (NamedViews has no measured shape, so that half stays local). `all_components` replaces the
    root-only census with the components a design-wide folder walk has to reach."""
    design = MakeDesign(comp=FakeRoot(occurrences, bodies, bbox), all_components=all_components)
    design.namedViews = named_views if named_views is not None else FakeNamedViews()
    return design


def _camera():
    """The camera the rig starts from: orthographic, 100 cm of LINEAR extents, and the up vector a
    real camera always carries (framing reads it to build the screen axes)."""
    return Camera(up=(0, 0, 1))


def _viewport():
    """The rig's viewport at the shared fake's 1516x757 frame."""
    return Viewport(camera=_camera())


class _StubbornViewport(Viewport):
    """A viewport that does not fully honour a camera assignment: every camera READ hands back a
    fresh camera carrying the projection (and optionally the perspective angle) this viewport
    insists on. Models the two states the orient read-back gates on - a projection set that never
    took, and a perspective angle the camera settles on somewhere else."""

    def __init__(self, camera_type, perspective_angle=None, angle_readable=True,
                 type_readable=True):
        super().__init__(camera=_camera())
        self._type = camera_type
        self._angle = perspective_angle
        self._angle_readable = angle_readable
        self._type_readable = type_readable

    @property
    def camera(self):
        cam = _camera()
        cam.cameraType = self._type
        if self._angle is not None:
            cam.perspectiveAngle = self._angle
        if not self._angle_readable:
            del cam.perspectiveAngle      # the property is unreadable, which is not an angle of 0
        if not self._type_readable:
            del cam.cameraType            # unreadable, which is not a projection that failed
        return cam

    @camera.setter
    def camera(self, value):
        self._assigned.append(value)


def _doc(name, data_file_id=None):
    """The active document. Never saved -> dataFile reads None (measured); only a saved document
    hands back one."""
    return FakeFusionDocument(
        name=name,
        data_file=FakeDataFile(file_id=data_file_id) if data_file_id is not None else None)


def _app(design, doc_name="Doc", doc_id=None):
    """The session the view verbs read through: the design as the active product, one open
    document, and the rig's viewport."""
    return FakeApplication(active_product=design, active_document=_doc(doc_name, doc_id),
                           active_viewport=_viewport())


class _DocWrapper:
    """One read of a document handle. Models the CLOSED-document behaviour as measured: after the
    document closes, a wrapper handed out earlier compares UNEQUAL to every live document (the
    comparison answers False, it does NOT raise), isValid reads False, and `.name` raises
    "An API Object refers to a deleted Object"."""

    def __init__(self, opened):
        self._opened = opened
        # A never-saved document answers dataFile None (measured - it does not raise); only a
        # saved one hands back a DataFile. None is the branch _doc_key's unsaved key exists for.
        self.dataFile = (FakeDataFile(file_id=opened.data_file_id)
                         if opened.data_file_id is not None else None)

    @property
    def isValid(self):
        return self._opened.is_open

    @property
    def name(self):
        if not self._opened.is_open:
            raise RuntimeError("4 : An API Object refers to a deleted Object")
        return self._opened.name

    def __eq__(self, other):
        if not self._opened.is_open:
            return False
        return isinstance(other, _DocWrapper) and other._opened is self._opened

    # Not hashable, like the wrapper it stands in for: anything keying a dict/set on a document
    # instead of comparing handles has to fail loudly here rather than silently mis-key.
    __hash__ = None


class _UnreadableComparisonWrapper(_DocWrapper):
    """A wrapper whose `==` will not read - an ARBITRARY unreadable comparison, claiming nothing
    about any particular platform state (a closed document's wrapper compares False, it does not
    raise). It exists to drive the safe() default: a comparison that cannot be read is not a match."""

    def __eq__(self, other):
        raise RuntimeError("the comparison could not be read")

    __hash__ = None


class OpenDocument:
    """One open document, handing back a FRESH wrapper on every read - what the real API does
    (_write_guard._open_documents carries the live measurement: `is` reads False for the one
    active document across two reads while `==` reads True). Two wrappers of THIS document
    compare equal; wrappers of a different OpenDocument never do."""

    def __init__(self, name, data_file_id=None, wrapper_class=_DocWrapper):
        self.name = name
        self.data_file_id = data_file_id
        self.wrapper_class = wrapper_class
        self.is_open = True

    def wrapper(self):
        return self.wrapper_class(self)

    def close(self):
        """Close the document. Wrappers already handed out (the registry holds one) stay reachable
        as Python objects and go invalid, which is what a closed Fusion document leaves behind."""
        self.is_open = False


class RewrappingApp(FakeApplication):
    """The session with the wrapper churn: every app.activeDocument read mints a new wrapper."""

    def __init__(self, design, opened):
        self._opened = opened
        FakeApplication.__init__(self, active_product=design, active_viewport=_viewport())

    @property
    def activeDocument(self):
        return self._opened.wrapper()

    @activeDocument.setter
    def activeDocument(self, value):
        # The base constructor assigns it; this document answers from `opened` instead.
        pass


def _body(name, bulb=True, hidden_by_ancestor=False):
    """A conftest BRepBody: own settable bulb; isVisible = bulb AND not hidden_by_ancestor."""
    return BRepBody(name=name, light_bulb=bulb, hidden_by_ancestor=hidden_by_ancestor)


def _install(monkeypatch, occurrences=(), named_views=None, doc_name="Doc", doc_id=None, bodies=(),
             design_bbox=None, all_components=None):
    design = FakeDesign(list(occurrences), named_views, bodies, design_bbox, all_components)
    app = _app(design, doc_name, doc_id=doc_id)
    monkeypatch.setattr(iv, "app", app)
    monkeypatch.setattr(iv._common, "app", app)
    # The snapshot key is minted by _write_guard.document_key, which reads the active document
    # through ITS OWN module-level app - patching only view_set's leaves the key read looking at the
    # real (absent) Fusion app.
    monkeypatch.setattr(iv._write_guard, "app", app)
    import adsk.fusion
    monkeypatch.setattr(adsk.fusion.Design, "cast", lambda x: x if isinstance(x, MakeDesign) else None)
    # adsk.core.VisualStyles.<Name> must resolve to an int for _do_style.
    import adsk.core
    vs = adsk.core.VisualStyles
    for i, attr in enumerate(["ShadedVisualStyle", "WireframeVisualStyle",
                              "ShadedWithVisibleEdgesOnlyVisualStyle",
                              "ShadedWithHiddenEdgesVisualStyle",
                              "WireframeWithHiddenEdgesVisualStyle",
                              "WireframeWithVisibleEdgesOnlyVisualStyle"]):
        monkeypatch.setattr(vs, attr, i + 1, raising=False)
    # Point3D/Vector3D create -> simple carriers (orient math touches these).
    monkeypatch.setattr(adsk.core.Point3D, "create", staticmethod(lambda x, y, z: FakePoint(x, y, z)))
    monkeypatch.setattr(adsk.core.Vector3D, "create", staticmethod(lambda x, y, z: FakeVector3D(x, y, z)))
    # framing reads the frame through viewToModelSpace, which takes a Point2D
    monkeypatch.setattr(adsk.core.Point2D, "create",
                        staticmethod(lambda x, y: types.SimpleNamespace(x=x, y=y)))
    return design


def _install_open_document(monkeypatch, occurrences, opened):
    """_install, then swap in an app whose activeDocument re-wraps on every read (OpenDocument).
    Call it again with the SAME OpenDocument to model switching back to that document."""
    design = _install(monkeypatch, occurrences)
    app = RewrappingApp(design, opened)
    monkeypatch.setattr(iv, "app", app)
    monkeypatch.setattr(iv._common, "app", app)
    monkeypatch.setattr(iv._write_guard, "app", app)
    return design


def _clear_snapshot_state():
    """BOTH halves of the module-level snapshot state: the store here and the per-instance key
    registry in _write_guard (shared with every other consumer of document_key). The registry
    outlives one call by design (that is what lets a document find its own snapshot on the next
    call), so a test that asserts on keys has to start it from empty."""
    iv._SNAPSHOTS.clear()
    iv._write_guard._UNSAVED_DOC_KEYS.clear()


@pytest.fixture(autouse=True)
def _isolated_snapshot_state():
    """Both stores are module-level SESSION state that no test owns on entry. Cleared around every
    test in this file, not just the snapshot ones: a fake document left in the key registry is
    still scanned by the next test's _doc_key, in any class."""
    _clear_snapshot_state()
    yield
    _clear_snapshot_state()


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_unknown_action(self, monkeypatch):
        _install(monkeypatch)
        res = iv.handler(action="zoomzoom")
        assert res["isError"] is True and "Unknown action" in res["message"]

    def test_no_design(self, monkeypatch):
        app = _app(None)
        monkeypatch.setattr(iv, "app", app)
        monkeypatch.setattr(iv._common, "app", app)
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion.Design, "cast", lambda x: None)
        res = iv.handler(action="orient", orientation="front")
        assert res["isError"] is True and "No active design" in res["message"]


# ── occurrence resolution via visibility verbs ──────────────────────────────

class TestVisibility:
    def test_hide_turns_bulb_off(self, monkeypatch):
        occ = FakeOcc("Bracket", bulb=True)
        _install(monkeypatch, [occ])
        _payload(iv.handler(action="hide", target="Bracket"))
        assert occ.isLightBulbOn is False

    def test_isolate_sets_flag(self, monkeypatch):
        occ = FakeOcc("Bracket")
        _install(monkeypatch, [occ])
        _payload(iv.handler(action="isolate", target="Bracket"))
        assert occ.isIsolated is True

    def test_isolate_requires_single_match(self, monkeypatch):
        occs = [FakeOcc(f"Leaf:{i}", full_path=f"Rig+Leaf:{i}", bulb=i % 2 == 0)
                for i in range(1, 13)]
        _install(monkeypatch, occs)
        before = [(occ.isLightBulbOn, occ.isIsolated) for occ in occs]
        res = iv.handler(action="isolate", target=[occ.fullPathName for occ in occs])
        assert res["isError"] is True
        assert "needs exactly one" in res["message"] and "12 targets resolved" in res["message"]
        assert "fuller" not in res["message"]
        assert "hide unwanted occurrences" in res["message"] and "show selected" in res["message"]
        assert [(occ.isLightBulbOn, occ.isIsolated) for occ in occs] == before

    def test_ambiguous_substring_refused_not_first_match(self, monkeypatch):
        # a bare substring matching SEVERAL occurrences must ERROR (naming the ambiguity), never
        # silently act on the first one - the wrong-instance risk OccurrenceRefList exists to refuse.
        a = FakeOcc("Bolt:1")
        b = FakeOcc("Bolt:2")
        _install(monkeypatch, [a, b])
        res = iv.handler(action="hide", target="Bolt")
        assert res["isError"] is True
        assert "ambiguous" in res["message"].lower()
        assert a.isLightBulbOn is True and b.isLightBulbOn is True   # neither touched

    def test_exact_name_beats_substring(self, monkeypatch):
        exact = FakeOcc("Bolt")
        longer = FakeOcc("Bolt Flange")
        _install(monkeypatch, [longer, exact])
        # 'Bolt' exact-matches one, substring-matches both; exact wins -> single isolate ok
        out = _payload(iv.handler(action="isolate", target="Bolt"))
        assert out["affected"] == ["Bolt"]
        assert exact.isIsolated is True
        assert longer.isIsolated is False

    def test_show_lights_ancestor_chain(self, monkeypatch):
        parent = FakeOcc("Assembly", bulb=False)
        child = FakeOcc("Screw", full_path="Assembly+Screw", parent=parent, bulb=False)
        _install(monkeypatch, [parent, child])
        out = _payload(iv.handler(action="show", target="Screw"))
        assert child.isLightBulbOn is True
        assert parent.isLightBulbOn is True          # ancestor lit too
        assert "Assembly" in out["ancestors_also_shown"]

    def test_clear_isolation_resets_all(self, monkeypatch):
        a = FakeOcc("A", isolated=True)
        b = FakeOcc("B", isolated=True)
        _install(monkeypatch, [a, b])
        out = _payload(iv.handler(action="clear_isolation"))
        assert out["cleared_count"] == 2
        assert a.isIsolated is False and b.isIsolated is False

    def test_unmatched_target_errors(self, monkeypatch):
        # hide resolves through the occurrence-or-body kind, so a full miss reports the whole contract
        # (not just occurrences).
        _install(monkeypatch, [FakeOcc("A")])
        res = iv.handler(action="hide", target="Ghost")
        assert res["isError"] is True and "did not resolve" in res["message"].lower()

    def test_missing_target_errors(self, monkeypatch):
        _install(monkeypatch, [FakeOcc("A")])
        res = iv.handler(action="hide")
        assert res["isError"] is True and "Provide 'target'" in res["message"]


class _StubbornOcc(FakeOccurrence):
    """An occurrence that ACCEPTS a bulb/isolation write and keeps its old value - the platform
    swallowing a visibility change, which no read-back would catch."""

    def __init__(self, name, full_path=None, swallow=("isLightBulbOn", "isIsolated"), **kw):
        super().__init__(**_occ_args(name, full_path=full_path, **kw))
        object.__setattr__(self, "_swallow", tuple(swallow))

    def __setattr__(self, key, value):
        if key in getattr(self, "_swallow", ()):
            return
        object.__setattr__(self, key, value)


class _BlindAfterClear(FakeOccurrence):
    """An occurrence that ACCEPTS the isolation clear and then DECLINES the read-back - a flag that
    answers nothing, which is neither a confirmed clear nor an observed stuck lock."""

    def __init__(self, name, full_path=None, **kw):
        super().__init__(**_occ_args(name, full_path=full_path, isolated=True, **kw))

    @FakeOccurrence.isIsolated.setter
    def isIsolated(self, value):
        FakeOccurrence.isIsolated.fset(self, value)
        self._raises_on["isIsolated"] = "2 : InternalValidationError : isIsolated"


class TestVisibilityReadBack:
    """Every occurrence write is read BACK: a swallowed hide/isolate/show is an error, and a
    mid-list failure names the targets it already changed."""

    def test_a_hide_the_platform_swallows_is_an_error(self, monkeypatch):
        occ = _StubbornOcc("Bracket", bulb=True)
        _install(monkeypatch, [occ])
        res = iv.handler(action="hide", target="Bracket")
        assert res["isError"] is True
        assert "isLightBulbOn reads back True" in res["message"]
        assert "did not take" in res["message"]

    def test_an_isolate_the_platform_swallows_is_an_error(self, monkeypatch):
        occ = _StubbornOcc("Bracket", isolated=False)
        _install(monkeypatch, [occ])
        res = iv.handler(action="isolate", target="Bracket")
        assert res["isError"] is True
        assert "isIsolated reads back False" in res["message"]

    def test_a_show_whose_ancestor_bulb_will_not_light_is_an_error(self, monkeypatch):
        # The leaf lights but the parent stays dark, so the target is STILL invisible - reporting
        # 'Visibility changed' there is the false ok.
        parent = _StubbornOcc("Assembly", bulb=False)
        child = FakeOcc("Screw", full_path="Assembly+Screw", parent=parent, bulb=False)
        _install(monkeypatch, [parent, child])
        res = iv.handler(action="show", target="Screw")
        assert res["isError"] is True
        assert "Assembly" in res["message"] and "stays hidden" in res["message"]

    def test_a_mid_list_failure_names_what_was_already_hidden(self, monkeypatch):
        # Without this the caller gets a bare error and cannot tell that 'A' is now hidden.
        a = FakeOcc("A", full_path="A", bulb=True)
        b = _StubbornOcc("B", full_path="B", bulb=True)
        _install(monkeypatch, [a, b])
        res = iv.handler(action="hide", target=["A", "B"])
        assert res["isError"] is True
        assert "Already changed before this failure: A." in res["message"]
        assert a.isLightBulbOn is False              # and it really is hidden

    def test_a_first_target_failure_names_nothing_extra(self, monkeypatch):
        b = _StubbornOcc("B", full_path="B", bulb=True)
        _install(monkeypatch, [b])
        res = iv.handler(action="hide", target=["B"])
        assert res["isError"] is True and "Already changed" not in res["message"]

    def test_a_clear_isolation_the_platform_swallows_is_disclosed(self, monkeypatch):
        stuck = _StubbornOcc("A", full_path="A", isolated=True, swallow=("isIsolated",))
        good = FakeOcc("B", full_path="B", isolated=True)
        _install(monkeypatch, [stuck, good])
        out = _payload(iv.handler(action="clear_isolation"))
        assert out["cleared_count"] == 1             # only the one that actually cleared
        assert out["stuck"] == ["A"]
        assert "still read isIsolated true" in out["note"]

    def test_a_fully_clean_clear_isolation_claims_nothing_stuck(self, monkeypatch):
        _install(monkeypatch, [FakeOcc("A", full_path="A", isolated=True)])
        out = _payload(iv.handler(action="clear_isolation"))
        assert out["cleared_count"] == 1 and "stuck" not in out

    def test_a_clear_whose_read_back_declines_is_published_as_not_confirmed(self, monkeypatch):
        # An unreadable read-back is the shape a `is not True` gate would count as cleared, which
        # claims an isolation was lifted on the one occurrence nothing was observed about.
        _install(monkeypatch, [_BlindAfterClear("A", full_path="A")])
        out = _payload(iv.handler(action="clear_isolation"))
        assert out["cleared_count"] == 0
        assert out["not_confirmed"] == ["A"]
        assert "stuck" not in out
        assert "did not read back" in out["note"]


class TestBodyVisibility:
    """hide/show reach single BODIES (root-level bodies / one body of a multi-body component) -
    the granularity occurrence bulbs cannot address. The bulb write is READ BACK per body."""

    def test_hide_one_root_body_leaves_the_other_lit(self, monkeypatch):
        b1, b2 = _body("Body1"), _body("Body2")
        _install(monkeypatch, bodies=[b1, b2])
        out = _payload(iv.handler(action="hide", target=["Body1"]))
        assert b1.isLightBulbOn is False
        assert b2.isLightBulbOn is True          # the sibling body is untouched
        assert out["affected"] == ["Body1"]
        assert out["bodies"] == [{"body": "Body1", "light_bulb_on": False, "visible": False}]

    def test_show_body_turns_bulb_on_and_reads_back(self, monkeypatch):
        b = _body("Body1", bulb=False)
        _install(monkeypatch, bodies=[b])
        out = _payload(iv.handler(action="show", target=["Body1"]))
        assert b.isLightBulbOn is True
        assert out["bodies"][0]["light_bulb_on"] is True

    def test_body_bulb_write_that_does_not_take_errors(self, monkeypatch):
        # Honesty gate: a bulb set the platform swallows must be an error, never a false ok.
        class _StubbornBody:
            name = "Body1"
            entityToken = "Body1"
            isVisible = True
            @property
            def isLightBulbOn(self):
                return True
            @isLightBulbOn.setter
            def isLightBulbOn(self, v):
                pass                             # silently ignores the write
        _install(monkeypatch, bodies=[_StubbornBody()])
        res = iv.handler(action="hide", target=["Body1"])
        assert res["isError"] is True and "reads back" in res["message"]

    def test_shown_but_still_invisible_body_is_named_in_the_note(self, monkeypatch):
        # bulb ON but isVisible False (an ancestor occurrence is dark) - report it, don't claim done.
        b = _body("Body1", bulb=False, hidden_by_ancestor=True)
        _install(monkeypatch, bodies=[b])
        out = _payload(iv.handler(action="show", target=["Body1"]))
        assert out["bodies"][0] == {"body": "Body1", "light_bulb_on": True, "visible": False}
        assert "visible:false" in out["note"]

    def test_mixed_occurrence_and_body_hide(self, monkeypatch):
        occ = FakeOcc("Bracket", bulb=True)
        b = _body("Body1")
        _install(monkeypatch, [occ], bodies=[b])
        out = _payload(iv.handler(action="hide", target=["Bracket", "Body1"]))
        assert occ.isLightBulbOn is False and b.isLightBulbOn is False
        assert out["affected"] == ["Bracket", "Body1"]
        assert [r["body"] for r in out["bodies"]] == ["Body1"]   # read-back rows are bodies only

    def test_hide_via_face_handle_walks_to_owning_body(self, monkeypatch):
        # find_geometry mints only face/edge/vertex handles - never a body handle - so a body target
        # arrives as a FACE handle and must resolve to the face's OWNING body (TargetRef's owner-walk;
        # also how an ambiguous body NAME is disambiguated, per the refusal's own advice).
        b = _body("Body1")
        face = type("F", (), {})()
        face.body = b
        design = _install(monkeypatch, bodies=[b])
        design.findEntityByToken = lambda tok: [face] if tok == "TOK_FACE" else []
        import adsk.fusion
        monkeypatch.setattr(adsk.fusion, "BRepFace", type(face), raising=False)
        out = _payload(iv.handler(action="hide", target=["TOK_FACE"]))
        assert b.isLightBulbOn is False
        assert out["bodies"][0]["body"] == "Body1"

    def test_isolate_refuses_a_body_target(self, monkeypatch):
        # isolate stays occurrence-granular (Fusion has no body isolate) - a body name must not resolve.
        _install(monkeypatch, bodies=[_body("Body1")])
        res = iv.handler(action="isolate", target=["Body1"])
        assert res["isError"] is True and "no occurrence matching" in res["message"].lower()


# ── style ────────────────────────────────────────────────────────────────────

class TestStyle:
    def test_wireframe_sets_visual_style(self, monkeypatch):
        _install(monkeypatch)
        out = _payload(iv.handler(action="style", style="wireframe"))
        assert out["style"] == "wireframe"
        # 'wireframe' -> WireframeVisualStyle, seeded 2 in _install: the viewport must hold THAT
        # enum and the payload must report it (not merely echo wherever the fake ended up)
        assert iv.app.activeViewport.visualStyle == 2
        assert out["visual_style_after"] == 2

    def test_unknown_style_errors(self, monkeypatch):
        _install(monkeypatch)
        res = iv.handler(action="style", style="crayon")
        assert res["isError"] is True and "Provide 'style'" in res["message"]


# ── orient ───────────────────────────────────────────────────────────────────

class TestOrient:
    def test_unknown_orientation_errors(self, monkeypatch):
        _install(monkeypatch, [FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))])
        res = iv.handler(action="orient", orientation="sideways")
        assert res["isError"] is True and "Unknown orientation" in res["message"]

    def test_focus_unknown_occurrence_errors(self, monkeypatch):
        _install(monkeypatch, [FakeOcc("Part")])
        res = iv.handler(action="orient", orientation="front", focus="Ghost")
        assert res["isError"] is True and "no occurrence matching" in res["message"].lower()

    def test_front_orientation_sets_up_vector(self, monkeypatch):
        _install(monkeypatch, [FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))])
        out = _payload(iv.handler(action="orient", orientation="front", focus="Part"))
        assert out["applied"]["orientation"] == "front"
        assert out["applied"]["focus"] == "Part"
        # front up vector is +Z per _ORIENTATIONS
        up = iv.app.activeViewport.camera.upVector
        assert (up.x, up.y, up.z) == (0, 0, 1)

    def test_front_eye_placed_on_minus_y_at_preserved_distance(self, monkeypatch):
        # focus Part centered at (1,1,1); front view_dir = (0,-1,0). The default camera sits at
        # eye (10,10,10) target (0,0,0): distance sqrt(300) ~= 17.32. Eye must land at
        # target + dir*dist = (1, 1 - dist, 1) — pure -Y from the (now re-targeted) center.
        import math
        _install(monkeypatch, [FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))])
        _payload(iv.handler(action="orient", orientation="front", focus="Part"))
        cam = iv.app.activeViewport.camera
        dist = math.sqrt(300)
        assert cam.target.x == 1 and cam.target.y == 1 and cam.target.z == 1
        assert cam.eye.x == 1
        assert math.isclose(cam.eye.y, 1 - dist, rel_tol=1e-9)
        assert cam.eye.z == 1

    def test_top_orientation_uses_plus_y_up(self, monkeypatch):
        # 'top' view_dir = (0,0,1), up = (0,1,0) (Y-up because the look axis IS Z).
        _install(monkeypatch, [FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))])
        _payload(iv.handler(action="orient", orientation="top", focus="Part"))
        up = iv.app.activeViewport.camera.upVector
        assert (up.x, up.y, up.z) == (0, 1, 0)

    def test_a_readable_standoff_publishes_no_fallback(self, monkeypatch):
        # The good path: the camera's own eye-target distance placed the eye, so there is no
        # fallback to disclose and the payload must not carry the key.
        _install(monkeypatch, [FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))])
        out = _payload(iv.handler(action="orient", orientation="front"))
        assert "standoff_fallback_cm" not in out["applied"]
        assert "standoff_fallback_cm" not in out["note"]

    def test_an_eye_on_the_target_falls_back_to_the_shared_standoff_and_says_so(self, monkeypatch):
        # eye == target reads a ZERO distance, which would rebuild the eye ON the target and leave
        # the view no direction at all. The shared standoff stands in, and the payload discloses it.
        _install(monkeypatch, [FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))])
        # _cam, not the camera property: a read hands back a copy, so a setup written through it
        # would move nothing - the same trap the tool avoids by assigning its camera back.
        cam = iv.app.activeViewport._cam
        cam.eye = FakePoint(0, 0, 0)
        cam.target = FakePoint(0, 0, 0)
        out = _payload(iv.handler(action="orient", orientation="front"))
        assert out["applied"]["standoff_fallback_cm"] == iv._view_common.STANDOFF_FALLBACK_CM
        assert "standoff_fallback_cm=100" in out["note"]
        # the eye landed the fallback distance out along front's -Y, not on the target
        moved = iv.app.activeViewport.camera.eye
        assert (moved.x, moved.y, moved.z) == (0, -iv._view_common.STANDOFF_FALLBACK_CM, 0)

    def test_focus_only_translates_eye_by_target_delta(self, monkeypatch):
        # No orientation, just focus -> the eye is SHIFTED by the same vector the target moved, so the
        # view DIRECTION is preserved (camera tracks to the new center without rotating).
        _install(monkeypatch, [FakeOcc("Part", bbox=make_bbox((0, 0, 4), (2, 2, 8)))])  # center (1,1,6)
        # default camera: eye (10,10,10), target (0,0,0). target moves to (1,1,6): delta (1,1,6).
        _payload(iv.handler(action="orient", focus="Part"))
        cam = iv.app.activeViewport.camera
        assert (cam.target.x, cam.target.y, cam.target.z) == (1, 1, 6)
        # eye shifted by the same delta -> (11, 11, 16)
        assert (cam.eye.x, cam.eye.y, cam.eye.z) == (11, 11, 16)


class TestFocusFraming:
    """'focus' promises the view is FRAMED ON that occurrence. Re-aiming the camera at its bbox
    centre does not deliver that: the fit that follows an orient recomputes the camera extents over
    EVERY visible entity, so a small part in a large design stays a speck at the centre of a
    whole-model frame. Framing therefore hides the rest of the design, fits, and turns every bulb
    back on - and none of those three halves may go missing."""

    def test_framing_shrinks_the_extents_to_the_focus_share_of_the_frame(self, monkeypatch):
        # Part spans 2 on both screen axes inside a 100-wide world, so the camera has to come down
        # to 2/100 of its fitted extents, plus the margin that keeps it off the viewport border.
        near = FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))
        far = FakeOcc("FarAway", bbox=make_bbox((400, 0, 0), (402, 2, 2)))
        _install(monkeypatch, [near, far])
        out = _payload(iv.handler(action="orient", orientation="front", focus="Part"))
        assert out["applied"]["frame_ratio"] == pytest.approx(0.02 * iv._FRAME_MARGIN)
        assert iv.app.activeViewport.camera.viewExtents == pytest.approx(100.0 * 0.02 * iv._FRAME_MARGIN)
        assert "framed on 'Part'" in out["note"]

    def test_framing_touches_no_visibility_at_all(self, monkeypatch):
        # The whole point of framing by camera extents: a camera move must not be a visibility
        # write. Nothing goes dark, so nothing can be left dark.
        near = FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))
        far = FakeOcc("FarAway", bbox=make_bbox((400, 0, 0), (402, 2, 2)))
        _install(monkeypatch, [near, far])
        out = _payload(iv.handler(action="orient", orientation="front", focus="Part"))
        assert near.isLightBulbOn is True and far.isLightBulbOn is True
        assert far.isIsolated is False
        assert "visibility_not_restored" not in out["applied"]

    def test_the_taller_axis_wins_so_the_focus_is_never_cropped(self, monkeypatch):
        # A focus that is a small fraction ACROSS but a large one DOWN must be framed on the down
        # axis - taking the smaller ratio would crop it vertically.
        tall = FakeOcc("Tall", bbox=make_bbox((0, 0, 0), (1, 1, 50)))
        _install(monkeypatch, [tall])
        out = _payload(iv.handler(action="orient", orientation="front", focus="Tall"))
        assert out["applied"]["frame_ratio"] == pytest.approx(0.5 * iv._FRAME_MARGIN)   # 50/100, not 1/100

    def test_the_rest_of_the_design_does_not_affect_the_framing(self, monkeypatch):
        """Framing measures the focus against the frame the viewport SHOWS, read from the viewport
        itself, so the size of the rest of the design cannot enter the arithmetic. Dividing by the
        whole-design box instead makes a wide flat scene read ~20x too loose; this pins that the
        world is not an input."""
        part = FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))
        tiny_world = FakeOcc("Speck", bbox=make_bbox((0, 0, 0), (3, 3, 3)))
        huge_world = FakeOcc("Strip", bbox=make_bbox((0, 0, 0), (4000, 60, 30)))

        _install(monkeypatch, [part, tiny_world], design_bbox=make_bbox((0, 0, 0), (3, 3, 3)))
        small = _payload(iv.handler(action="orient", orientation="front",
                                    focus="Part"))["applied"]["frame_ratio"]
        _install(monkeypatch, [part, huge_world],
                 design_bbox=make_bbox((0, 0, 0), (4000, 60, 30)))
        large = _payload(iv.handler(action="orient", orientation="front",
                                    focus="Part"))["applied"]["frame_ratio"]
        assert small == pytest.approx(large)

    def test_an_unreadable_viewport_size_is_refused_without_moving_the_camera(self, monkeypatch):
        """No readable frame, no baseline to scale from. The tempting fallback - fit the whole
        model - would answer a framing request with the exact view framing exists to avoid, and
        report ok for it. So it refuses, and leaves the camera alone."""
        part = FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))
        _install(monkeypatch, [part])
        before = iv.app.activeViewport.camera.viewExtents
        monkeypatch.setattr(iv.app.activeViewport, "width", 0)
        res = iv.handler(action="orient", orientation="front", focus="Part")
        assert res["isError"] is True and "NOT moved" in res["message"]
        assert iv.app.activeViewport.camera.viewExtents == before
        assert iv.app.activeViewport._fit_calls == 0

    def test_framing_zooms_OUT_when_the_focus_is_bigger_than_the_current_frame(self, monkeypatch):
        """The baseline is the frame the viewport shows right now, so moving from a tightly framed
        part onto a bigger one legitimately zooms out. A ratio capped at 1.0 would leave the bigger
        part cropped."""
        big = FakeOcc("Big", bbox=make_bbox((0, 0, 0), (400, 400, 400)))
        _install(monkeypatch, [big])
        out = _payload(iv.handler(action="orient", orientation="front", focus="Big"))
        # 400 across a 200-wide frame, 400 down a 100-tall one -> the DOWN axis needs 4x
        assert out["applied"]["frame_ratio"] == pytest.approx(4.0 * iv._FRAME_MARGIN)
        assert iv.app.activeViewport.camera.viewExtents == pytest.approx(100.0 * 4.0 * iv._FRAME_MARGIN)

    def test_a_sketch_name_frames_the_sketch(self, monkeypatch):
        """Sketch work owns no occurrence, so an occurrence-only focus can never aim at it - and a
        sweep that draws 49 sketches has a whole category of thing nobody can see. A sketch carries
        a boundingBox, which is all the framing arithmetic needs."""
        sketch = types.SimpleNamespace(name="SlotBand",
                                       boundingBox=make_bbox((0, 0, 0), (50, 50, 0)))
        _install(monkeypatch, [FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))])
        monkeypatch.setattr(iv._common, "find_sketch", lambda d, n, remedy=None: (sketch, None))
        # TOP, because a flat XY sketch is edge-on from the front - its height span there is 0 and
        # the frame would be driven by width alone, which is correct but tells us nothing.
        out = _payload(iv.handler(action="orient", orientation="top", focus="SlotBand"))
        assert out["applied"]["focus"] == "SlotBand"
        # 50 across a 200-wide frame, 50 down a 100-tall one -> the DOWN axis wins at 0.5
        assert out["applied"]["frame_ratio"] == pytest.approx(0.5 * iv._FRAME_MARGIN)

    def test_an_occurrence_wins_over_a_sketch_of_the_same_name(self, monkeypatch):
        # The occurrence is tried first: a name carried by both must not silently frame the sketch.
        part = FakeOcc("Twin", bbox=make_bbox((0, 0, 0), (2, 2, 2)))
        sketch = types.SimpleNamespace(name="Twin",
                                       boundingBox=make_bbox((0, 0, 0), (50, 50, 0)))
        _install(monkeypatch, [part])
        monkeypatch.setattr(iv._common, "find_sketch", lambda d, n, remedy=None: (sketch, None))
        out = _payload(iv.handler(action="orient", orientation="front", focus="Twin"))
        assert out["applied"]["frame_ratio"] == pytest.approx(0.02 * iv._FRAME_MARGIN)   # the OCCURRENCE's size

    def test_a_shared_sketch_name_refusal_offers_a_call_this_input_takes(self, monkeypatch):
        # SKETCH-6: 'focus' carries no component scope, so no spelling of it separates two sketches
        # of one name. The default remedy - rename one - is the dead end (a shared sketch name most
        # often comes from two referenced documents), so this tool hands over its OWN way forward:
        # the occurrence fullPathName the same input already resolves.
        _install(monkeypatch, [FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))])
        seen = {}

        def _find(d, n, remedy=None):
            seen["remedy"] = remedy
            return None, f"2 sketches are named '{n}'. {remedy}"

        monkeypatch.setattr(iv._common, "find_sketch", _find)
        res = iv.handler(action="orient", orientation="front", focus="Plate")
        assert res["isError"] is True
        assert seen["remedy"] == iv._FOCUS_SKETCH_REMEDY
        assert "occurrence fullPathName" in res["message"]
        assert "Rename" not in res["message"]

    def test_a_name_that_is_neither_names_both_kinds(self, monkeypatch):
        _install(monkeypatch, [FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))])
        monkeypatch.setattr(iv._common, "find_sketch", lambda d, n, remedy=None: (None, None))
        res = iv.handler(action="orient", orientation="front", focus="Ghost")
        assert res["isError"] is True
        assert "occurrence" in res["message"] and "sketch" in res["message"]

    def test_several_names_frame_their_union(self, monkeypatch):
        """A tool-group belongs on screen together. Framing one member hides the rest of it, so a
        list frames the box enclosing them all - here two 2-wide parts 40 apart, which spans 42."""
        a = FakeOcc("GroupA", bbox=make_bbox((0, 0, 0), (2, 2, 2)))
        b = FakeOcc("GroupB", bbox=make_bbox((40, 0, 0), (42, 2, 2)))
        _install(monkeypatch, [a, b])
        out = _payload(iv.handler(action="orient", orientation="front",
                                  focus=["GroupA", "GroupB"]))
        assert out["applied"]["focus"] == "GroupA, GroupB"
        # union spans 42 across a 200-wide frame; 2 down a 100-tall one -> width wins
        assert out["applied"]["frame_ratio"] == pytest.approx(42 / 200 * iv._FRAME_MARGIN)

    def test_a_group_focus_naming_something_absent_is_refused(self, monkeypatch):
        # Silently framing the members that DID resolve would show a group missing a member with
        # nothing to say it happened.
        a = FakeOcc("GroupA", bbox=make_bbox((0, 0, 0), (2, 2, 2)))
        _install(monkeypatch, [a])
        monkeypatch.setattr(iv._common, "find_sketch", lambda d, n, remedy=None: (None, None))
        res = iv.handler(action="orient", orientation="front", focus=["GroupA", "Ghost"])
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_the_frame_margin_leaves_real_headroom(self):
        """The ratio tests reference the margin rather than hard-coding it, so it stays tunable -
        but a margin at or below 1.0 would draw the frame tight to the subject or inside it, which
        is a different behaviour, not a tuning."""
        assert iv._FRAME_MARGIN > 1.0

    def test_a_focus_with_no_measurable_size_is_re_aimed_not_refused(self, monkeypatch):
        """A point sketch spans nothing on either screen axis. There is no size to scale to, but
        aiming at it is still exactly what was asked for - so the zoom is left alone and the payload
        says so, rather than failing a framing the caller was right to request."""
        point = types.SimpleNamespace(name="W3Pt", boundingBox=make_bbox((5, 5, 0), (5, 5, 0)))
        _install(monkeypatch, [FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))])
        monkeypatch.setattr(iv._common, "find_sketch", lambda d, n, remedy=None: (point, None))
        before = iv.app.activeViewport.camera.viewExtents
        out = _payload(iv.handler(action="orient", orientation="top", focus="W3Pt"))
        assert out["applied"]["no_measurable_size"] is True
        assert out["applied"]["frame_ratio"] is None
        assert iv.app.activeViewport.camera.viewExtents == before   # zoom untouched

    def test_an_unreadable_bounding_box_is_refused_not_reported_as_framed(self, monkeypatch):
        # No box, no ratio. Returning ok here would publish "framed on X" over a whole-model view.
        blind = FakeOcc("Blind", bbox=None)
        _install(monkeypatch, [blind])
        res = iv.handler(action="orient", orientation="front", focus="Blind")
        assert res["isError"] is True
        # the refusal NAMES what it could not read - with a group focus, "something had no box" is
        # not enough to act on
        assert "Blind" in res["message"] and "bounding box" in res["message"]

    def test_an_extents_write_the_platform_drops_is_an_error(self, monkeypatch):
        import adsk.core
        near = FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))
        _install(monkeypatch, [near])
        # every camera READ hands back a fresh camera, so the extents write never sticks - the
        # framing silently did not happen, which must not be published as a framed view
        vp = _StubbornViewport(adsk.core.CameraTypes.OrthographicCameraType)
        monkeypatch.setattr(iv.app, "activeViewport", vp)
        res = iv.handler(action="orient", orientation="front", focus="Part")
        assert res["isError"] is True and "did not take" in res["message"]

    def test_fit_false_re_aims_without_framing_and_says_so(self, monkeypatch):
        near = FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))
        far = FakeOcc("FarAway", bbox=make_bbox((400, 0, 0), (402, 2, 2)))
        _install(monkeypatch, [near, far])
        out = _payload(iv.handler(action="orient", orientation="front", focus="Part", fit=False))
        assert iv.app.activeViewport._fit_calls == 0      # no framing pass at all
        assert far.isLightBulbOn is True                 # so no visibility was touched either
        assert "WITHOUT zooming" in out["note"]

    def test_no_focus_leaves_visibility_alone(self, monkeypatch):
        a = FakeOcc("A", bbox=make_bbox((0, 0, 0), (2, 2, 2)))
        b = FakeOcc("B", bbox=make_bbox((400, 0, 0), (402, 2, 2)))
        _install(monkeypatch, [a, b])
        out = _payload(iv.handler(action="orient", orientation="front"))
        # a whole-model orient is the isFitView path - it must not run the isolation walk
        assert iv.app.activeViewport._fit_calls == 0
        assert a.isLightBulbOn is True and b.isLightBulbOn is True
        assert "framed on" not in out["note"]

    def test_a_refused_projection_read_back_never_touches_visibility(self, monkeypatch):
        import adsk.core
        near = FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))
        far = FakeOcc("FarAway", bbox=make_bbox((400, 0, 0), (402, 2, 2)))
        _install(monkeypatch, [near, far])
        # a viewport that refuses the projection: the orient errors on the read-back, and framing
        # sits AFTER that check so a failed call leaves the design exactly as it found it.
        vp = _StubbornViewport(adsk.core.CameraTypes.OrthographicCameraType)
        monkeypatch.setattr(iv.app, "activeViewport", vp)
        res = iv.handler(action="orient", orientation="front", focus="Part",
                         projection="perspective")
        assert res["isError"] is True and "did not take" in res["message"]
        assert vp._fit_calls == 0
        assert near.isLightBulbOn is True and far.isLightBulbOn is True


class TestFocusExtents:
    """A focus is framed on the SOLIDS it models. An occurrence's plain boundingBox also counts the
    sketches and construction geometry its component holds, so a small part beside a large sketch
    frames the sketch; boundingBox2 is the bodies-only read, and the payload says which box the
    frame was measured on."""

    def _occ(self, name, solids, all_geometry, parent=None):
        """One occurrence whose bodies-only box and plain box differ; solids=None places no body."""
        return FakeOccurrence(bodies_bounding_box=solids,
                              **_occ_args(name, bbox=all_geometry, parent=parent))

    def test_a_body_holding_occurrence_frames_on_its_solids(self, monkeypatch):
        # front screen axes: X across a 200-wide frame, Z down a 100-tall one. The solids span
        # 3 x 2 (Z wins at 0.02); the sketch-swept box spans 50 x 2 (X wins at 0.25).
        part = self._occ("ShellCap:1", make_bbox((0, 0, 0), (3, 3, 2)),
                         make_bbox((0, 0, 0), (50, 50, 2)))
        _install(monkeypatch, [part])
        out = _payload(iv.handler(action="orient", orientation="front", focus="ShellCap:1"))
        assert out["applied"]["frame_ratio"] == pytest.approx(0.02 * iv._FRAME_MARGIN)
        assert out["applied"]["extents"] == "solids"

    def test_a_bodyless_occurrence_falls_back_to_its_full_box(self, monkeypatch):
        # Measured: an occurrence placing no body answers None through boundingBox2. Refusing there
        # would leave a sketch-only or construction-only component with nothing that frames it.
        band = self._occ("Layout:1", None, make_bbox((0, 0, 0), (50, 50, 2)))
        _install(monkeypatch, [band])
        out = _payload(iv.handler(action="orient", orientation="front", focus="Layout:1"))
        assert out["applied"]["frame_ratio"] == pytest.approx(0.25 * iv._FRAME_MARGIN)
        assert out["applied"]["extents"] == "all_geometry"

    def test_a_mixed_list_unions_the_solids_with_the_bodyless_box_and_says_all_geometry(
            self, monkeypatch):
        # The union carries one full box, so the frame is not a solids-only one and does not claim
        # to be. Spans X 0..14 on the solids box; the part's sketch-swept box would reach 50.
        part = self._occ("Cap:1", make_bbox((0, 0, 0), (3, 3, 2)),
                         make_bbox((0, 0, 0), (50, 50, 2)))
        band = self._occ("Layout:1", None, make_bbox((10, 0, 0), (14, 4, 2)))
        _install(monkeypatch, [part, band])
        out = _payload(iv.handler(action="orient", orientation="front",
                                  focus=["Cap:1", "Layout:1"]))
        assert out["applied"]["frame_ratio"] == pytest.approx(14 / 200 * iv._FRAME_MARGIN)
        assert out["applied"]["extents"] == "all_geometry"

    def test_a_nested_occurrence_frames_where_it_is_placed(self, monkeypatch):
        # The box is read off the placed INSTANCE, whose extents are in root space. The component's
        # own box sits at the origin, so framing on that aims the camera 100 cm from the part.
        parent = FakeOcc("Frame:1", bbox=make_bbox((100, 0, 0), (103, 3, 2)))
        comp = MakeComp(name="Cap")
        comp.boundingBox = make_bbox((0, 0, 0), (3, 3, 2))
        cap = FakeOccurrence(path="Frame:1+Cap:1", component=comp, assembly_context=parent,
                             bounding_box=make_bbox((100, 0, 0), (150, 50, 2)),
                             bodies_bounding_box=make_bbox((100, 0, 0), (103, 3, 2)))
        _install(monkeypatch, [parent, cap])
        out = _payload(iv.handler(action="orient", orientation="front", focus="Frame:1+Cap:1"))
        cam = iv.app.activeViewport.camera
        assert (cam.target.x, cam.target.y, cam.target.z) == (101.5, 1.5, 1)
        assert out["applied"]["frame_ratio"] == pytest.approx(0.02 * iv._FRAME_MARGIN)
        assert out["applied"]["extents"] == "solids"


# ── camera projection ───────────────────────────────────────────────────────
# 'projection' maps a wire key onto an adsk.core.CameraTypes member and is READ BACK off the
# viewport camera; 'perspective_angle_deg' is a degrees-in / radians-to-the-API field of view that
# only a perspective camera accepts.

class TestProjection:
    def _part(self):
        return FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))

    def test_each_key_maps_to_its_own_camera_types_member(self, monkeypatch):
        import adsk.core
        expected = {"orthographic": adsk.core.CameraTypes.OrthographicCameraType,
                    "perspective": adsk.core.CameraTypes.PerspectiveCameraType}
        assert len(set(expected.values())) == 2          # two distinct members, not one alias
        for key, member in expected.items():
            _install(monkeypatch, [self._part()])
            out = _payload(iv.handler(action="orient", projection=key))
            assert iv.app.activeViewport.camera.cameraType == member, key
            assert out["applied"]["projection"] == key

    def test_unknown_projection_is_refused_listing_the_valid_keys(self, monkeypatch):
        _install(monkeypatch, [self._part()])
        res = iv.handler(action="orient", projection="fisheye")
        assert res["isError"] is True
        assert "Unknown projection 'fisheye'" in res["message"]
        assert "orthographic, perspective" in res["message"]

    def test_perspective_ortho_faces_is_not_offered(self, monkeypatch):
        # An assigned PerspectiveWithOrthoFaces camera reads back as PerspectiveCameraType (wrote
        # 2, read 1, measured with isFitView set), so the key is REFUSED rather than offered and
        # silently downgraded - offering it would trip the read-back mismatch on a correct call.
        assert list(iv._PROJECTIONS) == ["orthographic", "perspective"]      # the schema's enum
        _install(monkeypatch, [self._part()])
        res = iv.handler(action="orient", projection="perspective_ortho_faces")
        assert res["isError"] is True
        assert "Unknown projection 'perspective_ortho_faces'" in res["message"]
        assert "orthographic, perspective" in res["message"]

    def test_projection_combines_with_an_orientation_in_one_call(self, monkeypatch):
        import adsk.core
        _install(monkeypatch, [self._part()])
        out = _payload(iv.handler(action="orient", orientation="front", projection="perspective",
                                  focus="Part"))
        assert out["applied"]["orientation"] == "front"
        assert out["applied"]["projection"] == "perspective"
        cam = iv.app.activeViewport.camera
        assert cam.cameraType == adsk.core.CameraTypes.PerspectiveCameraType
        assert (cam.upVector.x, cam.upVector.y, cam.upVector.z) == (0, 0, 1)   # orientation kept

    def test_a_projection_that_does_not_take_is_an_error(self, monkeypatch):
        # the viewport swallows the assignment and keeps reading back orthographic - that must be
        # an error naming what it actually reads, never a false ok.
        import adsk.core
        _install(monkeypatch, [self._part()])
        monkeypatch.setattr(iv.app, "activeViewport",
                            _StubbornViewport(adsk.core.CameraTypes.OrthographicCameraType))
        res = iv.handler(action="orient", projection="perspective")
        assert res["isError"] is True
        assert "reads back 'orthographic'" in res["message"]
        assert "did not take" in res["message"]

    def test_an_unreadable_camera_type_is_an_error_not_a_mismatch_claim(self, monkeypatch):
        # an unreadable property is a different report from a read that shows the change did not
        # take - it must not be recast as a mismatch against a stringified None.
        import adsk.core
        _install(monkeypatch, [self._part()])
        monkeypatch.setattr(iv.app, "activeViewport",
                            _StubbornViewport(adsk.core.CameraTypes.PerspectiveCameraType,
                                              type_readable=False))
        res = iv.handler(action="orient", projection="perspective")
        assert res["isError"] is True
        assert "cameraType could not be read back" in res["message"]
        assert "projection is unverified" in res["message"]
        assert "did not take" not in res["message"]
        assert "None" not in res["message"]

    def test_untouched_projection_is_not_reported(self, monkeypatch):
        # an orient with no projection input must not claim a projection it never set
        _install(monkeypatch, [self._part()])
        out = _payload(iv.handler(action="orient", orientation="top", focus="Part"))
        assert "projection" not in out["applied"]

    def test_angle_only_call_does_not_claim_an_applied_projection(self, monkeypatch):
        import adsk.core
        _install(monkeypatch, [self._part()])
        iv.app.activeViewport._cam.cameraType = adsk.core.CameraTypes.PerspectiveCameraType
        out = _payload(iv.handler(action="orient", perspective_angle_deg=40))
        assert "projection" not in out["applied"]        # nothing was applied to the projection
        assert out["applied"]["perspective_angle_deg"] == 40.0


class TestProjectionExtents:
    """Flipping cameraType leaves the camera's extents inconsistent with the type it now carries -
    assigning such a camera raises 'Camera type must be orthographic for extents' unless isFitView
    recomputes them. So the projection path fits whether or not the caller asked it to."""

    def _part(self):
        return FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))

    def test_projection_sets_isfitview_even_when_fit_is_false(self, monkeypatch):
        _install(monkeypatch, [self._part()])
        out = _payload(iv.handler(action="orient", projection="perspective", fit=False))
        assert iv.app.activeViewport.camera.isFitView is True
        assert "fitted even though fit was false" in out["note"]

    def test_a_plain_orient_still_honours_fit_false(self, monkeypatch):
        # no projection change -> no extents recompute is needed, so fit=false stays fit=false
        _install(monkeypatch, [self._part()])
        out = _payload(iv.handler(action="orient", orientation="front", focus="Part", fit=False))
        assert iv.app.activeViewport.camera.isFitView is False
        assert "fitted even though" not in out["note"]

    def test_fit_true_with_a_projection_says_nothing_extra(self, monkeypatch):
        _install(monkeypatch, [self._part()])
        out = _payload(iv.handler(action="orient", projection="perspective", fit=True))
        assert iv.app.activeViewport.camera.isFitView is True
        assert "fitted even though" not in out["note"]


class TestPerspectiveAngle:
    def _part(self):
        return FakeOcc("Part", bbox=make_bbox((0, 0, 0), (2, 2, 2)))

    def test_degrees_reach_the_camera_as_radians(self, monkeypatch):
        # Camera.perspectiveAngle is radians (a fresh perspective camera reads 0.39479 = Fusion's
        # 22.62 deg default field of view), and a written angle survives the single camera
        # assignment bit-exactly - so the value on the camera is EXACTLY radians(45), and the
        # degree number itself never reaches the property.
        import math
        _install(monkeypatch, [self._part()])
        out = _payload(iv.handler(action="orient", projection="perspective",
                                  perspective_angle_deg=45))
        cam = iv.app.activeViewport.camera
        assert cam.perspectiveAngle == math.radians(45)
        assert cam.perspectiveAngle != 45
        assert out["applied"]["perspective_angle_deg"] == 45.0

    def test_angle_allowed_on_a_camera_already_in_perspective_ortho_faces(self, monkeypatch):
        # view_set cannot SET that mode - an assigned PerspectiveWithOrthoFaces camera reads back
        # as plain Perspective - but a camera ALREADY in it accepts a written angle exactly
        # (30 deg written, 30 deg read, live-measured), so the angle path must admit it.
        import adsk.core
        import math
        _install(monkeypatch, [self._part()])
        iv.app.activeViewport._cam.cameraType = (
            adsk.core.CameraTypes.PerspectiveWithOrthoFacesCameraType)
        out = _payload(iv.handler(action="orient", perspective_angle_deg=35))
        assert iv.app.activeViewport.camera.perspectiveAngle == math.radians(35)
        assert out["applied"]["perspective_angle_deg"] == 35.0

    def test_an_unreadable_camera_type_on_the_angle_path_names_no_projection(self, monkeypatch):
        # the precondition needs the camera's OWN type when no projection is given; when that read
        # fails there is no projection to name, so the refusal must say the read failed rather than
        # report a projection whose name is the missing read.
        import adsk.core
        _install(monkeypatch, [self._part()])
        monkeypatch.setattr(iv.app, "activeViewport",
                            _StubbornViewport(adsk.core.CameraTypes.PerspectiveCameraType,
                                              type_readable=False))
        res = iv.handler(action="orient", perspective_angle_deg=45)
        assert res["isError"] is True
        assert "cameraType could not be read" in res["message"]
        assert "None" not in res["message"]
        assert "projection='perspective'" in res["message"]

    def test_an_unreadable_angle_is_an_error_not_a_reported_zero(self, monkeypatch):
        # the read-back is a MEASUREMENT: when the property cannot be read, publishing 0.0 would
        # claim a field of view of zero degrees. Error naming the camera state instead.
        import adsk.core
        _install(monkeypatch, [self._part()])
        monkeypatch.setattr(iv.app, "activeViewport",
                            _StubbornViewport(adsk.core.CameraTypes.PerspectiveCameraType,
                                              angle_readable=False))
        res = iv.handler(action="orient", projection="perspective", perspective_angle_deg=45)
        assert res["isError"] is True
        assert "could not be read back" in res["message"]
        assert "'perspective'" in res["message"]         # names the camera state it found
        assert "0.0" not in res["message"]

    def test_angle_on_an_orthographic_projection_is_refused(self, monkeypatch):
        _install(monkeypatch, [self._part()])
        res = iv.handler(action="orient", projection="orthographic", perspective_angle_deg=35)
        assert res["isError"] is True
        assert "35" in res["message"] and "'orthographic'" in res["message"]
        assert "perspective_ortho_faces" not in res["message"]   # not a value it can suggest
        assert iv.app.activeViewport.camera.perspectiveAngle == 0.0   # nothing was written

    def test_angle_without_a_projection_refuses_against_the_current_camera(self, monkeypatch):
        # no projection given and the camera is orthographic -> the conflict is with the camera's
        # OWN type, and the refusal names it rather than assuming a perspective camera.
        _install(monkeypatch, [self._part()])
        res = iv.handler(action="orient", perspective_angle_deg=35)
        assert res["isError"] is True and "'orthographic'" in res["message"]

    def test_angle_without_a_projection_is_allowed_on_a_perspective_camera(self, monkeypatch):
        import adsk.core
        import math
        _install(monkeypatch, [self._part()])
        iv.app.activeViewport._cam.cameraType = adsk.core.CameraTypes.PerspectiveCameraType
        out = _payload(iv.handler(action="orient", perspective_angle_deg=45))
        assert math.isclose(iv.app.activeViewport.camera.perspectiveAngle, math.radians(45),
                            rel_tol=1e-9)
        assert out["applied"]["perspective_angle_deg"] == 45.0

    def test_angle_outside_the_accepted_range_is_refused(self, monkeypatch):
        # Fusion's setter accepts [1, 150) degrees and raises "3 : Invalid parameter value" outside
        # it, so the guard refuses by name rather than letting a raw platform error surface.
        for bad in (0, 0.99, -10, 150, 179.9, 200):
            _install(monkeypatch, [self._part()])
            res = iv.handler(action="orient", projection="perspective", perspective_angle_deg=bad)
            assert res["isError"] is True, bad
            assert str(float(bad)) in res["message"], bad
            assert "1 to just under 150" in res["message"], bad
            assert iv.app.activeViewport.camera.perspectiveAngle == 0.0, bad   # never written

    def test_the_accepted_boundaries_are_allowed(self, monkeypatch):
        # 1.0 is accepted (0.99 raises) and 149.99 is accepted (150 raises) - both live-measured,
        # so neither may be refused by our own guard.
        import math
        for good in (1, 149.99):
            _install(monkeypatch, [self._part()])
            out = _payload(iv.handler(action="orient", projection="perspective",
                                      perspective_angle_deg=good))
            assert out["applied"]["perspective_angle_deg"] == float(good), good
            assert iv.app.activeViewport.camera.perspectiveAngle == math.radians(good), good

    def test_non_numeric_angle_is_refused(self, monkeypatch):
        _install(monkeypatch, [self._part()])
        res = iv.handler(action="orient", projection="perspective", perspective_angle_deg="wide")
        assert res["isError"] is True and "wide" in res["message"]

    def test_an_angle_the_camera_settles_elsewhere_reports_both(self, monkeypatch):
        # the camera comes back at 20 deg after a 45 deg request - report what it READS BACK and
        # name the requested value, rather than echoing the request as if it stuck.
        import adsk.core
        import math
        _install(monkeypatch, [self._part()])
        monkeypatch.setattr(iv.app, "activeViewport",
                            _StubbornViewport(adsk.core.CameraTypes.PerspectiveCameraType,
                                              perspective_angle=math.radians(20)))
        out = _payload(iv.handler(action="orient", projection="perspective",
                                  perspective_angle_deg=45))
        assert out["applied"]["perspective_angle_deg"] == 20.0
        assert out["applied"]["perspective_angle_requested_deg"] == 45.0
        assert "different perspective angle" in out["note"]

    def test_a_matching_angle_read_back_reports_no_divergence(self, monkeypatch):
        _install(monkeypatch, [self._part()])
        out = _payload(iv.handler(action="orient", projection="perspective",
                                  perspective_angle_deg=45))
        assert "perspective_angle_requested_deg" not in out["applied"]
        assert "different perspective angle" not in out["note"]


class TestProjectionOnlyOnOrient:
    def test_projection_on_another_action_is_refused(self, monkeypatch):
        # accepting it on 'style'/'snapshot' would report ok while setting no projection at all
        _install(monkeypatch, [FakeOcc("Part")])
        res = iv.handler(action="style", style="wireframe", projection="perspective")
        assert res["isError"] is True
        assert "action='style'" in res["message"]

    def test_perspective_angle_on_another_action_is_refused(self, monkeypatch):
        _install(monkeypatch, [FakeOcc("Part")])
        res = iv.handler(action="list_views", perspective_angle_deg=45)
        assert res["isError"] is True and "action='list_views'" in res["message"]

    def test_other_actions_are_unaffected_when_the_inputs_are_absent(self, monkeypatch):
        _install(monkeypatch, [FakeOcc("Part")])
        out = _payload(iv.handler(action="style", style="wireframe"))
        assert out["style"] == "wireframe"


# ── named views ──────────────────────────────────────────────────────────────

class TestNamedViews:
    def test_save_view_adds(self, monkeypatch):
        nvs = FakeNamedViews()
        _install(monkeypatch, [], named_views=nvs)
        out = _payload(iv.handler(action="save_view", view_name="MyAngle"))
        assert out["view_name"] == "MyAngle"
        assert nvs.count == 1

    def test_save_view_overwrites_same_name(self, monkeypatch):
        old = FakeNamedView("MyAngle")
        nvs = FakeNamedViews([old])
        _install(monkeypatch, [], named_views=nvs)
        _payload(iv.handler(action="save_view", view_name="MyAngle"))
        assert old._deleted is True          # old one removed before re-add
        assert nvs.count == 1                 # still just one "MyAngle"

    def test_save_view_requires_name(self, monkeypatch):
        _install(monkeypatch, [], named_views=FakeNamedViews())
        res = iv.handler(action="save_view")
        assert res["isError"] is True and "Provide 'view_name'" in res["message"]

    def test_apply_view_moves_camera(self, monkeypatch):
        nv = FakeNamedView("Home", built_in=True)
        _install(monkeypatch, [], named_views=FakeNamedViews([nv]))
        _payload(iv.handler(action="apply_view", view_name="Home"))
        assert nv.applied is True

    def test_apply_returning_false_is_an_error_not_a_moved_camera(self, monkeypatch):
        # NamedView.apply() RETURNS a bool ("true if the operation was successful") - discarding it
        # reported a camera move the platform declined to make.
        nv = FakeNamedView("Side")
        nv.apply = lambda: False
        _install(monkeypatch, [], named_views=FakeNamedViews([nv]))
        res = iv.handler(action="apply_view", view_name="Side")
        assert res["isError"] is True
        assert "returned false" in res["message"] and "Side" in res["message"]

    def test_an_apply_that_raises_is_reported_not_swallowed(self, monkeypatch):
        nv = FakeNamedView("Side")

        def boom():
            raise RuntimeError("camera is busy")
        nv.apply = boom
        _install(monkeypatch, [], named_views=FakeNamedViews([nv]))
        res = iv.handler(action="apply_view", view_name="Side")
        assert res["isError"] is True and "camera is busy" in res["message"]

    def test_save_over_a_view_that_refuses_deletion_is_refused(self, monkeypatch):
        # deleteMe() answers a bool. Swallowing a false and adding anyway leaves TWO views sharing
        # one name, which no later apply_view can tell apart.
        stubborn = FakeNamedView("Side")
        stubborn.deleteMe = lambda: False
        nvs = FakeNamedViews([stubborn])
        _install(monkeypatch, [], named_views=nvs)
        res = iv.handler(action="save_view", view_name="Side")
        assert res["isError"] is True
        assert "refused to remove it" in res["message"]
        assert nvs.count == 1                     # no duplicate was added

    def test_apply_unknown_view_lists_available(self, monkeypatch):
        _install(monkeypatch, [], named_views=FakeNamedViews([FakeNamedView("Home")]))
        res = iv.handler(action="apply_view", view_name="Nope")
        assert res["isError"] is True
        assert "No named view 'Nope'" in res["message"]
        assert "Home" in res["message"]

    def test_list_views_reports_builtin_flag(self, monkeypatch):
        _install(monkeypatch, [], named_views=FakeNamedViews(
            [FakeNamedView("Home", built_in=True), FakeNamedView("Mine")]))
        out = _payload(iv.handler(action="list_views"))
        assert out["count"] == 2
        by = {v["name"]: v["built_in"] for v in out["named_views"]}
        assert by["Home"] is True and by["Mine"] is False


# ── snapshot / restore round-trip ───────────────────────────────────────────

class TestSnapshotRestore:
    def test_restore_without_snapshot_errors(self, monkeypatch):
        _install(monkeypatch, [FakeOcc("A")], doc_name="FreshDoc")
        iv._SNAPSHOTS.clear()
        res = iv.handler(action="restore")
        assert res["isError"] is True and "No snapshot saved" in res["message"]

    def test_snapshot_then_restore_puts_bulbs_back(self, monkeypatch):
        a = FakeOcc("A", full_path="A", bulb=True)
        b = FakeOcc("B", full_path="B", bulb=True)
        _install(monkeypatch, [a, b], doc_name="RoundTrip")
        iv._SNAPSHOTS.clear()
        _payload(iv.handler(action="snapshot"))
        # mutate after snapshot
        a.isLightBulbOn = False
        b.isLightBulbOn = False
        out = _payload(iv.handler(action="restore"))
        assert out["restored_occurrences"] == 2
        assert a.isLightBulbOn is True and b.isLightBulbOn is True
        # snapshot consumed (popped) on restore
        assert "RoundTrip" not in iv._SNAPSHOTS

    def test_restore_reinstates_isolation(self, monkeypatch):
        # snapshot captures an isolated occurrence; after clearing it, restore must put isolation back.
        a = FakeOcc("A", full_path="A", bulb=True, isolated=True)
        _install(monkeypatch, [a], doc_name="IsoRestore")
        iv._SNAPSHOTS.clear()
        _payload(iv.handler(action="snapshot"))
        a.isIsolated = False                 # user un-isolated after the snapshot
        out = _payload(iv.handler(action="restore"))
        assert out["restored_occurrences"] == 1
        assert a.isIsolated is True          # isolation reinstated from the snapshot

    def test_restore_counts_missing_occurrences(self, monkeypatch):
        a = FakeOcc("A", full_path="A")
        _install(monkeypatch, [a], doc_name="MissingTest")
        iv._SNAPSHOTS.clear()
        _payload(iv.handler(action="snapshot"))
        # remove 'A' from the design before restoring -> it's now missing
        iv.app.activeProduct.rootComponent.allOccurrences = []
        out = _payload(iv.handler(action="restore"))
        assert out["missing_occurrences"] == 1
        assert out["restored_occurrences"] == 0

    def test_a_snapshot_under_the_cap_makes_no_truncation_claim(self, monkeypatch):
        occs = [FakeOcc(f"O{i}", full_path=f"O{i}") for i in range(3)]
        _install(monkeypatch, occs, doc_name="Small")
        iv._SNAPSHOTS.clear()
        out = _payload(iv.handler(action="snapshot"))
        assert out["occurrences_saved"] == 3
        assert "truncated" not in out and "PARTIAL" not in out["note"]

    def test_a_snapshot_past_the_cap_discloses_the_partial_capture(self, monkeypatch):
        # An assembly bigger than the cap gets a PARTIAL snapshot. Reporting that as "all
        # occurrence visibility saved" is the lie: restore cannot put back what was never saved.
        monkeypatch.setattr(iv, "_MAX_OCC", 3)
        occs = [FakeOcc(f"O{i}", full_path=f"O{i}") for i in range(5)]
        _install(monkeypatch, occs, doc_name="Huge")
        iv._SNAPSHOTS.clear()
        out = _payload(iv.handler(action="snapshot"))
        assert out["truncated"] is True and out["occurrence_cap"] == 3
        assert out["occurrences_saved"] == 3
        assert "PARTIAL" in out["note"]

    def test_a_restore_from_a_partial_snapshot_says_so(self, monkeypatch):
        monkeypatch.setattr(iv, "_MAX_OCC", 3)
        occs = [FakeOcc(f"O{i}", full_path=f"O{i}", bulb=True) for i in range(5)]
        _install(monkeypatch, occs, doc_name="HugeRestore")
        iv._SNAPSHOTS.clear()
        _payload(iv.handler(action="snapshot"))
        for o in occs:
            o.isLightBulbOn = False
        out = _payload(iv.handler(action="restore"))
        assert out["truncated"] is True and out["occurrence_cap"] == 3
        assert out["restored_occurrences"] == 3
        assert "PARTIAL" in out["note"]
        assert [o.isLightBulbOn for o in occs] == [True, True, True, False, False]

    def test_a_partial_snapshot_stays_partial_when_the_design_later_fits_under_the_cap(
            self, monkeypatch):
        # The snapshot is what was CAPTURED; the restore walk only says what is reachable NOW.
        # If the design shrinks under the cap between the two calls the walk reads complete, so
        # only the stored flag still knows the capture was partial - reading the walk alone
        # reports a full restore of a state that was never fully saved.
        monkeypatch.setattr(iv, "_MAX_OCC", 3)
        occs = [FakeOcc(f"O{i}", full_path=f"O{i}", bulb=True) for i in range(5)]
        _install(monkeypatch, occs, doc_name="Shrinker")
        iv._SNAPSHOTS.clear()
        snap = _payload(iv.handler(action="snapshot"))
        assert snap["truncated"] is True
        # two occurrences deleted after the snapshot - the walk now fits under the cap
        iv.app.activeProduct.rootComponent.allOccurrences = occs[:3]
        out = _payload(iv.handler(action="restore"))
        assert out["truncated"] is True and out["occurrence_cap"] == 3
        assert out["restored_occurrences"] == 3
        assert "PARTIAL" in out["note"]

    def test_a_complete_snapshot_restored_into_a_grown_design_discloses_the_capped_walk(
            self, monkeypatch):
        # The mirror case: the CAPTURE was complete, but the design grew past the cap before the
        # restore, so the restore only LOOKED at the first cap occurrences. The stored flag says
        # nothing here - only the current walk knows the restore pass was partial.
        monkeypatch.setattr(iv, "_MAX_OCC", 3)
        occs = [FakeOcc(f"O{i}", full_path=f"O{i}", bulb=True) for i in range(2)]
        _install(monkeypatch, occs, doc_name="Grower")
        iv._SNAPSHOTS.clear()
        snap = _payload(iv.handler(action="snapshot"))
        assert "truncated" not in snap                  # the capture itself was complete
        grown = occs + [FakeOcc(f"N{i}", full_path=f"N{i}") for i in range(4)]
        iv.app.activeProduct.rootComponent.allOccurrences = grown
        out = _payload(iv.handler(action="restore"))
        assert out["truncated"] is True and out["occurrence_cap"] == 3
        assert "PARTIAL" in out["note"]

    def test_a_full_restore_keeps_the_plain_success_note(self, monkeypatch):
        occs = [FakeOcc(f"O{i}", full_path=f"O{i}", bulb=True) for i in range(2)]
        _install(monkeypatch, occs, doc_name="FullRestore")
        iv._SNAPSHOTS.clear()
        _payload(iv.handler(action="snapshot"))
        out = _payload(iv.handler(action="restore"))
        assert "truncated" not in out and "PARTIAL" not in out["note"]

    def test_clear_isolation_past_the_cap_discloses_what_it_did_not_check(self, monkeypatch):
        monkeypatch.setattr(iv, "_MAX_OCC", 2)
        occs = [FakeOcc(f"O{i}", full_path=f"O{i}", isolated=True) for i in range(4)]
        _install(monkeypatch, occs)
        out = _payload(iv.handler(action="clear_isolation"))
        assert out["cleared_count"] == 2 and out["truncated"] is True
        assert [o.isIsolated for o in occs] == [False, False, True, True]

    def test_clear_isolation_under_the_cap_claims_nothing_extra(self, monkeypatch):
        occs = [FakeOcc("A", full_path="A", isolated=True)]
        _install(monkeypatch, occs)
        out = _payload(iv.handler(action="clear_isolation"))
        assert out["cleared_count"] == 1 and "truncated" not in out

    def test_a_bulb_that_will_not_go_back_keeps_the_snapshot_and_is_not_counted(self, monkeypatch):
        # The snapshot is the ONLY copy of the pre-explore state. Counting attempts as restored and
        # then POPPING it made a wholly failed restore unrecoverable while reporting success.
        good = FakeOcc("A", full_path="A", bulb=True)
        stuck = _StubbornOcc("B", full_path="B", bulb=True, swallow=("isLightBulbOn",))
        _install(monkeypatch, [good, stuck], doc_name="StuckRestore")
        iv._SNAPSHOTS.clear()
        key = _payload(iv.handler(action="snapshot"))["saved_for"]
        object.__setattr__(stuck, "isLightBulbOn", False)     # hidden after the snapshot
        good.isLightBulbOn = False
        out = _payload(iv.handler(action="restore"))
        assert out["restored_occurrences"] == 1               # only the one that read back
        assert out["failed_restores"] == ["B"] and out["failed_restore_count"] == 1
        assert out["snapshot_kept"] is True
        assert key in iv._SNAPSHOTS                           # retryable, not consumed
        assert "PARTIAL RESTORE" in out["note"]

    def test_a_clean_restore_still_consumes_the_snapshot(self, monkeypatch):
        a = FakeOcc("A", full_path="A", bulb=True)
        _install(monkeypatch, [a], doc_name="CleanRestore")
        iv._SNAPSHOTS.clear()
        key = _payload(iv.handler(action="snapshot"))["saved_for"]
        a.isLightBulbOn = False
        out = _payload(iv.handler(action="restore"))
        assert out["snapshot_kept"] is False and "failed_restores" not in out
        assert out["visual_style_restored"] is True and out["camera_restored"] is True
        assert key not in iv._SNAPSHOTS

    def test_a_camera_the_viewport_refuses_is_reported_and_keeps_the_snapshot(self, monkeypatch):
        a = FakeOcc("A", full_path="A", bulb=True)
        _install(monkeypatch, [a], doc_name="CamRestore")
        iv._SNAPSHOTS.clear()
        key = _payload(iv.handler(action="snapshot"))["saved_for"]

        class _RefusingViewport(Viewport):
            """A viewport that hands back a camera but will not take one - the restore the
            platform declines after the explore already moved the view."""

            @property
            def camera(self):
                return self._cam

            @camera.setter
            def camera(self, value):
                raise RuntimeError("viewport busy")
        monkeypatch.setattr(iv.app, "activeViewport", _RefusingViewport(camera=_camera()))
        out = _payload(iv.handler(action="restore"))
        assert out["camera_restored"] is False
        assert "camera" in out["failed_restores"]
        assert "viewport busy" in out["note"]
        assert key in iv._SNAPSHOTS

    def test_a_visual_style_that_does_not_land_is_reported(self, monkeypatch):
        a = FakeOcc("A", full_path="A", bulb=True)
        _install(monkeypatch, [a], doc_name="StyleRestore")
        iv._SNAPSHOTS.clear()
        iv.app.activeViewport.visualStyle = 1
        key = _payload(iv.handler(action="snapshot"))["saved_for"]

        class _StuckStyle(Viewport):
            def __setattr__(self, key, value):
                if key == "visualStyle" and getattr(self, "_armed", False):
                    return
                object.__setattr__(self, key, value)
        vp = _StuckStyle(camera=_camera())
        vp.visualStyle = 7                       # the style the viewport insists on keeping
        object.__setattr__(vp, "_armed", True)
        monkeypatch.setattr(iv.app, "activeViewport", vp)
        out = _payload(iv.handler(action="restore"))
        assert out["visual_style_restored"] is False
        assert "visualStyle" in out["failed_restores"]
        assert key in iv._SNAPSHOTS

    def test_saved_documents_sharing_a_name_key_on_their_data_file_id(self, monkeypatch):
        # _SNAPSHOTS is keyed by document, not name: two open documents that happen to share a
        # name (e.g. two "Untitled") must not clobber each other's saved state. A snapshot saved
        # under one doc id must NOT be visible/restorable under another doc that shares the same
        # NAME but has a different id.
        a = FakeOcc("A", full_path="A", bulb=True)
        _install(monkeypatch, [a], doc_name="Untitled", doc_id="urn:doc-one")
        iv._SNAPSHOTS.clear()
        _payload(iv.handler(action="snapshot"))
        assert "urn:doc-one" in iv._SNAPSHOTS
        # switch to a DIFFERENT document that happens to share the same NAME
        b = FakeOcc("B", full_path="B", bulb=True)
        _install(monkeypatch, [b], doc_name="Untitled", doc_id="urn:doc-two")
        res = iv.handler(action="restore")
        assert res["isError"] is True and "No snapshot saved" in res["message"]
        # the first document's snapshot is untouched
        assert "urn:doc-one" in iv._SNAPSHOTS

    def test_two_unsaved_documents_sharing_a_name_get_different_keys(self, monkeypatch):
        # THE branch a name key collides on: a never-saved document has NO dataFile, and several
        # open "Untitled" documents are ordinary. Each must land under its own key.
        one, two = OpenDocument("Untitled"), OpenDocument("Untitled")
        _install_open_document(monkeypatch, [FakeOcc("A", full_path="A")], one)
        first = _payload(iv.handler(action="snapshot"))["saved_for"]
        _install_open_document(monkeypatch, [FakeOcc("B", full_path="B")], two)
        second = _payload(iv.handler(action="snapshot"))["saved_for"]
        assert first != second
        assert len(iv._SNAPSHOTS) == 2                     # neither overwrote the other
        assert set(iv._SNAPSHOTS) == {first, second}

    def test_an_unsaved_documents_snapshot_is_not_restorable_into_another(self, monkeypatch):
        # The clobber itself: doc one's camera/style/bulbs must not be restorable INTO doc two.
        one, two = OpenDocument("Untitled"), OpenDocument("Untitled")
        _install_open_document(monkeypatch, [FakeOcc("A", full_path="A", bulb=True)], one)
        saved_for = _payload(iv.handler(action="snapshot"))["saved_for"]
        # switch to the OTHER unsaved 'Untitled' and hide something there
        b = FakeOcc("A", full_path="A", bulb=True)
        _install_open_document(monkeypatch, [b], two)
        b.isLightBulbOn = False
        res = iv.handler(action="restore")
        assert res["isError"] is True and "No snapshot saved" in res["message"]
        assert b.isLightBulbOn is False                    # doc one's state did NOT land here
        assert saved_for in iv._SNAPSHOTS                  # and doc one's snapshot survives

    def test_one_unsaved_document_keeps_its_key_across_calls(self, monkeypatch):
        # The other half of the same rule: the SAME document must still find its own snapshot
        # across two calls, even though each app.activeDocument read hands back a new wrapper.
        opened = OpenDocument("Untitled")
        a = FakeOcc("A", full_path="A", bulb=True)
        _install_open_document(monkeypatch, [a], opened)
        _payload(iv.handler(action="snapshot"))
        a.isLightBulbOn = False
        out = _payload(iv.handler(action="restore"))
        assert out["restored_occurrences"] == 1
        assert a.isLightBulbOn is True
        assert iv._SNAPSHOTS == {}                         # its own snapshot, cleanly consumed

    def test_a_snapshot_survives_the_save_that_re_keys_its_document(self, monkeypatch):
        # THE BITE: explore an unsaved document, then save it. The document key changes from the
        # minted token to the data-file id WITHOUT the document closing, so nothing evicts and
        # nothing else can reclaim what was parked - the restore misses honestly and the
        # pre-explore camera/visibility state is lost for the session. The shared rename
        # announcement carries it onto the key the document answers now.
        opened = OpenDocument("Untitled")
        a = FakeOcc("A", full_path="A", bulb=True)
        _install_open_document(monkeypatch, [a], opened)
        saved_for = _payload(iv.handler(action="snapshot"))["saved_for"]
        assert saved_for.startswith("unsaved:")
        a.isLightBulbOn = False                            # the exploring
        opened.data_file_id = "urn:lineage:saved"          # doc_save_as lands mid-session
        out = _payload(iv.handler(action="restore"))
        assert out["restored_occurrences"] == 1
        assert a.isLightBulbOn is True                     # the pre-explore state came back

    def test_the_carried_snapshot_MOVES_onto_the_new_key(self, monkeypatch):
        # Carried, not copied: a snapshot left behind under the superseded token is state no live
        # document keys to again, and a restore under the new key pops only one of the two.
        opened = OpenDocument("Untitled")
        _install_open_document(monkeypatch, [FakeOcc("A", full_path="A")], opened)
        old_key = _payload(iv.handler(action="snapshot"))["saved_for"]
        opened.data_file_id = "urn:lineage:saved"
        assert iv._doc_key() == "urn:lineage:saved"
        assert list(iv._SNAPSHOTS) == ["urn:lineage:saved"]
        assert old_key not in iv._SNAPSHOTS

    def test_the_snapshot_is_carried_again_on_the_second_key_flip(self, monkeypatch):
        # A save can re-key one document more than once - IF a path form answers before the lineage
        # urn (PROBE NEEDED, KEY-2). The mechanism is tested regardless of what triggers a second
        # flip: carrying only the FIRST one strands the snapshot one key later.
        opened = OpenDocument("Untitled")
        a = FakeOcc("A", full_path="A", bulb=True)
        _install_open_document(monkeypatch, [a], opened)
        _payload(iv.handler(action="snapshot"))
        opened.data_file_id = "a.b.c:/Projects/Plate.f3d"
        assert iv._doc_key() == "a.b.c:/Projects/Plate.f3d"
        opened.data_file_id = "urn:lineage:saved"
        a.isLightBulbOn = False
        out = _payload(iv.handler(action="restore"))
        assert out["restored_occurrences"] == 1 and a.isLightBulbOn is True

    def test_only_the_re_keyed_documents_snapshot_moves(self, monkeypatch):
        # The announcement is broadcast to every consumer, but it names ONE key: another open
        # document's snapshot must stay exactly where it is, or a save in one document silently
        # re-addresses another's saved state.
        one, two = OpenDocument("Untitled"), OpenDocument("Untitled")
        _install_open_document(monkeypatch, [FakeOcc("A", full_path="A")], one)
        one_key = _payload(iv.handler(action="snapshot"))["saved_for"]
        _install_open_document(monkeypatch, [FakeOcc("B", full_path="B")], two)
        two_key = _payload(iv.handler(action="snapshot"))["saved_for"]
        _install_open_document(monkeypatch, [FakeOcc("A", full_path="A")], one)
        one.data_file_id = "urn:lineage:saved"
        assert iv._doc_key() == "urn:lineage:saved"
        assert set(iv._SNAPSHOTS) == {"urn:lineage:saved", two_key}
        assert one_key not in iv._SNAPSHOTS

    def test_a_rename_for_a_key_holding_no_snapshot_stores_nothing(self, monkeypatch):
        # The listener fires for every consumer on every flip, including flips of documents this
        # store never saw. Writing an entry for one would make a later restore find a snapshot
        # that was never taken.
        iv._carry_snapshot("unsaved:99", "urn:lineage:elsewhere")
        assert iv._SNAPSHOTS == {}

    def test_a_data_file_whose_id_will_not_read_falls_back_to_an_instance_key(self, monkeypatch):
        # A dataFile that answers an EMPTY id is not an identity - keying on it would put every
        # such document in one bucket. It takes the per-instance key instead.
        blank_one, blank_two = OpenDocument("Shared", ""), OpenDocument("Shared", "")
        _install_open_document(monkeypatch, [FakeOcc("A", full_path="A")], blank_one)
        first = _payload(iv.handler(action="snapshot"))["saved_for"]
        _install_open_document(monkeypatch, [FakeOcc("B", full_path="B")], blank_two)
        second = _payload(iv.handler(action="snapshot"))["saved_for"]
        assert first != second and first != "" and second != ""
        assert len(iv._SNAPSHOTS) == 2

    def test_a_comparison_that_will_not_read_is_not_a_match(self, monkeypatch):
        # The safe() default. This wrapper's `==` raises - an ARBITRARY unreadable comparison,
        # standing for no particular platform state (a CLOSED document's wrapper answers False,
        # it does not raise). A comparison nobody could read is not evidence of a match, so the
        # live document mints its own key rather than inheriting a registered one's snapshot.
        unreadable = OpenDocument("Untitled", wrapper_class=_UnreadableComparisonWrapper)
        _install_open_document(monkeypatch, [FakeOcc("A", full_path="A")], unreadable)
        other_key = _payload(iv.handler(action="snapshot"))["saved_for"]
        live = OpenDocument("Untitled")
        _install_open_document(monkeypatch, [FakeOcc("B", full_path="B")], live)
        res = iv.handler(action="restore")
        assert res["isError"] is True and "No snapshot saved" in res["message"]
        assert other_key in iv._SNAPSHOTS

    def test_a_closed_document_is_pruned_and_its_snapshot_dropped(self, monkeypatch):
        # A closed document's snapshot is unreachable - no live document keys to it again - so
        # keeping it parks a Camera copy and a per-occurrence dict per scratch document for the
        # session. The next unsaved-key read evicts both halves.
        gone = OpenDocument("Untitled")
        _install_open_document(monkeypatch, [FakeOcc("A", full_path="A")], gone)
        dead_key = _payload(iv.handler(action="snapshot"))["saved_for"]
        assert dead_key in iv._SNAPSHOTS and len(iv._write_guard._UNSAVED_DOC_KEYS) == 1
        gone.close()                                       # the tab is closed
        live = OpenDocument("Untitled")
        _install_open_document(monkeypatch, [FakeOcc("B", full_path="B")], live)
        live_key = _payload(iv.handler(action="snapshot"))["saved_for"]
        assert dead_key not in iv._SNAPSHOTS               # the orphan snapshot is gone
        assert list(iv._SNAPSHOTS) == [live_key]
        assert [k for _d, k in iv._write_guard._UNSAVED_DOC_KEYS] == [live_key]

    def test_several_closed_documents_are_all_evicted_in_one_pass(self, monkeypatch):
        # The eviction walks the registry BACKWARDS so a deletion cannot slide the next entry past
        # the cursor, and so the index it holds stays inside a list that is shrinking under it. A
        # forward walk mis-handles both, and only a registry holding MORE THAN ONE dead entry can
        # tell the two walks apart - which is the shape a --keep-open session full of scratch
        # documents actually produces.
        first, second, live = (OpenDocument("Untitled"), OpenDocument("Untitled"),
                               OpenDocument("Untitled"))
        keys = []
        for opened, occ in ((first, "A"), (second, "B"), (live, "C")):
            _install_open_document(monkeypatch, [FakeOcc(occ, full_path=occ)], opened)
            keys.append(_payload(iv.handler(action="snapshot"))["saved_for"])
        assert len(iv._write_guard._UNSAVED_DOC_KEYS) == 3 and len(iv._SNAPSHOTS) == 3
        first.close()
        second.close()                                     # two dead entries, adjacent, at the front
        _install_open_document(monkeypatch, [FakeOcc("C", full_path="C")], live)
        assert iv._doc_key() == keys[2]                    # the survivor keeps its own key
        assert [k for _d, k in iv._write_guard._UNSAVED_DOC_KEYS] == [keys[2]]
        assert list(iv._SNAPSHOTS) == [keys[2]]

    def test_a_document_whose_validity_will_not_read_keeps_its_snapshot(self, monkeypatch):
        # The prune only acts on a DEFINITE False. An isValid that will not read says nothing
        # about the document, and evicting a live document's only saved state on it is the worse
        # error - so the snapshot stays and the document still finds it.
        class _MuteValidity(_DocWrapper):
            @property
            def isValid(self):
                raise RuntimeError("isValid could not be read")

        opened = OpenDocument("Untitled", wrapper_class=_MuteValidity)
        a = FakeOcc("A", full_path="A", bulb=True)
        _install_open_document(monkeypatch, [a], opened)
        key = _payload(iv.handler(action="snapshot"))["saved_for"]
        a.isLightBulbOn = False
        assert key in iv._SNAPSHOTS
        out = _payload(iv.handler(action="restore"))
        assert out["restored_occurrences"] == 1 and a.isLightBulbOn is True

    def test_the_refusal_names_the_document_not_the_session_token(self, monkeypatch):
        # WIRE: 'unsaved:N' is not a name, a URN or doc_get's 'open:N' - no tool accepts it and no
        # read reports it, so a refusal built on it hands the caller nothing to act on.
        _install_open_document(monkeypatch, [FakeOcc("A", full_path="A")],
                               OpenDocument("Fixture Plate"))
        res = iv.handler(action="restore")
        assert res["isError"] is True
        assert "No snapshot saved for 'Fixture Plate'" in res["message"]
        assert "unsaved:" not in res["message"]

    def test_the_snapshot_payload_names_the_document_beside_its_key(self, monkeypatch):
        # 'saved_for' is a session token for an unsaved document; alone it says nothing about
        # WHICH document the snapshot covers.
        _install_open_document(monkeypatch, [FakeOcc("A", full_path="A")],
                               OpenDocument("Fixture Plate"))
        out = _payload(iv.handler(action="snapshot"))
        assert out["document"] == "Fixture Plate"
        assert out["saved_for"].startswith("unsaved:")

    def test_no_active_document_keys_on_a_placeholder(self, monkeypatch):
        _install(monkeypatch, [FakeOcc("A", full_path="A")])
        monkeypatch.setattr(iv.app, "activeDocument", None)
        assert iv._doc_key() == "<active>"


# ── request tracer: a per-response 'request_echo' (monotonic seq + the args the handler received) so ──
# ── a REPLAYED response (an identical-replay failure) is diagnosable next time. ─────────────────────

class TestRequestTracer:
    def test_trace_advances_seq_and_echoes_received_args(self):
        t1 = iv._trace("orient", None, "front", "", "", "")
        t2 = iv._trace("hide", "Gear:1", "", "", "", "")
        assert t2["seq"] == t1["seq"] + 1                       # monotonic - a repeat means a replay
        assert t1["received"] == {"action": "orient", "orientation": "front"}
        assert t2["received"] == {"action": "hide", "target": "Gear:1"}

    def test_with_trace_injects_into_ok_payload(self):
        res = iv.ok({"action": "orient", "note": "aimed"})
        out = iv._with_trace(res, {"seq": 7, "received": {"action": "orient"}})
        payload = json.loads(out["content"][0]["text"])
        assert payload["request_echo"] == {"seq": 7, "received": {"action": "orient"}}
        assert payload["note"] == "aimed"                      # original payload preserved

    def test_with_trace_is_noop_on_error(self):
        err = iv.error("boom")
        out = iv._with_trace(err, {"seq": 1, "received": {}})
        assert out is err and out["isError"] is True

    def test_handler_stamps_the_echo(self, monkeypatch):
        # end-to-end: a successful view_set call carries request_echo reflecting its action
        monkeypatch.setattr(iv._common, "design", lambda: object())
        monkeypatch.setattr(iv, "_do_list_views", lambda design: iv.ok({"action": "list_views"}))
        out = json.loads(iv.handler(action="list_views")["content"][0]["text"])
        assert out["request_echo"]["received"]["action"] == "list_views"


# ── display: the non-body folder bulbs ───────────────────────────────────────

_FOLDER_ATTRS = ("isSketchFolderLightBulbOn", "isConstructionFolderLightBulbOn",
                 "isOriginFolderLightBulbOn", "isJointsFolderLightBulbOn")


def _lit_root(design, token="root-tok"):
    """Give the fake root the four folder bulbs (all on) + a token for the component walk."""
    r = design.rootComponent
    for attr in _FOLDER_ATTRS:
        setattr(r, attr, True)
    r.entityToken = token
    return r


# The x-ref shape for COMPONENTS, measured on a job assembled from several source documents: each
# document's ROOT component reads the SAME byte-identical entityToken while the documents' lineage
# ids differ. So a component's document is the half that tells two of them apart.
_SHARED_ROOT_TOKEN = "/v4BAAEAAwAAAAAAAAAAAAAA"
_URN_ONE = "urn:adsk.wipprod:dm.lineage:K3I2nkywRlaWPHJexysOdA"
_URN_TWO = "urn:adsk.wipprod:dm.lineage:N_QoPrrrSJmF__f9BZV86A"


def _doc_comp(name, urn, sketches=True):
    """A component in the document with lineage id `urn`, carrying the four folder bulbs. Both of
    these answer the SAME entityToken, which is what two documents' root components do; `sketches`
    sets the one bulb the pair is made to DISAGREE on."""
    c = MakeComp(name=name, entity_token=_SHARED_ROOT_TOKEN,
                 parent_design=make_source_document(urn))
    for attr in _FOLDER_ATTRS:
        setattr(c, attr, True)
    c.isSketchFolderLightBulbOn = sketches
    return c


def _folder_comps():
    """The colliding pair, both fully lit - for the walk-reaches-everything case."""
    return _doc_comp("StockDoc", _URN_ONE), _doc_comp("ViseDoc", _URN_TWO)


class TestDisplay:
    def test_hide_all_categories_sets_each_folder_bulb(self, monkeypatch):
        design = _install(monkeypatch)
        r = _lit_root(design)
        out = _payload(iv.handler(action="display", visible=False))
        assert out["folders_set"] == {"sketches": 1, "construction": 1, "origins": 1, "joints": 1}
        assert all(getattr(r, a) is False for a in _FOLDER_ATTRS)
        assert "Hidden" in out["note"]

    def test_scoped_categories_touch_only_their_folders(self, monkeypatch):
        design = _install(monkeypatch)
        r = _lit_root(design)
        out = _payload(iv.handler(action="display", visible=False, categories=["construction"]))
        assert out["folders_set"] == {"construction": 1}
        assert r.isConstructionFolderLightBulbOn is False
        assert r.isSketchFolderLightBulbOn is True          # untouched

    def test_an_already_matching_bulb_is_not_rewritten(self, monkeypatch):
        design = _install(monkeypatch)
        r = _lit_root(design)
        r.isSketchFolderLightBulbOn = False
        out = _payload(iv.handler(action="display", visible=False, categories=["sketches"]))
        assert out["folders_set"] == {"sketches": 0}         # nothing to write

    def test_the_walk_reaches_every_component_not_just_the_root(self, monkeypatch):
        # display() writes through _view_common's walk, so a walk that merged two components would
        # leave the second one's folders lit while the payload counted only the components it saw.
        a, b = _folder_comps()
        _install(monkeypatch, all_components=[a, b])
        out = _payload(iv.handler(action="display", visible=False))
        # 3 per category: the root component plus both of the colliding pair
        assert out["folders_set"] == {"sketches": 3, "construction": 3, "origins": 3, "joints": 3}
        assert out["components_walked"] == 3
        assert all(getattr(c, attr) is False for c in (a, b) for attr in _FOLDER_ATTRS)

    def test_the_folder_snapshot_round_trip_gives_each_component_ITS_OWN_bulbs_back(
            self, monkeypatch):
        # snapshot -> display(hide) -> restore, across two components whose document-local tokens
        # COLLIDE and whose starting bulbs DIFFER. Keyed on the bare token the snapshot holds one
        # entry (the last component walked wins) and restore writes that one component's state onto
        # both - so the component whose sketches were ON never gets them back, the write reads back
        # as the value it just wrote, and the restore reports full success. The wrong value is the
        # only observable, which is why it needs a differing pair to show up at all.
        lit = _doc_comp("StockDoc", _URN_ONE, sketches=True)
        dark = _doc_comp("ViseDoc", _URN_TWO, sketches=False)
        _install(monkeypatch, all_components=[lit, dark])

        _payload(iv.handler(action="snapshot"))
        _payload(iv.handler(action="display", visible=False))
        assert lit.isSketchFolderLightBulbOn is False        # the toggle really moved both
        assert dark.isConstructionFolderLightBulbOn is False

        out = _payload(iv.handler(action="restore"))
        assert lit.isSketchFolderLightBulbOn is True         # its OWN pre-snapshot state
        assert dark.isSketchFolderLightBulbOn is False       # and its own, which differed
        assert all(getattr(c, a) is True for c in (lit, dark)
                   for a in _FOLDER_ATTRS if a != "isSketchFolderLightBulbOn")
        # a wrong restore is silent - it reads back as whatever it wrote - so the payload claiming a
        # clean restore is exactly what a token-keyed collapse would also claim
        assert "failed_restores" not in out and out["snapshot_kept"] is False

    def test_a_component_whose_identity_does_not_read_is_left_alone(self, monkeypatch):
        # No identity means no key: storing such a component would put it under a key every other
        # unidentifiable component shares. It is skipped on both ends rather than restored wrong.
        anon = MakeComp(name="Anon")                          # no token, no parentDesign
        for attr in _FOLDER_ATTRS:
            setattr(anon, attr, True)
        _install(monkeypatch, all_components=[anon])
        _payload(iv.handler(action="snapshot"))
        _payload(iv.handler(action="display", visible=False))
        out = _payload(iv.handler(action="restore"))
        assert anon.isSketchFolderLightBulbOn is False        # never restored, never claimed
        assert "failed_restores" not in out

    def test_a_string_false_is_parsed_never_truthy(self, monkeypatch):
        # A permissive client delivers booleans as strings; bool('false') would SHOW instead.
        design = _install(monkeypatch)
        r = _lit_root(design)
        out = _payload(iv.handler(action="display", visible="false"))
        assert out["visible"] is False and r.isSketchFolderLightBulbOn is False

    def test_a_garbage_visible_string_is_refused(self, monkeypatch):
        _install(monkeypatch)
        res = iv.handler(action="display", visible="maybe")
        assert res["isError"] is True and "'visible' must be true or false" in res["message"]

    def test_json_string_categories_decode(self, monkeypatch):
        design = _install(monkeypatch)
        r = _lit_root(design)
        out = _payload(iv.handler(action="display", visible=False,
                                  categories='["construction", "sketches"]'))
        assert sorted(out["categories"]) == ["construction", "sketches"]
        assert r.isOriginFolderLightBulbOn is True           # unscoped category untouched

    def test_unknown_category_refused(self, monkeypatch):
        _install(monkeypatch)
        res = iv.handler(action="display", visible=False, categories=["decals"])
        assert res["isError"] is True and "decals" in res["message"]

    def test_missing_visible_refused(self, monkeypatch):
        _install(monkeypatch)
        res = iv.handler(action="display", categories=["sketches"])
        assert res["isError"] is True and "Provide 'visible'" in res["message"]

    def test_a_stuck_bulb_is_disclosed_not_silent(self, monkeypatch):
        design = _install(monkeypatch)
        r = _lit_root(design)

        class _StuckFolders:
            entityToken = "stuck-tok"
            name = "Stuck"
            isSketchFolderLightBulbOn = True     # class attr; instance writes land, but the
                                                 # property below swallows the one that matters

            def __setattr__(self, attr, value):
                if attr == "isSketchFolderLightBulbOn":
                    return                        # the platform's silent drop
                object.__setattr__(self, attr, value)

        stuck = _StuckFolders()
        for a in _FOLDER_ATTRS[1:]:
            setattr(stuck, a, True)
        monkeypatch.setattr(iv._view_common, "all_display_components",
                            lambda design: [r, stuck])
        out = _payload(iv.handler(action="display", visible=False, categories=["sketches"]))
        assert out["folders_set"] == {"sketches": 1}
        assert out["stuck"] == [{"component": "Stuck", "category": "sketches", "reads": True}]
        assert "did not land" in out["note"]

    def test_snapshot_restore_covers_the_folder_bulbs(self, monkeypatch):
        design = _install(monkeypatch)
        r = _lit_root(design)
        r.isConstructionFolderLightBulbOn = False            # a pre-existing hidden folder
        _payload(iv.handler(action="snapshot"))
        _payload(iv.handler(action="display", visible=True))  # shows construction too
        assert r.isConstructionFolderLightBulbOn is True
        _payload(iv.handler(action="restore"))
        assert r.isConstructionFolderLightBulbOn is False     # back to the snapshotted state
        assert r.isSketchFolderLightBulbOn is True
