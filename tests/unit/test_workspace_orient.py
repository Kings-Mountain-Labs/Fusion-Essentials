"""Unit tests for ``workspace_orient.py`` — the cold-boot orientation call.

This tool's WHOLE POINT is progressive disclosure: one cheap read that situates the agent + a
budget-aware 'pointers' block steering to TARGETED refinement instead of whole-design dumps. So the
tests pin: the content/health rollup is assembled correctly from the object model; CAM is detected
WITHOUT a CAM product being active; the depth-1 digest is capped (not the full tree); and — the
load-bearing behaviour — the pointers flip to 'scope it' guidance once the design crosses the size
thresholds. Plus the guards (no document; a non-Design document).

No live Fusion — fakes model exactly the read surface the handler touches.
"""

import json
import types

import adsk.cam
import adsk.core

from conftest import (
    Camera,
    FakeApplication,
    FakeDataFile,
    FakeDataFolder,
    FakeDataProject,
    FakeDocumentReference,
    FakeFusionDocument,
    FakeJoint as _SharedJoint,
    FakeOperation,
    FakeSetup as _SharedSetup,
    FakeProducts,
    FakeSelection,
    FakeSelections,
    FakeTimeline,
    FakeUserInterface,
    FakeUserParameter,
    FakeUserParameters,
    MakeComp,
    MakeDesign,
    Viewport,
    _NamedCollection,
    load_tool,
    make_bbox,
    make_cam,
    make_occurrence,
    strategy_factory,
)

wo = load_tool("workspace_orient")


# ── fakes: just the read surface workspace_orient touches ───────────────────────────────────────

class _Coll:
    def __init__(self, items):
        self._i = list(items)
    @property
    def count(self):
        return len(self._i)
    def item(self, i):
        return self._i[i]
    def __iter__(self):
        # Live adsk collections are iterable (allComponents is walked with list()); model that so a
        # design-wide walk over this collection behaves like the real API.
        return iter(self._i)


UNAVAILABLE = ("3 : The occurrence's referenced component is unavailable (broken or missing "
               "external reference).")


class BrokenOcc:
    """An occurrence whose referenced component will not load. Only `name` reads; component,
    fullPathName and childOccurrences all RAISE, isReferencedComponent reads FALSE (a live xref
    reads True) and isValid reads True - so occ.component raising is the only signal."""
    def __init__(self, name="45740"):
        self.name = name
        self.isReferencedComponent = False
        self.isValid = True

    @property
    def component(self):
        raise RuntimeError(UNAVAILABLE)

    @property
    def fullPathName(self):
        raise RuntimeError("2 : InternalValidationError : path.valid()")

    @property
    def childOccurrences(self):
        raise RuntimeError("2 : InternalValidationError : path.valid()")


def FakeOcc(name, comp=None, children=0, bodies=1, grounded=False, xref=False,
            broken_children=()):
    """One occurrence in the browser digest, on the shared occurrence fake.

    component.occurrences is the COMPONENT-LOCAL superset - the only collection an unresolved child
    appears in; childOccurrences (the assembly-context one) drops it."""
    component = MakeComp(name=comp or name.split(":")[0],
                         occurrences=list(broken_children) + [None] * children)
    return make_occurrence(path=name, component=component, children=[None] * children,
                           bodies=[None] * bodies, grounded=grounded, referenced=xref)


class FakeJoint(_SharedJoint):
    """The shared joint narrowed to the name and health an orientation rollup reads."""
    def __init__(self, name, health=0):
        super().__init__(name=name, health=health)


class FakeTL:
    def __init__(self, health):
        self.healthState = health


class FakeTimelineHealthOnly:
    """The shape an AsBuiltJoint and a RigidGroup share: the OBJECT carries no healthState and no
    errorOrWarningMessage attribute AT ALL (measured - AttributeError on both), so only its
    timelineObject says whether it computed. One class serves both walks because the two live
    classes are indistinguishable to this read.

    The absence is an object that genuinely lacks the attribute, never a member deleted off the
    shared adsk mock - that deletion does not reliably undo itself (tests/CLAUDE.md), and an ABSENT
    attribute is exactly what has to stay distinguishable from a state that read fine."""
    def __init__(self, name, tl_health=0):
        self.name = name
        self.timelineObject = FakeTL(tl_health)


class FakeNoHealthAnywhere:
    """A joint or relation NEITHER source answers for: no healthState of its own and no
    timelineObject either. The counterpart to FakeTimelineHealthOnly, whose timeline item does
    answer - together they separate 'could not read the state' from 'read it, it is fine'."""
    def __init__(self, name):
        self.name = name


# The read a design holding an unresolved reference refuses: the walk will not enumerate at all, so
# the census has to be rebuilt from component.occurrences.
_WALK_UNREADABLE = "2 : InternalValidationError : occ"


def _unreadable_walk():
    return _NamedCollection(raises=_WALK_UNREADABLE)


def FakeRoot(top_occs=(), all_count=None, joints=(), bodies=0, sketches=0, bbox=None,
             walk_raises=False, as_built=()):
    """The root component on the shared component fake: both occurrence walks, both joint
    collections (an as-built joint appears only in the second), and the body/sketch counts.
    `bbox` is ((minx,miny,minz),(maxx,maxy,maxz)) in cm, None a design with no geometry."""
    root = MakeComp(name="Root", occurrences=list(top_occs),
                    all_occurrences=[None] * (len(top_occs) if all_count is None else all_count),
                    joints=list(joints), as_built_joints=list(as_built),
                    bodies=[f"Body{i + 1}" for i in range(bodies)],
                    sketches=[None] * sketches)
    if walk_raises:
        root.allOccurrences = _unreadable_walk()
    root.boundingBox = make_bbox(*bbox) if bbox is not None else None
    return root


class _UnitsMgr:
    def __init__(self, units):
        self.defaultLengthUnits = units

    def convert(self, value, from_u, to_u):
        # the handler converts cm -> display units; mimic the common ones the tests use
        factor = {("cm", "mm"): 10.0, ("cm", "cm"): 1.0, ("cm", "in"): 1 / 2.54}.get((from_u, to_u), 1.0)
        return value * factor


def FakeSubComp(name, bodies=0, sketches=0):
    """A sub-component carrying its OWN sketches/bodies collections - the scope a design-wide count
    has to reach past the root for. Component carries no allComponents (that collection is a Design
    property), so a count that read it off here would raise rather than silently degrade."""
    return MakeComp(name=name, bodies=[f"{name}Body{i + 1}" for i in range(bodies)],
                    sketches=[None] * sketches)


def FakeDesign(root, timeline=(), units="mm", design_type=1, parameters=0, sub_components=(),
               marker=None):
    """The design behind the orientation read, on the shared design fake. `design_type` is 1
    parametric / 0 direct; a `marker` below the timeline count means the features after it are
    reverted. allComponents lives on the DESIGN (root included), as in the live API - never on a
    Component."""
    des = MakeDesign(comp=root, all_components=[root] + list(sub_components),
                     design_type=design_type,
                     timeline=FakeTimeline(list(timeline), marker=marker),
                     user_parameters=FakeUserParameters(
                         [FakeUserParameter(name=f"d{i}") for i in range(parameters)]))
    des.unitsManager = _UnitsMgr(units)
    return des


class FakeSetup(_SharedSetup):
    """The shared setup taking its operations positionally, as this file's rollups build it."""
    def __init__(self, ops, name="Setup1"):
        super().__init__(name, ops=ops)


class _OpFolder:
    """A CAMFolder: the direct-children collection the shared walk recurses through, which is the
    only place the folder objects exist - setup.allOperations flattens past them."""
    def __init__(self, name, ops):
        self.name = name
        self.operations = _Coll(ops)
        self.folders = _Coll([])
        self.patterns = _Coll([])


class _FoldersSetup:
    """A Setup whose operations are reachable BOTH ways: .operations + .folders (what the walk
    descends, keeping the containers) and .allOperations (the flat list the census counts against,
    holding the same operations with the folders dropped)."""
    def __init__(self, name, ops=(), folders=()):
        self.name = name
        self.operations = _Coll(list(ops))
        self.folders = _Coll(list(folders))
        self.patterns = _Coll([])
        nested = [o for f in folders for o in f.operations]
        self.allOperations = _Coll(list(ops) + nested)


class FakeOp:
    """A CAM Operation as the orientation read buckets it: the lifecycle state plus the toolpath
    pair that tells a generated-but-EMPTY op (valid, no toolpath) from one that never generated."""

    def __init__(self, has_toolpath, name="Op", toolpath_valid=True, suppressed=False, state=None,
                 errored=False):
        self.name = name
        self.hasToolpath = has_toolpath
        self.isToolpathValid = toolpath_valid
        self.isSuppressed = suppressed
        self.operationState = (adsk.cam.OperationStates.IsValidOperationState
                               if state is None else state)
        self.hasError = errored
        self.hasWarning = False
        self.isGenerating = False


def FakeCAM(setups, machining_times=None):
    """The CAM product carrying `setups`. The EMPTY class reads a second signal off it - an
    operation whose toolpath generated empty reads hasToolpath True and only its machining time
    answers - and that answer is the shared one, so this file cannot teach a different
    getMachiningTime contract than the other CAM tests read."""
    return make_cam(*setups, machining_times=machining_times)


def _ref(name, out_of_date=False):
    """A DocumentReference to the file called `name` - the external-component freshness signal."""
    return FakeDocumentReference(data_file=FakeDataFile(name=name), out_of_date=out_of_date)


def _data_file(urn="urn:adsk:lineage:abc", version=3, latest=3,
               url="https://x/g/data", folder="Parts", folder_id="fld.1",
               project="Sample Project", project_id="a.123", hub="Test Hub"):
    """A saved doc's data-model identity on the shared cloud fakes: URN + version + web URL +
    the parent folder/project chain. The hub is stamped on the project here - DataHub has no shape
    dump, so no shared fake stands for it."""
    owner = FakeDataProject(name=project, project_id=project_id)
    owner.parentHub = types.SimpleNamespace(name=hub)
    return FakeDataFile(file_id=urn, version=version, latest_version=latest, web_url=url,
                        parent_folder=FakeDataFolder(name=folder, folder_id=folder_id,
                                                     parent_project=owner),
                        parent_project=owner)


def _doc(name="Doc", design=None, cam=None, saved=True, modified=False, refs=(), data_file=None):
    """The active document as the orientation read touches it: its two products, its external
    references, and the DataFile only a saved one carries."""
    return FakeFusionDocument(name=name, design=design, data_file=data_file, is_saved=saved,
                              is_modified=modified, references=refs,
                              products=FakeProducts(design=design, cam=cam))


# The workspace the orientation read names. Workspace has no shape dump, so it stays bespoke.
_DESIGN_WORKSPACE = types.SimpleNamespace(name="Design")


def _install(active_product=None, doc=None, cam=None, design_for_cast=None,
             camera=None, selection=()):
    """Wire the module's app + adsk casts. active_product is what app.activeProduct returns (a design,
    a CAM product, or None); design_for_cast is what Design.cast resolves to (default: active_product
    if it's a design). camera/selection feed the new view + selection echo."""
    cam_obj = camera if camera is not None else Camera()
    ui = FakeUserInterface(FakeSelections([FakeSelection(entity=e) for e in selection]),
                           active_workspace=_DESIGN_WORKSPACE)
    wo.app = FakeApplication(active_document=doc, active_product=active_product,
                             user_interface=ui, version="TEST.0",
                             active_viewport=Viewport(camera=cam_obj))
    wo._common.app = wo.app

    import adsk.fusion, adsk.cam
    dcast = design_for_cast if design_for_cast is not None else (
        active_product if isinstance(active_product, MakeDesign) else None)
    adsk.fusion.Design.cast = lambda x: dcast if (x is active_product or x is None) else (
        x if isinstance(x, MakeDesign) else None)
    # get_cam reaches the product through the APP-level document, which is the bare adsk mock here -
    # so the cast answers only for the CAM product this document was built with, and None (no
    # Manufacture workspace) for the mock's stand-in.
    ccast = cam if cam is not None else (
        doc.products.itemByProductType("CAMProductType") if doc is not None else None)
    adsk.cam.CAM.cast = lambda x: ccast if (ccast is not None and x is ccast) else None


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


# ── guards ──────────────────────────────────────────────────────────────────────────────────────

class TestGuards:
    def test_no_active_document(self):
        _install(active_product=None, doc=None)
        res = wo.handler()
        assert res["isError"] is True and "No active document" in res["message"]

    def test_document_without_a_design(self):
        # a doc is open but no Design product (e.g. a drawing) -> has_design False, still reports doc/cam
        doc = _doc(name="Drawing1", design=None, cam=None)
        _install(active_product=None, doc=doc, design_for_cast=None)
        out = _payload(wo.handler())
        assert out["has_design"] is False
        assert out["document"]["name"] == "Drawing1"
        assert out["has_cam"] is False
        assert "no Design product" in out["note"]


# ── the orientation report ───────────────────────────────────────────────────────────────────────

class TestOrientation:
    def _small_design(self, **kw):
        occs = [FakeOcc("Wheel:1", bodies=1, grounded=False),
                FakeOcc("Fork:1", bodies=1, grounded=True)]
        root = FakeRoot(top_occs=occs, all_count=2,
                        joints=[FakeJoint("Wheel_Spin")], bodies=0, sketches=3)
        return FakeDesign(root, timeline=[FakeTL(0), FakeTL(0)], **kw)

    def test_reports_the_running_fusion_build(self):
        # the build number an agent quotes when an API behaves differently than a doc says; it is
        # read off the session, never a constant, so a dropped read must not pass as one.
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        assert _payload(wo.handler())["fusion_version"] == "TEST.0"

    def test_reports_document_and_design_identity(self):
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["has_design"] is True
        assert out["design"]["mode"] == "parametric"     # designType 1
        assert out["design"]["units"] == "mm"
        assert out["design"]["sketches"] == 3
        assert out["design"]["top_level_occurrences"] == 2

    def test_reports_parameters_count_and_pointer(self):
        # parameters were invisible in the front-door orient - a design WITH them must report the count
        # AND a param_get breadcrumb (the same inbound crumb design_get now gives).
        des = self._small_design(parameters=18)
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["design"]["parameters"] == 18
        assert "param_get" in out["pointers"]["parameters"]

    def test_no_param_pointer_when_zero(self):
        des = self._small_design(parameters=0)
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["design"]["parameters"] == 0            # count still reported (like bodies/sketches)
        assert "parameters" not in out["pointers"]         # but no pointer when there's nothing to point at

    def test_healthy_rollup(self):
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        h = _payload(wo.handler())["health"]
        assert h["is_healthy"] is True
        assert h["timeline_errors"] == 0 and h["broken_joints"] == []
        assert h["joint_count"] == 1
        assert h["grounded_occurrences"] == 1            # Fork is grounded

    def test_timeline_errors_make_it_unhealthy(self):
        occs = [FakeOcc("A:1")]
        root = FakeRoot(top_occs=occs, joints=[])
        des = FakeDesign(root, timeline=[FakeTL(0), FakeTL(2), FakeTL(1), FakeTL(3)])
        _install(active_product=des, doc=_doc(design=des))
        h = _payload(wo.handler())["health"]
        assert h["timeline_errors"] == 1 and h["timeline_warnings"] == 1 and h["timeline_suppressed"] == 1
        assert h["is_healthy"] is False

    def test_broken_joint_surfaced_by_name(self):
        # healthState 2 = error (a real compute failure) -> flagged as failed-to-compute.
        root = FakeRoot(top_occs=[FakeOcc("A:1")],
                        joints=[FakeJoint("Good", 0), FakeJoint("PistonSlide", 2)])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["health"]["broken_joints"] == ["PistonSlide"]
        assert out["health"]["is_healthy"] is False
        # the note leads with the facts (not an "unhealthy" verdict) so a skimming agent sees what
        # was found without being told a deliberate config is broken.
        assert out["note"].startswith("Attention")
        assert "failed to compute" in out["note"]

    def test_suppressed_joint_is_not_broken(self):
        # healthState 3 = SUPPRESSED (author-parked alternate, e.g. a fixture template's reversed jaw).
        # It must NOT count as broken and must NOT drop is_healthy. (Live: 'Jaw ... REVERSED' hs=3.)
        root = FakeRoot(top_occs=[FakeOcc("A:1")],
                        joints=[FakeJoint("Active", 0), FakeJoint("Parked REVERSED", 3)])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["health"]["broken_joints"] == []
        assert out["health"]["is_healthy"] is True
        assert out["note"].startswith("No compute errors")

    def test_healthy_note_says_so(self):
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        assert _payload(wo.handler())["note"].startswith("No compute errors")

    def test_direct_mode_has_no_timeline(self):
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[], design_type=0)   # direct
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["design"]["mode"] == "direct"
        assert out["health"]["timeline_features"] == 0

    def test_browser_digest_is_depth_one(self):
        occs = [FakeOcc("Asm:1", children=12, bodies=0, xref=True),
                FakeOcc("Plate:1", children=0, bodies=2, grounded=True)]
        root = FakeRoot(top_occs=occs)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        digest = _payload(wo.handler())["browser_digest"]
        assert len(digest) == 2
        asm = next(d for d in digest if d["name"] == "Asm:1")
        assert asm["children"] == 12 and asm["is_xref"] is True
        plate = next(d for d in digest if d["name"] == "Plate:1")
        assert plate["bodies"] == 2 and plate["grounded"] is True

    def test_digest_capped_for_wide_assemblies(self):
        occs = [FakeOcc(f"P{i}:1") for i in range(40)]
        root = FakeRoot(top_occs=occs, all_count=40)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert len(out["browser_digest"]) == wo._DIGEST_LIMIT      # capped, not all 40
        assert out["design"]["top_level_occurrences"] == 40        # but the true count is reported


# ── timeline markers (null health) + rolled-back marker + warnings surfaced distinctly ────────────

class TestTimelineHonesty:
    def _des(self, timeline, marker=None):
        root = FakeRoot(top_occs=[FakeOcc("A:1")], joints=[])
        return FakeDesign(root, timeline=timeline, marker=marker)

    def test_null_health_marker_counted_distinctly(self):
        # a Snapshot reads NULL health (neither healthy nor error) - counted as a marker, not silently
        # folded into an implied 'healthy'. Flip the elif off and timeline_markers goes to 0 (red).
        des = self._des([FakeTL(0), FakeTL(None), FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        h = _payload(wo.handler())["health"]
        assert h["timeline_markers"] == 1
        assert h["timeline_errors"] == 0 and h["timeline_warnings"] == 0
        assert h["is_healthy"] is True                  # a marker alone is not unhealthy

    def test_rolled_back_marker_is_unhealthy_and_surfaced(self):
        des = self._des([FakeTL(0), FakeTL(0), FakeTL(0)], marker=1)   # marker at 1 of 3 = rolled back
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["health"]["timeline_rolled_back"] is True
        assert out["health"]["is_healthy"] is False
        assert "rolled back" in out["note"]
        assert "fix_health" in out["pointers"] and "rolled-back" in out["pointers"]["fix_health"]

    def test_marker_at_end_is_not_rolled_back(self):
        des = self._des([FakeTL(0), FakeTL(0)], marker=2)             # marker at the end
        _install(active_product=des, doc=_doc(design=des))
        assert _payload(wo.handler())["health"]["timeline_rolled_back"] is False

    def test_warning_surfaced_distinctly_even_when_otherwise_healthy(self):
        # errors 0 -> is_healthy stays True, but a timeline WARNING must be STATED in the note, never
        # folded into a clean 'no problems'.
        des = self._des([FakeTL(0), FakeTL(1)])                       # one warning, no error
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["health"]["timeline_warnings"] == 1
        assert out["health"]["is_healthy"] is True
        assert "WARNING" in out["note"]


# ── design-wide counts: sketches + bodies span sub-components, not just root (F25) ────────────────

class TestDesignWideCounts:
    """The sketch/body counts in the design summary must be DESIGN-WIDE (every component), not
    root/active-scoped. Live regression: a gimbal doc whose 3 sketches all live in sub-components,
    with root active, reported 'sketches: 0' - misleading a cold agent about whether geometry exists.
    """

    def test_sketches_in_sub_components_are_counted_with_empty_root(self):
        # root has ZERO sketches; the geometry lives in three sub-components. A root-only count reports
        # 0; the design-wide walk must report the true 3 (this is the exact F25 shape).
        root = FakeRoot(top_occs=[FakeOcc("Frame:1"), FakeOcc("OuterRing:1"), FakeOcc("InnerRing:1")],
                        all_count=3, sketches=0, bodies=0)
        subs = [FakeSubComp("Frame", sketches=1), FakeSubComp("OuterRing", sketches=1),
                FakeSubComp("InnerRing", sketches=1)]
        des = FakeDesign(root, timeline=[FakeTL(0)], sub_components=subs)
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["design"]["sketches"] == 3      # NOT 0 - would be 0 under a root-only count

    def test_bodies_summed_across_root_and_sub_components(self):
        # root holds 1 body; two sub-components hold 2 and 3 - total 6 across the design.
        root = FakeRoot(top_occs=[FakeOcc("A:1"), FakeOcc("B:1")], all_count=2, bodies=1, sketches=2)
        subs = [FakeSubComp("A", bodies=2, sketches=1), FakeSubComp("B", bodies=3, sketches=0)]
        des = FakeDesign(root, timeline=[FakeTL(0)], sub_components=subs)
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["design"]["bodies"] == 6        # 1 + 2 + 3, NOT the root-only 1
        assert out["design"]["sketches"] == 3      # 2 + 1 + 0, NOT the root-only 2

    def test_single_component_design_matches_root(self):
        # No sub-components: design-wide == root-only, so the common single-part case is unchanged.
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=1, bodies=4, sketches=5)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["design"]["bodies"] == 4 and out["design"]["sketches"] == 5


# ── CAM detection (without switching to Manufacture) ─────────────────────────────────────────────

class TestCam:
    def test_no_cam(self):
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des, cam=None))
        out = _payload(wo.handler())
        assert out["has_cam"] is False and "cam" not in out

    _STATES = adsk.cam.OperationStates

    def _orient(self, monkeypatch, ops, machining_times=None):
        return self._orient_setups(monkeypatch, [FakeSetup(ops)], machining_times)

    def _orient_setups(self, monkeypatch, setups, machining_times=None):
        cam = FakeCAM(setups, machining_times)
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des, cam=cam))
        monkeypatch.setattr(wo._cam_common, "get_cam", lambda: (cam, None))
        return _payload(wo.handler())

    def test_cam_present_with_ungenerated_ops(self, monkeypatch):
        out = self._orient(monkeypatch, [
            FakeOp(True),
            FakeOp(False, toolpath_valid=False, state=self._STATES.NoToolpathOperationState),
            FakeOp(False, toolpath_valid=False, state=self._STATES.IsInvalidOperationState)])
        assert out["has_cam"] is True
        assert out["cam"]["setups"] == 1 and out["cam"]["total_operations"] == 3
        assert out["cam"]["ungenerated_operations"] == 2
        assert "cam" in out["pointers"] and "need generating" in out["pointers"]["cam"]

    def test_suppressed_and_empty_ops_are_not_counted_as_ungenerated(self, monkeypatch):
        # the CAM-11 arithmetic: a parked op and a generated-but-empty one both read hasToolpath
        # false, and counting that flag alone reported the pair as needing generation.
        suppressed = [FakeOp(False, name=f"Parked{i}", toolpath_valid=False, suppressed=True,
                             state=self._STATES.SuppressedOperationState) for i in range(65)]
        empty = [FakeOp(False, name=f"Empty{i}") for i in range(13)]
        out = self._orient(monkeypatch, suppressed + empty + [FakeOp(True, name="Cut")])
        cam = out["cam"]
        assert cam["total_operations"] == 79
        assert cam["ungenerated_operations"] == 0        # NOT 78
        assert cam["suppressed_operations"] == 65
        assert cam["empty_toolpath_operations"] == 13

    def test_an_op_whose_toolpath_generated_empty_is_counted_and_named(self, monkeypatch):
        # the shape the flags read as a clean valid: hasToolpath TRUE, state IsValid, and 0.0 s of
        # machining time. Without the time signal this operation lands in no count at all and the
        # pointer reports the job as generated.
        out = self._orient(monkeypatch, [FakeOp(True, name="Swarf1"), FakeOp(True, name="Cut")],
                           machining_times={"Swarf1": 0.0, "Cut": 4.193083})
        cam = out["cam"]
        assert cam["total_operations"] == 2
        assert cam["empty_toolpath_operations"] == 1
        assert cam["empty_toolpaths"] == ["Swarf1"]
        assert "Swarf1" in out["pointers"]["cam"] and "Cut" not in out["pointers"]["cam"]

    def test_an_operation_whose_time_did_not_read_stays_out_of_the_empty_count(self, monkeypatch):
        # the whole op set holds a toolpath and NO time answers (every call raises) - an unread
        # signal is not a measurement of zero, so nothing is named empty
        out = self._orient(monkeypatch, [FakeOp(True, name="Swarf1"), FakeOp(True, name="Cut")])
        cam = out["cam"]
        assert cam["empty_toolpath_operations"] == 0 and "empty_toolpaths" not in cam
        assert cam["ungenerated_operations"] == 0

    def test_the_two_empty_shapes_are_counted_together(self, monkeypatch):
        # one operation of each shape - hasToolpath False (the flags) and hasToolpath True with
        # 0.0 s (the time) - land in ONE count, and the cutting operation beside them does not
        out = self._orient(monkeypatch, [FakeOp(False, name="NoPath"), FakeOp(True, name="Swarf1"),
                                         FakeOp(True, name="Cut")],
                           machining_times={"Swarf1": 0.0, "Cut": 9.66})
        cam = out["cam"]
        assert cam["empty_toolpath_operations"] == 2
        assert sorted(cam["empty_toolpaths"]) == ["NoPath", "Swarf1"]

    def test_an_op_whose_state_did_not_read_is_not_named_an_empty_toolpath(self, monkeypatch):
        # both the count and the named row gate on _cam_common.is_empty_toolpath, and this op's
        # operationState RAISED - it has no lifecycle to publish, so the row that says it cut
        # nothing would be built on a state nothing read. It still counts in the census. The unread
        # op carries the EMPTY class's own toolpath pair (isToolpathValid True, hasToolpath False),
        # so those flags alone would name it.
        out = self._orient(monkeypatch, [FakeOperation("Unread", has_toolpath=False,
                                                       state_readable=False),
                                         FakeOp(False, name="Empty1")])
        cam = out["cam"]
        assert cam["total_operations"] == 2
        assert cam["empty_toolpath_operations"] == 1
        assert cam["empty_toolpaths"] == ["Empty1"]
        # and it is not silently absorbed into the census either - it gets the bucket its state
        # earned, so the counts above cannot read as a job whose every op answered.
        assert cam["unread_state_operations"] == 1

    def test_a_job_whose_states_all_read_carries_no_unread_bucket(self, monkeypatch):
        # the other side: a zero here on every healthy job would read as a checked-and-clean claim
        out = self._orient(monkeypatch, [FakeOp(True, name="Cut")], machining_times={"Cut": 9.66})
        assert "unread_state_operations" not in out["cam"]

    def test_the_pointer_names_the_empty_toolpaths(self, monkeypatch):
        out = self._orient(monkeypatch, [FakeOp(True, name="Cut"),
                                         FakeOp(False, name="Rest Finishing")])
        pointer = out["pointers"]["cam"]
        assert "need generating" not in pointer
        assert "EMPTY" in pointer and "Rest Finishing" in pointer

    def test_the_empty_name_list_is_capped_while_the_count_is_not(self, monkeypatch):
        out = self._orient(monkeypatch,
                           [FakeOp(False, name=f"Empty{i}") for i in range(wo._EMPTY_NAME_CAP + 4)])
        cam = out["cam"]
        assert cam["empty_toolpath_operations"] == wo._EMPTY_NAME_CAP + 4
        assert len(cam["empty_toolpaths"]) == wo._EMPTY_NAME_CAP
        # the sentence must not read as if it named them all, and must not run an ellipsis into
        # its own full stop ('....', which is what the first live read printed)
        pointer = out["pointers"]["cam"]
        assert f"(first {wo._EMPTY_NAME_CAP} of {wo._EMPTY_NAME_CAP + 4})" in pointer
        assert "...." not in pointer

    def test_a_complete_empty_list_is_not_marked_as_more(self, monkeypatch):
        out = self._orient(monkeypatch, [FakeOp(False, name="Empty1")])
        assert "..." not in out["pointers"]["cam"]

    def test_an_empty_name_two_setups_share_is_told_apart_by_its_setup(self, monkeypatch):
        # This list is DOCUMENT-scoped and an operation name is unique only within a setup, so the
        # bare name printed twice addresses two operations and separates neither.
        out = self._orient_setups(monkeypatch, [
            FakeSetup([FakeOp(False, name="Rest Finishing")], name="Front"),
            FakeSetup([FakeOp(False, name="Rest Finishing")], name="Back")])
        assert out["cam"]["empty_toolpaths"] == ["Front / Rest Finishing",
                                                 "Back / Rest Finishing"]
        # the pointer renders the same discriminated rows, not the shared name twice
        assert "Front / Rest Finishing, Back / Rest Finishing" in out["pointers"]["cam"]

    def test_a_name_only_one_setup_carries_stays_the_bare_name(self, monkeypatch):
        # The discriminator separates nothing already separate, and the bare name is what a caller
        # passes to cam_get/cam_generate - so a distinct name crosses the wire unchanged.
        out = self._orient_setups(monkeypatch, [
            FakeSetup([FakeOp(False, name="Bore")], name="Front"),
            FakeSetup([FakeOp(False, name="Face")], name="Back")])
        assert out["cam"]["empty_toolpaths"] == ["Bore", "Face"]

    def test_two_empty_operations_of_one_name_in_ONE_setup_are_told_apart_by_position(
            self, monkeypatch):
        # The pair the setup path alone cannot separate: same name, same container. The walk
        # position is what this read holds over them, so neither row repeats the other - a repeated
        # string here addresses two operations and cam_get refuses it as ambiguous.
        out = self._orient_setups(monkeypatch, [
            FakeSetup([FakeOp(False, name="Bore"), FakeOp(False, name="Bore")], name="Front")])
        listed = out["cam"]["empty_toolpaths"]
        assert listed == ["Front / Bore (operation 1)", "Front / Bore (operation 2)"]
        assert len(set(listed)) == len(listed)

    def test_two_empty_namesakes_in_DIFFERENT_folders_are_told_apart_by_the_folder(
            self, monkeypatch):
        # The folder-inclusive breadcrumb the container-preserving walk builds separates the pair
        # the flattened setup path could not - and it is read, not counted, so no position is spent.
        out = self._orient_setups(monkeypatch, [
            _FoldersSetup("Front", folders=[_OpFolder("Rough", [FakeOp(False, name="Bore")]),
                                            _OpFolder("Finish", [FakeOp(False, name="Bore")])])])
        assert out["cam"]["empty_toolpaths"] == ["Front / Rough / Bore", "Front / Finish / Bore"]
        assert out["cam"]["empty_toolpath_operations"] == 2

    def test_a_folder_whose_name_does_not_read_keeps_the_operation_name(self, monkeypatch):
        # The walk writes whatever each level answered into the path, so a folder whose name did not
        # read takes a segment of it anyway and the row would be named after a container nothing
        # read. A row is named by its whole chain or by its own name - never by a breadcrumb with a
        # hole in the middle. The sibling in the same list still gets its own, which is what shows
        # the decision is per row.
        class _BlindNameFolder:
            def __init__(self, ops):
                self.operations = _Coll(ops)
                self.folders = _Coll([])
                self.patterns = _Coll([])

            @property
            def name(self):
                raise RuntimeError("folder name unreadable")

        out = self._orient_setups(monkeypatch, [
            _FoldersSetup("Front", folders=[_OpFolder("Rough", [FakeOp(False, name="Bore")]),
                                            _BlindNameFolder([FakeOp(False, name="Bore")])])])
        listed = out["cam"]["empty_toolpaths"]
        assert listed == ["Front / Rough / Bore", "Bore"]
        assert not any("None" in n for n in listed)

    def test_a_folder_LITERALLY_named_None_still_yields_a_whole_address(self, monkeypatch):
        # THE GUARD beside the test above: the degrade is decided on the READ - each level's own
        # name through the walk's parent links - never on a string match against the joined path.
        # A folder really named 'None' answered, so its row keeps the whole address the walk built
        # and is NOT degraded to the bare operation name its unreadable sibling falls back to.
        out = self._orient_setups(monkeypatch, [
            _FoldersSetup("Front", folders=[_OpFolder("None", [FakeOp(False, name="Bore")]),
                                            _OpFolder("Rough", [FakeOp(False, name="Bore")])])])
        assert out["cam"]["empty_toolpaths"] == ["Front / None / Bore", "Front / Rough / Bore"]

    def test_the_position_is_the_documents_walk_order_not_the_setups(self, monkeypatch):
        # The position has to be unique across the whole list, which is document-scoped: two setups
        # each holding their own same-named pair number 1,2 and 3,4 - a per-setup count would print
        # '(operation 1)' twice and separate neither pair.
        pair = lambda: [FakeOp(False, name="Bore"), FakeOp(False, name="Bore")]
        out = self._orient_setups(monkeypatch, [FakeSetup(pair(), name="Front"),
                                                FakeSetup(pair(), name="Front")])
        listed = out["cam"]["empty_toolpaths"]
        assert listed == ["Front / Bore (operation 1)", "Front / Bore (operation 2)",
                          "Front / Bore (operation 3)", "Front / Bore (operation 4)"]

    def test_a_folder_nested_operation_is_named_by_the_folder_it_sits_in(self, monkeypatch):
        # allOperations FLATTENS folder-nested operations and drops the folders, so a read walking
        # it names this pair 'Front / Bore' twice; the container-preserving walk names the nested
        # one by the folder it actually sits in.
        out = self._orient_setups(monkeypatch, [
            _FoldersSetup("Front", ops=[FakeOp(False, name="Bore")],
                          folders=[_OpFolder("Rough", [FakeOp(False, name="Bore")])])])
        assert out["cam"]["total_operations"] == 2
        assert out["cam"]["empty_toolpaths"] == ["Front / Bore", "Front / Rough / Bore"]

    def test_the_substitution_is_judged_over_every_empty_operation_not_the_capped_head(
            self, monkeypatch):
        # The cap is applied AFTER the substitution: a listed name whose namesake falls outside the
        # cap is still replaced, or the visible list would print an address that reaches two
        # operations while looking unique.
        fillers = [FakeOp(False, name=f"Empty{i}") for i in range(wo._EMPTY_NAME_CAP)]
        out = self._orient_setups(monkeypatch, [
            FakeSetup([FakeOp(False, name="Dup")] + fillers, name="Front"),
            FakeSetup([FakeOp(False, name="Dup")], name="Back")])
        listed = out["cam"]["empty_toolpaths"]
        assert len(listed) == wo._EMPTY_NAME_CAP
        assert listed[0] == "Front / Dup"          # its namesake was cut, the row is still told apart
        assert out["cam"]["empty_toolpath_operations"] == wo._EMPTY_NAME_CAP + 2

    def test_a_setup_whose_name_does_not_read_keeps_the_operation_name(self, monkeypatch):
        # An empty discriminator is not a label: told_apart keeps the plain name, so a setup whose
        # name RAISES never renders as half of an address. The sibling in the same list still gets
        # its own, which is what shows the row-by-row decision.
        class _BlindNameSetup:
            def __init__(self, ops):
                self.allOperations = _Coll(ops)

            @property
            def name(self):
                raise RuntimeError("setup name unreadable")

        out = self._orient_setups(monkeypatch, [
            _BlindNameSetup([FakeOp(False, name="Rest Finishing")]),
            FakeSetup([FakeOp(False, name="Rest Finishing")], name="Back")])
        assert out["cam"]["empty_toolpaths"] == ["Rest Finishing", "Back / Rest Finishing"]

    def test_two_namesakes_under_an_unread_setup_keep_their_names_not_a_bare_position(
            self, monkeypatch):
        # A blank breadcrumb is not an address, so it is never dressed up with a position: the
        # position qualifies a path, and there is no path here. Spending one anyway ships rows
        # carrying NO operation name at all - ' (operation 1)' - which names nothing and reaches
        # the wire beside a count that says two operations are empty. A repeated plain name is the
        # honest rendering: told_apart's own rule, and what a caller can still pass to cam_get.
        class _BlindNameSetup:
            def __init__(self, ops):
                self.allOperations = _Coll(ops)

            @property
            def name(self):
                raise RuntimeError("setup name unreadable")

        out = self._orient_setups(monkeypatch, [
            _BlindNameSetup([FakeOp(False, name="Bore"), FakeOp(False, name="Bore")])])
        listed = out["cam"]["empty_toolpaths"]
        assert listed == ["Bore", "Bore"]
        assert all(n.strip() for n in listed)
        assert out["cam"]["empty_toolpath_operations"] == 2

    def test_a_folder_whose_name_reads_EMPTY_also_withholds_the_whole_path(self, monkeypatch):
        # The withhold covers two DIFFERENT reads: a level that answered nothing, and one that
        # answered an empty name. The second puts a blank segment in the middle - 'Front /  / Bore'
        # - which is as much an address to nothing as the first, so the row degrades to its own
        # name here too. The readable sibling still gets its whole chain.
        out = self._orient_setups(monkeypatch, [
            _FoldersSetup("Front", folders=[_OpFolder("", [FakeOp(False, name="Bore")]),
                                            _OpFolder("Rough", [FakeOp(False, name="Bore")])])])
        assert out["cam"]["empty_toolpaths"] == ["Bore", "Front / Rough / Bore"]

    def test_a_fully_generated_job_reports_generated(self, monkeypatch):
        out = self._orient(monkeypatch, [FakeOp(True), FakeOp(True)])
        assert out["cam"]["ungenerated_operations"] == 0
        assert "toolpaths look generated" in out["pointers"]["cam"]
        assert "empty_toolpaths" not in out["cam"]

    def test_a_job_whose_only_anomaly_is_an_unread_state_is_not_a_clean_bill(self, monkeypatch):
        # nothing else fires for this job - no error, nothing ungenerated, nothing empty - so the
        # pointer would report it generated while one operation answered no state at all.
        out = self._orient(monkeypatch, [FakeOp(True, name="Cut"),
                                         FakeOperation("Unread", state_readable=False)])
        assert out["cam"]["unread_state_operations"] == 1
        pointer = out["pointers"]["cam"]
        assert "toolpaths look generated" not in pointer
        assert "census is incomplete" in pointer

    def test_the_pointer_names_the_parked_operations_beside_the_verdict(self, monkeypatch):
        parked = FakeOp(False, name="Parked", toolpath_valid=False, suppressed=True,
                        state=self._STATES.SuppressedOperationState)
        out = self._orient(monkeypatch, [FakeOp(True, name="Cut"), parked])
        assert "toolpaths look generated." in out["pointers"]["cam"]
        assert "1 suppressed." in out["pointers"]["cam"]

    def test_the_pointer_falls_back_when_the_summary_could_not_be_built(self):
        # has_cam true with no summary: say the neutral thing, never invent a count
        assert wo._cam_pointer(None) == "toolpaths look generated."

    def test_an_errored_op_is_its_own_count_not_a_clean_bill(self, monkeypatch):
        # an errored op is neither suppressed nor ungenerated nor empty - without its own bucket it
        # would fall through every branch and the pointer would report the job as generated.
        out = self._orient(monkeypatch, [FakeOp(True, name="Cut"),
                                         FakeOp(False, name="Broken", toolpath_valid=False,
                                                errored=True)])
        cam = out["cam"]
        assert cam["errored_operations"] == 1
        assert cam["ungenerated_operations"] == 0      # generating again will not clear an error
        assert cam["empty_toolpath_operations"] == 0
        assert "ERRORS" in out["pointers"]["cam"] and "1 operation(s)" in out["pointers"]["cam"]

    def test_errors_are_named_before_ungenerated_work(self, monkeypatch):
        out = self._orient(monkeypatch, [
            FakeOp(False, name="Broken", toolpath_valid=False, errored=True),
            FakeOp(False, name="Stale", toolpath_valid=False,
                   state=self._STATES.IsInvalidOperationState)])
        assert out["cam"]["errored_operations"] == 1
        assert out["cam"]["ungenerated_operations"] == 1
        assert "ERRORS" in out["pointers"]["cam"]        # the blocker leads, not the stale count

    def test_every_operation_lands_in_exactly_one_bucket(self, monkeypatch):
        out = self._orient(monkeypatch, [
            FakeOp(True, name="Cut"),
            FakeOp(False, name="Empty"),
            FakeOp(False, name="Stale", toolpath_valid=False,
                   state=self._STATES.IsInvalidOperationState),
            FakeOp(False, name="Parked", toolpath_valid=False, suppressed=True,
                   state=self._STATES.SuppressedOperationState),
            FakeOp(False, name="Broken", toolpath_valid=False, errored=True)])
        cam = out["cam"]
        counted = (cam["ungenerated_operations"] + cam["errored_operations"]
                   + cam["suppressed_operations"] + cam["empty_toolpath_operations"])
        assert cam["total_operations"] == 5
        assert counted == 4                    # the one op holding a real toolpath is in none
        assert "operations_unread" not in cam

    def test_an_operation_that_does_not_read_is_disclosed_not_counted_away(self, monkeypatch):
        # iter_collection SKIPS an item(i) that raises, so the walk comes back short; the setup's
        # own count is the witness that the census is incomplete.
        class _ShortCollection:
            def __init__(self, items):
                self._i = list(items)

            @property
            def count(self):
                return len(self._i) + 2      # the setup declares two more than item() will hand over

            def item(self, i):
                if i >= len(self._i):
                    raise RuntimeError("operation cannot be read")
                return self._i[i]

        cam = FakeCAM([type("S", (), {"allOperations": _ShortCollection([FakeOp(True)])})()])
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des, cam=cam))
        monkeypatch.setattr(wo._cam_common, "get_cam", lambda: (cam, None))
        out = _payload(wo.handler())
        assert out["cam"]["total_operations"] == 1
        assert out["cam"]["operations_unread"] == 2
        assert "incomplete" in out["pointers"]["cam"]     # never a clean bill on a short census

    def test_an_unreadable_operation_count_is_disclosed_as_unknown(self, monkeypatch):
        class _NoCount:
            @property
            def count(self):
                raise RuntimeError("count cannot be read")

            def item(self, i):
                raise RuntimeError("item cannot be read")

        cam = FakeCAM([type("S", (), {"allOperations": _NoCount()})()])
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des, cam=cam))
        monkeypatch.setattr(wo._cam_common, "get_cam", lambda: (cam, None))
        out = _payload(wo.handler())
        assert out["cam"]["setups_with_unreadable_operation_count"] == 1
        assert "operations_unread" not in out["cam"]      # unknown is not a number
        assert "incomplete" in out["pointers"]["cam"]

    def test_a_short_census_is_disclosed_beside_an_error_verdict(self, monkeypatch):
        # the blocker leads, but a job whose census came back short must not read as a complete
        # count of its errors either
        class _ShortCollection:
            def __init__(self, items):
                self._i = list(items)

            @property
            def count(self):
                return len(self._i) + 3

            def item(self, i):
                if i >= len(self._i):
                    raise RuntimeError("operation cannot be read")
                return self._i[i]

        broken = FakeOp(False, name="Broken", toolpath_valid=False, errored=True)
        cam = FakeCAM([type("S", (), {"allOperations": _ShortCollection([broken])})()])
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des, cam=cam))
        monkeypatch.setattr(wo._cam_common, "get_cam", lambda: (cam, None))
        out = _payload(wo.handler())
        assert out["cam"]["errored_operations"] == 1 and out["cam"]["operations_unread"] == 3
        pointer = out["pointers"]["cam"]
        assert "ERRORS" in pointer and "incomplete" in pointer

    def test_a_short_census_is_disclosed_even_when_there_is_work_to_do(self, monkeypatch):
        # the incompleteness rides on every verdict, not only the clean-looking one
        class _ShortCollection:
            def __init__(self, items):
                self._i = list(items)

            @property
            def count(self):
                return len(self._i) + 1

            def item(self, i):
                if i >= len(self._i):
                    raise RuntimeError("operation cannot be read")
                return self._i[i]

        stale = FakeOp(False, name="Stale", toolpath_valid=False,
                       state=self._STATES.IsInvalidOperationState)
        cam = FakeCAM([type("S", (), {"allOperations": _ShortCollection([stale])})()])
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des, cam=cam))
        monkeypatch.setattr(wo._cam_common, "get_cam", lambda: (cam, None))
        pointer = _payload(wo.handler())["pointers"]["cam"]
        assert "1 operation(s) need generating." in pointer and "incomplete" in pointer


# ── external-reference (OOD) health — for ANY doc with xrefs, not just templates ─────────────────

class TestExternalReferences:
    def _design_with_refs(self, refs):
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        doc = _doc(design=des, refs=refs)
        _install(active_product=des, doc=doc)
        return des

    def test_no_references_is_clean(self):
        self._design_with_refs([])
        out = _payload(wo.handler())
        # the key names its noun: referenced DOCUMENTS, which is a different count from
        # doc_get(xref_tree).reference_link_count (reference LINKS).
        assert out["references"]["referenced_documents"] == 0
        assert out["references"]["out_of_date"] == []
        assert out["health"]["is_healthy"] is True
        assert "fix_references" not in out["pointers"]

    def test_references_all_current_is_healthy(self):
        self._design_with_refs([_ref("PartA"), _ref("PartB")])
        out = _payload(wo.handler())
        assert out["references"]["referenced_documents"] == 2
        assert out["references"]["out_of_date"] == []
        assert out["health"]["is_healthy"] is True
        assert "fix_references" not in out["pointers"]

    def test_out_of_date_reference_is_flagged_for_attention(self):
        self._design_with_refs([_ref("Fresh"), _ref("StalePart", out_of_date=True)])
        out = _payload(wo.handler())
        assert out["references"]["out_of_date"] == ["StalePart"]
        assert out["health"]["out_of_date_references"] == ["StalePart"]
        assert out["health"]["is_healthy"] is False              # OOD still counts against health
        assert "fix_references" in out["pointers"]
        assert "doc_update_xref" in out["pointers"]["fix_references"]
        assert "StalePart" in out["pointers"]["fix_references"]
        # the note states the facts and flags that they may be intentional, rather than a bare verdict
        assert out["note"].startswith("Attention")
        assert "out-of-date reference" in out["note"]
        assert "intentional" in out["note"]                      # tells the agent to confirm, not assume

    def test_a_healthy_design_publishes_no_unresolved_marker(self):
        # the 0-broken boundary: the list is empty, is_healthy stays true, and the note keeps its
        # clean verdict - no unresolved wording appears on a document that has none.
        self._design_with_refs([_ref("PartA")])
        out = _payload(wo.handler())
        assert out["health"]["unresolved_references"] == []
        assert out["health"]["is_healthy"] is True
        assert "UNRESOLVED" not in out["note"]
        assert "unresolved_references" not in out["pointers"]
        assert out["design"]["occurrences_walk"] == "allOccurrences"

    def test_one_unresolved_reference_makes_the_document_unhealthy_and_is_NAMED(self):
        # the specimen: is_healthy read TRUE beside a note declaring the document clean while an
        # occurrence's referenced component would not load.
        container = FakeOcc("Op1 Workholding Container:1", broken_children=[BrokenOcc("45740")])
        root = FakeRoot(top_occs=[container], walk_raises=True)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        h = out["health"]
        assert h["is_healthy"] is False
        assert [u["name"] for u in h["unresolved_references"]] == ["45740"]
        assert h["unresolved_references"][0]["parent_path"] == "Op1 Workholding Container:1"
        assert UNAVAILABLE in h["unresolved_references"][0]["detail"]
        assert "45740" in out["note"] and "UNRESOLVED" in out["note"]
        assert "No compute errors" not in out["note"]
        assert "45740" in out["pointers"]["unresolved_references"]

    def test_the_note_never_promises_a_source_file_or_hub(self):
        # the API exposes NO path from a broken occurrence to its document/project/hub, so the note
        # must not send the agent after one.
        container = FakeOcc("Op1 Workholding Container:1", broken_children=[BrokenOcc("45740")])
        des = FakeDesign(FakeRoot(top_occs=[container], walk_raises=True), timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        note = _payload(wo.handler())["note"]
        assert "doc_update_xref" not in note        # cannot refresh a reference with no DocumentReference
        assert "switch" not in note.lower()          # no hub advice is buildable
        assert "browser tree" in note

    def test_a_raising_walk_reports_the_recursed_marker_and_an_honest_total(self):
        # the blast radius: total_occurrences read 0 beside top_level_occurrences 5.
        container = FakeOcc("Op1:1", broken_children=[BrokenOcc("45740")])
        des = FakeDesign(FakeRoot(top_occs=[container, FakeOcc("Stock:1")], walk_raises=True),
                         timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["design"]["occurrences_walk"] == "recursed"
        assert out["design"]["total_occurrences"] == 3      # 2 readable + the unresolved one
        assert "recursed" in out["note"]

    def test_an_unreadable_census_publishes_null_not_zero(self):
        # neither walk enumerated: the count is UNKNOWN. A 0 here is a read failure dressed as a fact.
        root = FakeRoot(walk_raises=True)
        root.occurrences = _unreadable_walk()
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["design"]["total_occurrences"] is None
        assert out["design"]["occurrences_walk"] == "unreadable"
        assert "unknown rather than zero" in out["note"]

    def test_a_broken_TOP_LEVEL_occurrence_gets_a_digest_row_instead_of_a_blank_one(self):
        # a row built from swallowed reads would show it as an ordinary empty component - which is
        # exactly how it stayed invisible.
        des = FakeDesign(FakeRoot(top_occs=[BrokenOcc("45740"), FakeOcc("Stock:1")],
                                  walk_raises=True), timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        rows = {r["name"]: r for r in _payload(wo.handler())["browser_digest"]}
        assert rows["45740"]["unresolved"] is True
        assert "bodies" not in rows["45740"]           # nothing readable is claimed about it
        assert rows["Stock:1"].get("unresolved") is None

    def test_a_container_row_counts_its_unresolved_descendants(self):
        container = FakeOcc("Op1:1", children=2, broken_children=[BrokenOcc("45740")])
        des = FakeDesign(FakeRoot(top_occs=[container], walk_raises=True), timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        row = _payload(wo.handler())["browser_digest"][0]
        assert row["children"] == 2                  # childOccurrences, which DROPS the broken one
        assert row["unresolved_descendants"] == 1     # so the subtree count is published beside it

    def test_the_digest_names_its_own_depth_rather_than_overstating_is_xref(self):
        # is_xref reads false on every row of a document whose references sit deeper; the note says
        # which depth the flag describes instead of leaving the agent to conclude "no references".
        self._design_with_refs([_ref("PartA")])
        note = _payload(wo.handler())["note"]
        assert "browser_digest is DEPTH-1" in note
        assert "referenced_documents" in note and "reference_link_count" in note

    def test_ood_reported_even_without_an_active_design(self):
        # a non-Design doc (e.g. a drawing) that still has stale xrefs must surface them
        doc = _doc(name="Drawing1", design=None, cam=None,
                      refs=[_ref("StaleXref", out_of_date=True)])
        _install(active_product=None, doc=doc, design_for_cast=None)
        out = _payload(wo.handler())
        assert out["has_design"] is False
        assert out["references"]["out_of_date"] == ["StaleXref"]
        assert "out-of-date reference" in out["note"]


# ── POINTERS: the progressive-disclosure heart ───────────────────────────────────────────────────

class TestPointers:
    def test_small_design_points_to_whole_tree(self):
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=3, bodies=5)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        p = _payload(wo.handler())["pointers"]
        assert "whole assembly in one call" in p["assembly_structure"]
        assert "find_geometry" in p["geometry"]

    def test_large_assembly_steers_to_scoped_tree(self):
        # > _BIG_OCCURRENCES occurrences -> the pointer must say to scope to a component, not dump all
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=wo._BIG_OCCURRENCES + 5)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert "scope to a component" in out["pointers"]["assembly_structure"]
        assert "LARGE" in out["note"]

    def test_many_bodies_steers_geometry_to_target(self):
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=3, bodies=wo._BIG_BODIES + 1)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        p = _payload(wo.handler())["pointers"]
        assert "always scope by target" in p["geometry"]

    def test_broken_health_adds_fix_pointer(self):
        root = FakeRoot(top_occs=[FakeOcc("A:1")], joints=[FakeJoint("J", 2)])
        des = FakeDesign(root, timeline=[FakeTL(2)])
        _install(active_product=des, doc=_doc(design=des))
        p = _payload(wo.handler())["pointers"]
        assert "fix_health" in p and "design_recompute" in p["fix_health"]

    def test_kinematics_pointer_only_when_joints_or_grounding(self):
        # no joints, nothing grounded -> no kinematics pointer (don't suggest probing an empty thing)
        root = FakeRoot(top_occs=[FakeOcc("A:1", grounded=False)], joints=[])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        p = _payload(wo.handler())["pointers"]
        assert "kinematics" not in p

    def test_the_guidance_pointer_ships_on_every_design_and_names_the_recipe_index(self):
        # design practice is not tied to what this document happens to contain, so it is the one
        # pointer that does not gate on a count - and it points at the INDEX, not a flood.
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=3, bodies=5)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        pointer = _payload(wo.handler())["pointers"]["guidance"]
        assert pointer.startswith("sys_get_guidance()")
        assert "recipes" in pointer

    def test_the_guidance_pointer_is_dropped_where_that_tool_is_not_registered(self, monkeypatch):
        # the pointer follows the '<tool_name>(...)' shape exactly so _drop_unregistered_pointers
        # can see which tool it names; written any other way it would survive as a dead pointer.
        monkeypatch.setattr(wo.registry, "get_tools", lambda: {"design_get": object()})
        monkeypatch.setattr(wo.registry, "has_tool", lambda name: name != "sys_get_guidance")
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=3, bodies=5)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        assert "guidance" not in _payload(wo.handler())["pointers"]


# ── data-model identity (where the doc lives: hub/project/folder + URN) ───────────────────────────

class TestDataModel:
    def test_saved_doc_reports_full_location_and_urn(self):
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=1)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        df = _data_file(urn="urn:adsk:lineage:xyz", version=4, latest=5,
                        folder="Rovers", project="Sample Project", project_id="a.999",
                        hub="Test Hub")
        _install(active_product=des, doc=_doc(design=des, data_file=df))
        dm = _payload(wo.handler())["document"]["data_model"]
        assert dm["saved_to_cloud"] is True
        assert dm["document_id"] == "urn:adsk:lineage:xyz"
        assert dm["version_number"] == 4 and dm["latest_version_number"] == 5
        assert dm["hub"] == "Test Hub"
        assert dm["project"] == "Sample Project" and dm["project_id"] == "a.999"
        assert dm["folder"] == "Rovers"

    def test_version_numbers_say_which_handle_they_were_read_off(self):
        # Both numbers come off the ONE DataFile handle the open document HOLDS, and that handle
        # keeps its pre-save values - so after a save they can disagree with each other (v3 beside
        # latest 2 was observed). The note states that, and names the post-save read to trust
        # instead - it must NOT promise the numbers merely lag by a few seconds.
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=1)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des, data_file=_data_file(version=3, latest=2)))
        dm = _payload(wo.handler())["document"]["data_model"]
        note = dm["version_lag_note"]
        assert dm["version_number"] == 3 and dm["latest_version_number"] == 2
        assert "HOLDS" in note and "pre-save" in note
        assert "fresh data_get" in note and "version_confirmed is true only" in note
        assert "false/pending is unknown" in note
        assert "few seconds" not in note

    def test_unsaved_doc_has_no_version_note_to_warn_about(self):
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=1)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des, data_file=None))
        dm = _payload(wo.handler())["document"]["data_model"]
        assert "version_lag_note" not in dm

    def test_unsaved_doc_has_no_urn_and_note_warns(self):
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=1)
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des, data_file=None))  # never saved
        out = _payload(wo.handler())
        dm = out["document"]["data_model"]
        assert dm["saved_to_cloud"] is False
        assert dm["document_id"] is None and dm["project"] is None and dm["hub"] is None
        assert "UNSAVED" in out["note"]

    def test_data_model_present_even_without_a_design(self):
        # a drawing (no Design) that IS saved still reports its data-model location
        df = _data_file(project="Badass Pen", folder="Drawings")
        doc = _doc(name="Sheet1", design=None, cam=None, data_file=df)
        _install(active_product=None, doc=doc, design_for_cast=None)
        dm = _payload(wo.handler())["document"]["data_model"]
        assert dm["saved_to_cloud"] is True
        assert dm["project"] == "Badass Pen" and dm["folder"] == "Drawings"


# ── overall bbox + camera view + selection echo ──────────────────────────────────────────────────

class TestBbox:
    def test_bbox_reported_in_display_units(self):
        # 0..5 cm box, units mm -> size 50 mm, center 25 mm (convert cm->mm = x10)
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=1, bbox=((0, 0, 0), (5, 5, 5)))
        des = FakeDesign(root, timeline=[FakeTL(0)], units="mm")
        _install(active_product=des, doc=_doc(design=des))
        bb = _payload(wo.handler())["design"]["overall_bbox"]
        assert bb["units"] == "mm"
        assert bb["size"] == {"x": 50.0, "y": 50.0, "z": 50.0}
        assert bb["center"] == {"x": 25.0, "y": 25.0, "z": 25.0}

    def test_bbox_none_when_no_geometry(self):
        root = FakeRoot(top_occs=[], all_count=0, bbox=None)   # empty/sketch-only design
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des))
        assert _payload(wo.handler())["design"]["overall_bbox"] is None

    def test_a_failed_conversion_reports_null_not_raw_centimetres(self):
        # The payload labels these numbers with the design's display unit. Falling back to the
        # UNCONVERTED cm value publishes 5 as "50 mm" - a wrong measurement wearing the right label.
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=1, bbox=((0, 0, 0), (5, 5, 5)))
        des = FakeDesign(root, timeline=[FakeTL(0)], units="mm")

        def _boom(value, from_u, to_u):
            raise RuntimeError("units manager unavailable")
        des.unitsManager.convert = _boom
        _install(active_product=des, doc=_doc(design=des))
        bb = _payload(wo.handler())["design"]["overall_bbox"]
        assert bb["units"] == "mm"
        assert bb["size"] == {"x": None, "y": None, "z": None}
        assert bb["center"] == {"x": None, "y": None, "z": None}
        assert bb["unreadable_values"] is True
        assert "null" in bb["note"]

    def test_an_unreadable_coordinate_reports_null_not_zero(self):
        # An unreadable max.z defaulted to 0.0 published a Z size of "0 mm" - a design that is flat,
        # which is an ANSWER, not a missing read. Only that axis goes null; x/y still report.
        class _NoZ:
            x = 5.0
            y = 5.0
            @property
            def z(self):
                raise RuntimeError("unreadable")
        root = FakeRoot(top_occs=[FakeOcc("A:1")], all_count=1, bbox=((0, 0, 0), (5, 5, 5)))
        root.boundingBox.maxPoint = _NoZ()
        des = FakeDesign(root, timeline=[FakeTL(0)], units="mm")
        _install(active_product=des, doc=_doc(design=des))
        bb = _payload(wo.handler())["design"]["overall_bbox"]
        assert bb["size"] == {"x": 50.0, "y": 50.0, "z": None}
        assert bb["center"]["z"] is None and bb["center"]["x"] == 25.0
        assert bb["unreadable_values"] is True


class TestViewState:
    def test_orthographic_camera(self):
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des),
                 camera=Camera(camera_type=adsk.core.CameraTypes.OrthographicCameraType,
                               eye=(10, 0, 0), target=(0, 0, 0)))
        v = _payload(wo.handler())["view"]
        assert v["projection"] == "orthographic"
        assert v["eye"] == {"x": 10.0, "y": 0.0, "z": 0.0}
        assert v["target"] == {"x": 0.0, "y": 0.0, "z": 0.0}

    def test_perspective_camera(self):
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des),
                 camera=Camera(camera_type=adsk.core.CameraTypes.PerspectiveCameraType))
        assert _payload(wo.handler())["view"]["projection"] == "perspective"

    def test_an_unreadable_eye_component_reports_a_null_point_not_a_zero_axis(self):
        # (10, 0.0, 0) with the 0.0 standing in for an unreadable Y is a DIFFERENT world position
        # from the one the camera holds - null says the point is unknown, which is the truth.
        class _BadPt:
            x = 10.0
            z = 4.0
            @property
            def y(self):
                raise RuntimeError("unreadable")
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        camera = Camera(camera_type=adsk.core.CameraTypes.OrthographicCameraType,
                        eye=(10, 0, 0), target=(1, 2, 3))
        camera.eye = _BadPt()
        _install(active_product=des, doc=_doc(design=des), camera=camera)
        v = _payload(wo.handler())["view"]
        assert v["eye"] is None
        assert v["target"] == {"x": 1.0, "y": 2.0, "z": 3.0}     # the readable point still reports


class TestSelectionEcho:
    def test_no_selection_is_empty(self):
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des), selection=())
        out = _payload(wo.handler())
        assert out["selection"] == {"count": 0, "selected": []}
        assert "selection" not in out["pointers"]      # no pointer when nothing selected

    def test_selected_body_echoed_with_pointer(self):
        body = type("BRepBody", (), {"name": "Body1"})()
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des), selection=[body])
        out = _payload(wo.handler())
        assert out["selection"]["count"] == 1
        rec = out["selection"]["selected"][0]
        assert rec["kind"] == "body" and rec["name"] == "Body1"
        # a pointer to sys_get_selection (the deep read) appears when something is selected
        assert "selection" in out["pointers"] and "sys_get_selection" in out["pointers"]["selection"]

    def test_selected_face_reports_body_and_occurrence(self):
        face = type("BRepFace", (), {
            "body": type("B", (), {"name": "Plate"})(),
            "assemblyContext": type("O", (), {"fullPathName": "Sub:1+Plate:1"})(),
        })()
        root = FakeRoot(top_occs=[FakeOcc("A:1")])
        des = FakeDesign(root, timeline=[FakeTL(0)])
        _install(active_product=des, doc=_doc(design=des), selection=[face])
        rec = _payload(wo.handler())["selection"]["selected"][0]
        assert rec["kind"] == "face" and rec["body"] == "Plate"
        assert rec["occurrence"] == "Sub:1+Plate:1"


class TestRelationHealthInFirstCall:
    def _design_with_constraint(self, health):
        occs = [FakeOcc("A:1")]
        root = FakeRoot(top_occs=occs, joints=[])
        con = type("C", (), {"name": "Constraint 1", "healthState": health})()
        root.assemblyConstraints = _Coll([con])
        return FakeDesign(root, timeline=[FakeTL(0)])

    def test_failed_constraint_drops_the_first_call_health(self):
        # Measured: a failed assembly constraint left this read healthy while only a deeper
        # include=['relations'] slice named it - the orientation read folds relation health in.
        des = self._design_with_constraint(health=2)
        _install(active_product=des, doc=_doc(design=des))
        h = _payload(wo.handler())["health"]
        assert h["is_healthy"] is False
        assert h["broken_relations"] == ["Constraint 1"]

    def test_healthy_constraint_leaves_the_rollup_alone(self):
        des = self._design_with_constraint(health=0)
        _install(active_product=des, doc=_doc(design=des))
        h = _payload(wo.handler())["health"]
        assert h["is_healthy"] is True and h["broken_relations"] == []

    def test_a_broken_relation_alone_contradicts_neither_the_verdict_nor_the_pointers(self):
        # is_healthy counts broken_relations, so the verdict sentence and the pointers must too:
        # otherwise the one fault in the design reads "No compute errors ..." beside is_healthy
        # false, with nothing naming the tool that repairs it.
        des = self._design_with_constraint(health=2)
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["health"]["is_healthy"] is False
        assert "No compute errors" not in out["note"]
        assert "1 assembly relation(s) failed to compute" in out["note"]
        assert "Constraint 1" in out["pointers"]["fix_relations"]
        assert out["pointers"]["fix_relations"].startswith("assembly_get(")

    def test_a_healthy_design_keeps_the_clean_verdict_and_no_relation_pointer(self):
        des = self._design_with_constraint(health=0)
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert "No compute errors" in out["note"]
        assert "fix_relations" not in out["pointers"]


class TestHealthReadOffTheTimelineItem:
    """An entity carrying no healthState of its own is read through its timelineObject, so the
    orientation rollup reaches the same verdict assembly_get does on one design. Live: an as-built
    joint whose geometry body was deleted reported healthState 1 on its timeline item while the
    joint object itself raised AttributeError."""

    def _design(self, joints=(), as_built=(), relations=(), kind="rigidGroups"):
        root = FakeRoot(top_occs=[FakeOcc("A:1")], joints=joints, as_built=as_built)
        if relations:
            setattr(root, kind, _Coll(relations))
        return FakeDesign(root, timeline=[FakeTL(0)])

    def test_as_built_joint_broken_on_its_timeline_item_is_named(self):
        # tl healthState 1 = WARNING - the exact state a live as-built joint reported after its
        # geometry body was deleted, and the state the joint object itself cannot answer.
        des = self._design(as_built=[FakeTimelineHealthOnly("ArmSpin", tl_health=1)])
        _install(active_product=des, doc=_doc(design=des))
        h = _payload(wo.handler())["health"]
        assert h["broken_joints"] == ["ArmSpin"]
        assert h["is_healthy"] is False
        assert h["joint_count"] == 1
        assert "joints_health_unknown" not in h

    def test_as_built_joint_errored_on_its_timeline_item_is_named(self):
        # the other failing state (2 = ERROR); both sides of the warning/error pair must count.
        des = self._design(as_built=[FakeTimelineHealthOnly("ArmSpin", tl_health=2)])
        _install(active_product=des, doc=_doc(design=des))
        assert _payload(wo.handler())["health"]["broken_joints"] == ["ArmSpin"]

    def test_as_built_joint_healthy_on_its_timeline_item_is_not_named_or_unknown(self):
        # healthState 0 = HEALTHY: the boundary just below the failing pair. Counted healthy, so
        # neither broken nor withheld.
        des = self._design(as_built=[FakeTimelineHealthOnly("ArmSpin", tl_health=0)])
        _install(active_product=des, doc=_doc(design=des))
        h = _payload(wo.handler())["health"]
        assert h["broken_joints"] == [] and h["is_healthy"] is True
        assert "joints_health_unknown" not in h

    def test_suppressed_as_built_joint_is_neither_broken_nor_unknown(self):
        # healthState 3 = SUPPRESSED: the boundary just above the failing pair. A state that READ
        # and is deliberately not flagged - so it must not land in the withheld count either, which
        # is what separates 'parked on purpose' from 'could not be read'.
        des = self._design(as_built=[FakeTimelineHealthOnly("Parked REVERSED", tl_health=3)])
        _install(active_product=des, doc=_doc(design=des))
        h = _payload(wo.handler())["health"]
        assert h["broken_joints"] == [] and h["is_healthy"] is True
        assert "joints_health_unknown" not in h

    def test_joint_no_source_answers_is_counted_unknown_not_broken_and_not_healthy(self):
        des = self._design(as_built=[FakeNoHealthAnywhere("Mystery")])
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        h = out["health"]
        assert h["joints_health_unknown"] == 1
        assert h["broken_joints"] == []          # a withheld state is not a fault
        assert h["is_healthy"] is True           # ... and is_healthy makes no claim about it
        assert "1 joint(s) (joints_health_unknown)" in out["note"]
        assert "counted neither broken nor healthy" in out["note"]

    def test_a_joint_that_answers_directly_still_counts_as_before(self):
        # the plain Joint path is untouched: its own healthState answers and the timeline item is
        # never needed.
        des = self._design(joints=[FakeJoint("Good", 0), FakeJoint("PistonSlide", 2)])
        _install(active_product=des, doc=_doc(design=des))
        h = _payload(wo.handler())["health"]
        assert h["broken_joints"] == ["PistonSlide"] and "joints_health_unknown" not in h

    def test_rigid_group_broken_on_its_timeline_item_is_named(self):
        des = self._design(relations=[FakeTimelineHealthOnly("Rigid Group 1", tl_health=2)])
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["health"]["broken_relations"] == ["Rigid Group 1"]
        assert out["health"]["is_healthy"] is False
        assert "Rigid Group 1" in out["pointers"]["fix_relations"]

    def test_healthy_rigid_group_is_neither_broken_nor_unknown(self):
        des = self._design(relations=[FakeTimelineHealthOnly("Rigid Group 1", tl_health=0)])
        _install(active_product=des, doc=_doc(design=des))
        h = _payload(wo.handler())["health"]
        assert h["broken_relations"] == [] and h["is_healthy"] is True
        assert "relations_health_unknown" not in h

    def test_relation_no_source_answers_is_counted_unknown_not_broken(self):
        des = self._design(relations=[FakeNoHealthAnywhere("Rigid Group 1")])
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        assert out["health"]["relations_health_unknown"] == 1
        assert out["health"]["broken_relations"] == []
        assert out["health"]["is_healthy"] is True
        assert "1 assembly relation(s) (relations_health_unknown)" in out["note"]

    def test_both_withheld_counts_are_reported_together(self):
        des = self._design(as_built=[FakeNoHealthAnywhere("Mystery")],
                           relations=[FakeNoHealthAnywhere("Rigid Group 1")])
        _install(active_product=des, doc=_doc(design=des))
        out = _payload(wo.handler())
        h = out["health"]
        assert h["joints_health_unknown"] == 1 and h["relations_health_unknown"] == 1
        assert ("1 joint(s) (joints_health_unknown) and 1 assembly relation(s) "
                "(relations_health_unknown) published NO compute state") in out["note"]
        # a withheld state is not a fault, so the clean verdict still leads
        assert out["note"].startswith("No compute errors")


# ── entitlement / capability block (LICENSE-CTX-1) ───────────────────────────────────────────────
#
# The block probes a SMALL sentinel set of strategy names document-independently through
# adsk.cam.OperationStrategy.createFromString(name).isGenerationAllowed and reports the OBSERVED
# generation flag per strategy - never a claimed license name/tier/SKU (Fusion exposes no license
# API). The flag is read through _cam_common.strategy_generation_allowed - the one seam cam_generate's
# launch pre-flight also excludes on - so these tests fake that module's _create_strategy to return
# allowed / blocked / unreadable / raising, and one guards the sentinel vocabulary offline.

class TestCapabilityBlock:
    def _small_design(self):
        return FakeDesign(FakeRoot(top_occs=[FakeOcc("A:1")], all_count=1), timeline=[FakeTL(0)])

    def _all(self, value):
        return {name: value for name in wo._CAPABILITY_SENTINELS}

    def test_every_sentinel_reads_true_when_generation_is_allowed(self, monkeypatch):
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        monkeypatch.setattr(wo._cam_common, "_create_strategy", strategy_factory(self._all(True)))
        og = _payload(wo.handler())["machining_capabilities"]["observed_generation"]
        assert og == {name: True for name in wo._CAPABILITY_SENTINELS}

    def test_blocked_strategies_read_false_not_null(self, monkeypatch):
        # false is an OBSERVED answer (the base license declines it), distinct from an unread probe's
        # null - so a blocked strategy must publish False, never fold into the unknown case.
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        monkeypatch.setattr(wo._cam_common, "_create_strategy", strategy_factory(self._all(False)))
        og = _payload(wo.handler())["machining_capabilities"]["observed_generation"]
        assert all(v is False for v in og.values())
        assert not any(v is None for v in og.values())

    def test_an_unknown_or_renamed_strategy_reads_null_and_does_not_crash_the_orient(self, monkeypatch):
        # createFromString RAISES '3 : Unknown strategy' for a renamed sentinel; the probe must
        # degrade it to null and the orient must still return ok, with the readable sentinels intact.
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        table = self._all(True)
        table.pop("probe_geometry")
        monkeypatch.setattr(wo._cam_common, "_create_strategy", strategy_factory(table))
        res = wo.handler()
        assert res["isError"] is False
        og = _payload(res)["machining_capabilities"]["observed_generation"]
        assert og["probe_geometry"] is None
        assert og["steep_and_shallow"] is True

    def test_an_unreadable_flag_reads_null_not_false(self, monkeypatch):
        # the strategy built but isGenerationAllowed itself would not read: unknown, never a confident
        # False that an agent would read as 'this capability is blocked'.
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        table = self._all(True)
        table["swarf"] = None
        monkeypatch.setattr(wo._cam_common, "_create_strategy", strategy_factory(table))
        og = _payload(wo.handler())["machining_capabilities"]["observed_generation"]
        assert og["swarf"] is None
        assert og["multiaxis_finishing"] is True

    def test_the_block_keys_are_exactly_the_sentinels(self, monkeypatch):
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        monkeypatch.setattr(wo._cam_common, "_create_strategy", strategy_factory(self._all(True)))
        og = _payload(wo.handler())["machining_capabilities"]["observed_generation"]
        assert set(og) == set(wo._CAPABILITY_SENTINELS)
        assert len(og) == 4                         # compact - a few keys, not the 54-row dump

    def test_the_note_cites_the_flag_and_asserts_no_license_tier(self, monkeypatch):
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        monkeypatch.setattr(wo._cam_common, "_create_strategy", strategy_factory(self._all(True)))
        note = _payload(wo.handler())["machining_capabilities"]["note"]
        assert "isGenerationAllowed" in note                       # cited to the read that backs it
        assert "cam_get(include=['strategies'])" in note           # pointer kept: empty test registry
        assert "No license tier or SKU is asserted" in note        # the honesty line
        assert "you have" not in note.lower()                      # never claims a named license/tier
        # the flag is an ENTITLEMENT read, not a generation promise - the note must not say a
        # true-reading strategy 'generates' (tool/geometry/machine can still fail it).
        assert "true = it generates" not in note
        assert "INSTALLATION, not the open document" in note

    def test_the_cam_get_pointer_is_dropped_when_the_cam_family_is_gated_off(self, monkeypatch):
        # cam is a gateable family: on a server with cam_get unregistered the note must not name a
        # tool that 404s. A NON-empty registry without cam_get is that server; the empty registry
        # (this test context, patched non-empty here) keeps the pointer via the escape hatch.
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        monkeypatch.setattr(wo._cam_common, "_create_strategy", strategy_factory(self._all(True)))
        monkeypatch.setattr(wo.registry, "get_tools", lambda: {"workspace_orient": object()})
        monkeypatch.setattr(wo.registry, "has_tool", lambda name: name == "workspace_orient")
        note = _payload(wo.handler())["machining_capabilities"]["note"]
        assert "cam_get" not in note
        assert "isGenerationAllowed" in note                       # the note itself still ships

    def test_the_block_rides_a_non_design_document_too(self, monkeypatch):
        # createFromString needs no document/CAM, so the capability is ambient even on a drawing doc
        # where has_design is false - the whole point of a document-INDEPENDENT signal.
        doc = _doc(name="Drawing1", design=None, cam=None)
        _install(active_product=None, doc=doc, design_for_cast=None)
        monkeypatch.setattr(wo._cam_common, "_create_strategy", strategy_factory(self._all(True)))
        out = _payload(wo.handler())
        assert out["has_design"] is False
        assert out["machining_capabilities"]["observed_generation"]["swarf"] is True

    def test_every_sentinel_is_a_measured_extension_unblocked_strategy(self):
        # THE VOCABULARY GUARD: a renamed/typo'd sentinel is caught HERE, offline, not in production -
        # where createFromString would raise Unknown forever and the capability would silently read
        # null. Ground truth is the measured extension A/B: a base license generates 33 of 54 milling
        # strategies, the Machining Extension 50, and these 17 are the ones it unblocks.
        extension_unblocked = {
            "advanced_swarf", "corner", "deburr", "feature_construction", "hole_recognition",
            "inspect_surface", "multi_axis_contour", "multi_axis_morph", "multiaxis_finishing",
            "multiaxis_roughing", "probe_geometry", "rotary_contour", "rotary_finishing",
            "rotary_pocket", "steep_and_shallow", "swarf", "three_plus_two"}
        assert len(extension_unblocked) == 17
        stray = set(wo._CAPABILITY_SENTINELS) - extension_unblocked
        assert not stray, f"sentinel(s) not in the measured extension-unblocked set: {sorted(stray)}"

    def test_the_block_reads_the_flag_through_the_shared_seam(self, monkeypatch):
        # the block owns no probe of its own: cam_generate's launch pre-flight excludes on the same
        # read, and a second copy is how the two answer differently about one strategy.
        monkeypatch.setattr(wo._cam_common, "strategy_generation_allowed", lambda name: name == "swarf")
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        og = _payload(wo.handler())["machining_capabilities"]["observed_generation"]
        assert og["swarf"] is True and og["probe_geometry"] is False

    def test_the_entitlement_verdict_keeps_an_unread_flag_apart_from_a_blocked_one(self):
        # sys_capability_map points a cold agent here for this ONE verdict. A bare all() over the
        # flags would fold an unread None into False - a confident 'not entitled' for a probe that
        # never answered, which is the opposite of what null means on the wire.
        assert wo._entitled_over([True, True, True, True]) is True
        assert wo._entitled_over([True, False, True, True]) is False
        assert wo._entitled_over([True, None, True, True]) is None

    def test_the_orient_publishes_the_verdict_beside_the_flags(self, monkeypatch):
        # The pointer sys_capability_map hands out has to land on a FIELD: an agent that had to
        # fold the four flags itself is the second vocabulary the map exists to prevent.
        des = self._small_design()
        _install(active_product=des, doc=_doc(design=des))
        table = self._all(True)
        table["swarf"] = None                       # the flag itself will not read
        monkeypatch.setattr(wo._cam_common, "_create_strategy", strategy_factory(table))
        block = _payload(wo.handler())["machining_capabilities"]
        # the SHARED fold, not a second one taken here: an unread flag leaves the verdict null
        assert block["entitled"] is None
        assert block["observed_generation"]["swarf"] is None
        assert block["observed_generation"]["probe_geometry"] is True
