# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The design world: bodies, faces, edges, the component and design builders, occurrences,
the timeline and the parameters."""

import math
import types

import live_api_facts as _api_facts

from tests.fakes.appearance import FakeAppearance
from tests.fakes.data_docs import FakeDataFile, FakeFusionDocument
from tests.fakes.geometry import FakeMatrix3D
from tests.fakes.mesh import _MeshBodies
from tests.fakes.scaffold import (_EntityProxy, _NamedCollection, _SimpleNamed, _Vertex,
                                  _absent_member, fusion_fake)


@fusion_fake(live_type="BRepBody", facts=("shape-dump-design-world",
                                          "brepbody-area-cm2-solid-and-open-surface"))
class BRepBody:
    """Matches type(entity).__name__ == 'BRepBody' in the tool's logic. volume/is_solid/entity_token
    are optional (all real BRepBody attributes per live_api_facts.SHAPES) for a before/after
    read-back check (e.g. a cut's volume delta). Visibility mirrors the live contract:
    isLightBulbOn is the body's OWN settable browser bulb; isVisible is the EFFECTIVE state
    (own bulb AND every ancestor's, modeled by hidden_by_ancestor) and has no setter. `vertices`
    takes FakePoints and wraps each as a BRepVertex-shaped item exposing .geometry. `faces` takes
    BRepFace objects a face-level read walks; `face_count` is the lighter count-only stand-in.

    This is a NATIVE body: nativeObject reads None and assemblyContext reads None, the pair a live
    component-owned body answers. Its occurrence proxy is ``body_proxy(this)``.

    `area` (cm2) and `mesh_manager` are set only when given, so a body whose surface area or mesh
    manager does not read stays a testable state. `solid_readable` False makes the isSolid read
    itself RAISE - the state a solid/surface verdict has to publish as null rather than guess.

    The appearance side is MEASURED (shape-dump-appearance-world) on a fresh scratch body with no
    override applied: `appearance` is already a live Appearance (never None) and `opacity` reads
    1.0, so both are defaults here; `visibleOpacity` RAISES on the NATIVE body, so the read declines
    unless `visible_opacity` names the number it answers - which is what the same read through an
    assembly-context proxy gave."""

    _UNSET = object()

    def __init__(self, name="Body", bbox=None, volume=0.0, is_solid=True, entity_token=None,
                 light_bulb=True, hidden_by_ancestor=False, vertices=(), parent_component=None,
                 face_count=0, faces=(), area=None, mesh_manager=None, solid_readable=True,
                 is_derived=False, appearance=_UNSET, opacity=1.0, visible_opacity=_UNSET,
                 edges=None):
        self.appearance = FakeAppearance() if appearance is BRepBody._UNSET else appearance
        self.opacity = opacity
        self._visible_opacity = visible_opacity
        self.name = name
        # A body a derive brought in answers True here; every ordinary one answers False.
        self.isDerived = is_derived
        self.parentComponent = parent_component
        self.nativeObject = None
        self.assemblyContext = None
        self.boundingBox = bbox
        self.vertices = _NamedCollection([_Vertex(p) for p in vertices])
        self.faces = _NamedCollection(list(faces) or [None] * face_count)
        self.volume = volume
        self._solid_readable = solid_readable
        self.isSolid = is_solid
        self.entityToken = entity_token or name
        self.isLightBulbOn = light_bulb
        self._hidden_by_ancestor = hidden_by_ancestor
        if area is not None:
            self.area = area
        # `edges` are the body's BRepEdges, for a free-edge census; unset, the walk does not read.
        if edges is not None:
            self.edges = _NamedCollection(list(edges))
        if mesh_manager is not None:
            self.meshManager = mesh_manager

    @property
    def isVisible(self):
        return bool(self.isLightBulbOn) and not self._hidden_by_ancestor

    @property
    def visibleOpacity(self):
        if self._visible_opacity is BRepBody._UNSET:
            if _NATIVE_OPACITY_RAISES:
                raise RuntimeError("3 : InternalValidationError : res")
            return getattr(self, "opacity", 1.0)
        return self._visible_opacity

    @property
    def isSolid(self):
        if not self._solid_readable:
            raise RuntimeError("3 : the solid flag of this body is unavailable")
        return self._is_solid

    @isSolid.setter
    def isSolid(self, value):
        self._is_solid = value

    @isSolid.deleter
    def isSolid(self):
        # `del body.isSolid` and go_stale(body, attrs=("isSolid",)) both mean the flag stops
        # answering, which is what solid_readable False already models.
        self._solid_readable = False


@fusion_fake(live_type="BRepBody", facts=("shape-dump-design-world", "body-proxy-token-differs",
                                          "entity-proxy-token-shared"))
class _OccurrenceProxy(_EntityProxy):
    """See ``body_proxy``. Every read but the three that make a proxy a proxy goes to the native."""

    def __init__(self, native, occurrence=None, entity_token=None):
        _EntityProxy.__init__(self, native)          # explicit: zero-arg super() and __class__ clash
        # One token PER PLACEMENT: a proxy's token addresses a body IN one occurrence (a handle for
        # instance 2 must not resolve to instance 1), so two placements' proxies read distinct
        # tokens live - the fake folds the occurrence into the default for the same reason.
        token = entity_token or (f"PROXY::{getattr(occurrence, 'name', None)}"
                                 f"::{getattr(native, 'entityToken', None)}")
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_occurrence", occurrence)

    @property
    def __class__(self):
        # A proxy IS a BRepBody live, and body code isinstance-checks against adsk.fusion.BRepBody.
        # isinstance consults __class__, so the proxy answers the wrapped fake's type.
        return type(object.__getattribute__(self, "_obj"))

    @property
    def entityToken(self):
        return object.__getattribute__(self, "_token")

    @property
    def nativeObject(self):
        return object.__getattribute__(self, "_obj")

    @property
    def assemblyContext(self):
        return object.__getattribute__(self, "_occurrence")


@fusion_fake(factory_for="_OccurrenceProxy")
def body_proxy(native, occurrence=None, entity_token=None):
    """The occurrence PROXY of `native` (measured: body-proxy-token-differs - a proxy's entityToken
    differs from its native's, proxy.nativeObject IS the native, a native's nativeObject reads None,
    so a de-dup keys on (nativeObject or self).entityToken); every other read delegates to `native`."""
    return _OccurrenceProxy(native, occurrence, entity_token)


# The SENTINEL string internalUnits reads and whether convert refuses a dimension-incompatible
# pair, both measured by units-manager-internal-units-and-convert; BEHAVIOR carries them after the
# republish, so the `in` gate is what keeps the module importable until then - the ROW is the
# provenance either way, never the fallback beside it.
_UM_SENTINEL = (_api_facts.BEHAVIOR["units_manager_internal_units_sentinel"]
                if "units_manager_internal_units_sentinel" in _api_facts.BEHAVIOR
                else "InternalUnits")
_UM_REFUSES_INCOMPATIBLE = (_api_facts.BEHAVIOR["units_manager_convert_refuses_incompatible"]
                            if "units_manager_convert_refuses_incompatible" in _api_facts.BEHAVIOR
                            else True)


@fusion_fake(live_type="UnitsManager",
             facts=("shape-dump-units-manager", "units-manager-internal-units-and-convert"))
class FakeUnitsManager:
    """A UnitsManager whose evaluateExpression resolves only a known set - an unknown reference
    RAISES, matching the live FusionUnitsManager (it errors on an unresolvable or dimension-
    incompatible expression). The shared engine for a tool that accepts a parameter-EXPRESSION
    string routed through ValueInput.createByString (model_extrude's distance, model_construction's
    offset). model_extrude.py still keeps a local copy pending migration to this one.

    A resolvable expression evaluates to `value`, and the `units` argument acts the four measured
    live ways - which is what lets a caller tell the shapes apart:
      arithmetic (the default)   scales with the argument      '5'   -> 5.0 "", 0.5 "mm", 0.0873 "deg"
      `dimensioned` names        carries its own unit          '5 mm'-> 0.5 under "" and "mm"
      `angle_dimensioned` names  carries its own ANGULAR unit  30 deg-> its radian value under "" and
                                                                "deg", RAISES under a length unit
      `strict_unitless` names    RAISES under ANY unit         a bare unitless PARAMETER is readable
                                                                only with no units at all
    `value` sets the number a case needs (e.g. a non-positive one).

    `internalUnits` is a SENTINEL string, not a unit name (measured by
    units-manager-internal-units-and-convert; BEHAVIOR carries it after the republish), and
    `convert` runs the same table backwards from it: handed that sentinel as the from-unit it lifts
    a DATABASE number into the display unit - cm for a length target, radians for an angular one,
    one call shape for both, which is how a parameter row publishes a value in the parameter's OWN
    unit. From a LITERAL unit it converts only within one dimension and raises '6 : The input and
    output units are not compatible' across the length/angle boundary; an EMPTY to-unit raises
    '3 : Bad units parameter'. Both messages and the sentinel come off that same row.

    The unit table here is NARROWER than live: live converts into every length unit it knows, while
    a to-unit outside this table raises '6 : The units parameter is not a valid unit string' (the
    message that row measured for an unknown unit) - so a test needing another unit adds it here
    rather than reading the raise as the platform's answer."""
    defaultLengthUnits = "mm"
    internalUnits = _UM_SENTINEL

    # internal units per display unit - Fusion holds lengths in cm and angles in radians, so a
    # unitless expression read as mm returns a tenth of its plain number and one read as deg returns
    # its radian equivalent.
    _CM_PER_UNIT = {"": 1.0, "mm": 0.1, "cm": 1.0, "in": 2.54,
                    "deg": 0.017453292519943295, "rad": 1.0}

    _ANGLE_UNITS = ("deg", "rad")

    def __init__(self, valid=("25 mm", "StockZ/2"), value=2.5, dimensioned=(), strict_unitless=(),
                 angle_dimensioned=()):
        self._valid = set(valid)
        self._value = value
        self._dimensioned = set(dimensioned)
        self._strict_unitless = set(strict_unitless)
        self._angle_dimensioned = set(angle_dimensioned)

    def evaluateExpression(self, expr, units=None):
        if expr not in self._valid:
            raise RuntimeError(f"unresolved parameter in '{expr}'")
        if expr in self._dimensioned:
            return self._value
        if expr in self._angle_dimensioned:
            if units and units not in self._ANGLE_UNITS:
                raise RuntimeError(f"'{expr}' is not a valid expression in a {units} context")
            return self._value
        if units and expr in self._strict_unitless:
            raise RuntimeError(f"'{expr}' is not a valid expression in a {units} context")
        return self._value * self._CM_PER_UNIT.get(units or "", 1.0)

    def convert(self, value, from_unit, to_unit):
        if not to_unit:
            raise RuntimeError("3 : Bad units parameter")
        if to_unit not in self._CM_PER_UNIT:
            raise RuntimeError("6 : The units parameter is not a valid unit string")
        # Only the sentinel crosses the length/angle boundary; a literal from-unit is held to one
        # dimension, which is what stops a caller reaching an angle out of a centimetre.
        if from_unit != self.internalUnits and _UM_REFUSES_INCOMPATIBLE:
            if (from_unit in self._ANGLE_UNITS) != (to_unit in self._ANGLE_UNITS):
                raise RuntimeError("6 : The input and output units are not compatible")
        return value / self._CM_PER_UNIT[to_unit]


@fusion_fake(live_type="BRepFace", facts=("shape-dump-design-world",))
class BRepFace:
    """Matches type(entity).__name__ == 'BRepFace'. `geometry` is the surface.

    `_classify` reads area/centroid/edges.count/body.name when it builds a face
    record; supply benign defaults so the result is JSON-serializable. Override
    via kwargs in tests that assert on them. `entity_token` is None by default (matching a fake
    built before handle-minting needed it - safe() degrades a None token the same as a missing
    attribute); set it for a test that mints/asserts on a find_geometry-style handle.

    `body` supplies the owning BRepBody itself (for a volume/token read); `body_name` is the
    lighter name-only stand-in. `normal` installs a surface evaluator whose getNormalAtPoint
    returns it - the live (bool, Vector3D) tuple. Without `normal` the face carries no evaluator,
    which is how a face whose normal cannot be sampled reads. `bounding_box` is the face's own AABB
    (BRepFace.boundingBox); without it the face reads as one whose box is unavailable.
    `assembly_context` is the occurrence a PROXY face was read through - None (the default) is a
    NATIVE face, whose reads are in its owning component's space; `native_object` is the face that
    proxy STANDS FOR, and reads None on a native one. `param_reversed` is the live
    isParamReversed a normal flip is read back on; None leaves that flag unreadable.
    `assembly_proxy` installs createForAssemblyContext answering it - pass None for the factory that
    ANSWERS NOTHING, which is the state a caller must not fall back to the native face on. Left
    unset the face carries no factory at all, so a proxy read declines instead.

    MEASURED (shape-dump-appearance-world): a fresh face's `appearance` is already a live
    Appearance, so that is the default here; pass one to seed a specific asset.
    """

    _UNSET = object()

    def __init__(self, surface, area=0.0, centroid=None, edge_count=0, body_name=None,
                 entity_token=None, body=None, point_on_face=None, normal=None,
                 bounding_box=None, assembly_context=None, param_reversed=None,
                 assembly_proxy=_UNSET, appearance=_UNSET, edges=None, native_object=None):
        self.appearance = FakeAppearance() if appearance is BRepFace._UNSET else appearance
        self.geometry = surface
        self.assemblyContext = assembly_context
        self.nativeObject = native_object
        if assembly_proxy is not BRepFace._UNSET:
            self.createForAssemblyContext = lambda _occ, _p=assembly_proxy: _p
        if param_reversed is not None:
            self.isParamReversed = bool(param_reversed)
        if bounding_box is not None:
            self.boundingBox = bounding_box
        self.area = area
        self.centroid = centroid
        # `edges` are the BRepEdges bounding the face, for a caller that walks them; `edge_count`
        # alone gives that many placeholder slots, which is enough for a count read.
        self.edges = _NamedCollection(list(edges) if edges is not None
                                      else [None] * edge_count)
        self.body = body if body is not None else (_SimpleNamed(body_name) if body_name else None)
        self.entityToken = entity_token
        self.pointOnFace = point_on_face
        if normal is not None:
            self.evaluator = types.SimpleNamespace(
                getNormalAtPoint=lambda _pt, n=normal: (True, n))


@fusion_fake(live_type="BRepEdge", facts=("shape-dump-design-world",
                                          "brepedge-evaluator-tangent-follows-curve-not-edge"))
class BRepEdge:
    """Matches type(entity).__name__ == 'BRepEdge'. `geometry` is the curve. `point_on_edge`/
    `entity_token` are None by default (see BRepFace) - set them for a test that mints/asserts on a
    find_geometry-style handle. `tangent` installs a curve evaluator answering the live
    (bool, value) tuples getParameterAtPoint/getTangent return; `co_edges` installs the BRepCoEdges
    bounding it. Left None the edge carries neither attribute, which is how one whose curve or
    topology will not read reads. `param_reversed` is the live isParamReversed - whether the edge
    runs against its own curve; None leaves that flag unreadable too. `body` is the BRepBody the edge
    belongs to, set only when given - a one-body chain check walks it. `assembly_proxy` installs
    createForAssemblyContext answering it (the same knob BRepFace carries) - pass None for the
    factory that ANSWERS NOTHING, which is the state a caller must not fall back to the native edge
    on. Left unset the edge carries no factory at all, so a proxy read declines instead."""

    _UNSET = object()

    def __init__(self, curve, start=None, end=None, point_on_edge=None, entity_token=None,
                 tangent=None, co_edges=None, param_reversed=False, body=None,
                 assembly_proxy=_UNSET, faces=None):
        self.geometry = curve
        # `faces` are the BRepFaces the edge bounds - one on a surface's open boundary, two on a
        # sealed edge; left None the edge answers no face count at all.
        if faces is not None:
            self.faces = _NamedCollection(list(faces))
        if assembly_proxy is not BRepEdge._UNSET:
            self.createForAssemblyContext = lambda _occ, _p=assembly_proxy: _p
        self.startVertex = _Vertex(start) if start else None
        self.endVertex = _Vertex(end) if end else None
        self.pointOnEdge = point_on_edge
        self.entityToken = entity_token
        if body is not None:
            self.body = body
        if param_reversed is not None:
            self.isParamReversed = bool(param_reversed)
        if tangent is not None:
            self.evaluator = types.SimpleNamespace(
                getParameterAtPoint=lambda _pt: (True, 0.0),
                getTangent=lambda _prm, t=tangent: (True, t))
        if co_edges is not None:
            self.coEdges = _NamedCollection(list(co_edges))


@fusion_fake(live_type="Component", facts=("shape-dump-design-world",))
class MakeComp:
    """A component with the standard Fusion collection protocol (count/item/itemByName).

    Pass bodies as names (str) or objects with a `.name`. Extra collections a specific tool needs
    (e.g. a fake `features`) can be attached by the caller after construction, or pass a ready-made
    component to `make_design(comp=...)` instead.

    `mesh_bodies` is the separate MeshBody collection (a _MeshBodies, which carries no itemByName) -
    set only when given, so a component that answers no meshBodies at all stays a testable state.

    `origin_construction_point`, `construction_axes` (x, y, z) and `origin_planes` (xY, xZ, yZ) are
    the component's own origin geometry - the anchor a coordinate snap resolves to, the three axis
    entities a world-axis or revolve read picks from, and the three planes a plane alias resolves to.
    Each is set only when given, so a component whose origin geometry does not read stays a testable
    state; live every component carries all seven.
    """

    _UNSET = object()

    def __init__(self, name="Root", bodies=(), occurrences=(), sketches=(), entity_token=None,
                 parent_design=None, mesh_bodies=None, all_occurrences=None, joints=None,
                 as_built_joints=None, joint_origins=None, rigid_groups=None, motion_links=None,
                 assembly_constraints=None, occurrences_by_component=None,
                 origin_construction_point=_UNSET, construction_axes=None, origin_planes=None):
        self.name = name
        norm = [BRepBody(b) if isinstance(b, str) else b for b in bodies]
        for body in norm:
            # Renaming it into BRepBody(<that object>) makes `name` the object itself: every
            # by-name read then misses and the row publishes junk, far from the line that built it.
            if not hasattr(body, "name"):
                raise TypeError(
                    "MakeComp body %r carries no name - pass a body name (str) or an object "
                    "with a .name" % (body,))
        self.bRepBodies = _NamedCollection(norm)
        if mesh_bodies is not None:
            self.meshBodies = _MeshBodies(list(mesh_bodies))
        self.occurrences = _NamedCollection(list(occurrences))
        # allOccurrences is the NESTED walk - the same list as occurrences unless a caller hands the
        # deeper one, which is the only walk reaching an occurrence inside a sub-assembly.
        self.allOccurrences = list(occurrences if all_occurrences is None else all_occurrences)
        self.sketches = _NamedCollection(list(sketches))
        self.boundingBox = None
        # The assembly collections a live Component ALWAYS carries - a component holding no joints
        # answers an EMPTY walk, never a missing member, so each defaults to empty. A collection
        # that will not enumerate is _NamedCollection(raises=...) handed in here.
        self.joints = _NamedCollection(list(joints or ()))
        self.asBuiltJoints = _NamedCollection(list(as_built_joints or ()))
        self.jointOrigins = _NamedCollection(list(joint_origins or ()))
        self.rigidGroups = _NamedCollection(list(rigid_groups or ()))
        self.motionLinks = _NamedCollection(list(motion_links or ()))
        self.assemblyConstraints = _NamedCollection(list(assembly_constraints or ()))
        self._occurrences_by_component = occurrences_by_component
        if origin_construction_point is not MakeComp._UNSET:
            self.originConstructionPoint = origin_construction_point
        if construction_axes is not None:
            (self.xConstructionAxis, self.yConstructionAxis,
             self.zConstructionAxis) = construction_axes
        if origin_planes is not None:
            (self.xYConstructionPlane, self.xZConstructionPlane,
             self.yZConstructionPlane) = origin_planes
        # Set only when asked: a component whose token does NOT read is its own tested state, and
        # every live component has a token but two DISTINCT ones can share it (document-local), so a
        # test that cares about identity must choose the tokens rather than inherit a default.
        if entity_token is not None:
            self.entityToken = entity_token
        # The design this component belongs to - the first hop of the chain a body's SOURCE DOCUMENT
        # is read through (parentComponent -> parentDesign -> parentDocument -> dataFile.id). Set
        # only when asked, so a component whose document cannot be read stays a testable state; see
        # make_source_document.
        if parent_design is not None:
            self.parentDesign = parent_design

    def allOccurrencesByComponent(self, component):
        """Every occurrence in this component's subtree placing `component` - the multi-placement
        rung of the placement ladder. Matched on entityToken, since two references to one component
        are distinct objects sharing one token and an identity compare would answer none; a curated
        `occurrences_by_component` map keyed by component NAME overrides that walk."""
        # A COUNTED collection, never a bare list: a caller reads `.count` off it, and a list's own
        # `count` is a bound METHOD - truthy, never an int, so a list silently breaks that read.
        if self._occurrences_by_component is not None:
            return _NamedCollection(
                self._occurrences_by_component.get(getattr(component, "name", None), []))
        want = getattr(component, "entityToken", None)
        if want is None:
            return _NamedCollection([])
        out = []
        for occ in self.allOccurrences:
            try:
                placed = occ.component
            except Exception:
                continue
            if getattr(placed, "entityToken", None) == want:
                out.append(occ)
        return _NamedCollection(out)


# How many appearance assets a design carrying NO body holds, measured by
# parameter-favorite-maker-text-value-and-fresh-appearances. The `in` gate keeps the import
# working while the generated facts file is one republish behind the row that emits the key.
_FRESH_DESIGN_APPEARANCES = (_api_facts.BEHAVIOR["fresh_design_appearances_count"]
                             if "fresh_design_appearances_count" in _api_facts.BEHAVIOR else 0)
# A native body's visibleOpacity raises with no override set (measured on the appearance world).
_NATIVE_OPACITY_RAISES = (_api_facts.BEHAVIOR["native_body_visible_opacity_raises"]
                          if "native_body_visible_opacity_raises" in _api_facts.BEHAVIOR else True)


@fusion_fake(live_type="Design",
             facts=("shape-dump-design-world", "allcomponents-design-only",
                    "find-entity-token-shape", "find-entity-token-miss",
                    "find-entity-token-multi", "design-computeall-returns-true",
                    "design-activate-root-reads-back",
                    "parameter-favorite-maker-text-value-and-fresh-appearances"))
class MakeDesign:
    """A design exposing the attributes tools/inputs read: rootComponent, activeComponent (defaults to
    root), allOccurrences, allComponents, and findEntityByToken(token) backed by a `tokens` map.

    `parent_document` is the Document this design belongs to - the second hop of the source-document
    chain (see make_source_document). Set only when asked: a design whose document cannot be read is
    its own tested state, and that is what an unsaved or unreachable document looks like.

    `design_type` is the PARAMETRIC/DIRECT mode a ModeGuard branches on and `active_edit_object` the
    open base-feature scope it detects; both are set only when asked, since a design answering
    neither read is its own tested state.

    `timeline` (make_timeline), `user_parameters` (FakeUserParameters) and `all_parameters` are the
    three design-level collections the timeline and parameter tools read; each is set only when
    asked, since a design whose timeline or parameters do not read is its own tested state.

    `computeAll()` is the full recompute design_recompute drives; it answers the measured True,
    `compute_raises` is the message it throws with instead, and the calls are counted privately in
    `_computes`.

    `active_occurrence` is the occurrence holding the edit target, None at the root - the read
    `isRootComponentActive` is the other face of, never disagreeing with it live. `root_activate_ok`
    is what `activateRootComponent()` answers and `root_activate_lies` the answer-true-and-never-
    clear state a return-to-root read-back catches (the Design-side twin of FakeOccurrence's
    `activate_lies`); Occurrence carries no deactivate(), so this is the only way back to root.
    `root_active_reads` forces `isRootComponentActive` to one answer whatever `activeOccurrence`
    holds - a DECLARED worst case, since live the two are one state, and the only shape in which a
    caller checking just one of the pair can be caught.

    `appearances` is the document's own appearance assets. MEASURED (parameter-favorite-maker-text-
    value-and-fresh-appearances): a design carrying NO body answers an EMPTY collection - an asset
    arrives with the geometry, so a design holding an extruded body holds that body's default
    appearance - and that count is READ from BEHAVIOR["fresh_design_appearances_count"] through an
    `in` gate, so the default here follows the measurement rather than a literal. Passing an
    explicit None installs the member
    ANSWERING null, which is a different state from the absent member and the one a safe() read
    cannot tell from it by accident. `analyses` is the analyses collection a section read reaches
    sectionAnalyses through, set only when asked: no measurement says what a plain design answers."""

    _UNSET = object()
    def __init__(self, comp=None, tokens=None, all_components=None, parent_document=None,
                 design_type=None, active_edit_object=None, timeline=None, user_parameters=None,
                 all_parameters=None, compute_raises=None, active_occurrence=None,
                 root_activate_ok=True, root_activate_lies=False, root_active_reads=None,
                 snapshots=None, appearances=_UNSET, analyses=None):
        self._compute_raises = compute_raises
        self._computes = 0
        self.rootComponent = comp if comp is not None else MakeComp()
        self.activeComponent = self.rootComponent
        self.activeOccurrence = active_occurrence
        self._root_activate_ok = root_activate_ok
        self._root_activate_lies = root_activate_lies
        self._root_active_reads = root_active_reads
        # A design whose snapshots read DECLINES leaves the moved-but-uncaptured flag unreadable,
        # which is not the answer "nothing is pending" - a joint create refuses only on a flag that
        # proved True. Set when given, since no shared Snapshots fake carries the ordinary answer.
        if snapshots is not None:
            self.snapshots = snapshots
        self.appearances = (_NamedCollection([None] * _FRESH_DESIGN_APPEARANCES)
                            if appearances is MakeDesign._UNSET else appearances)
        if analyses is not None:
            self.analyses = analyses
        if parent_document is not None:
            self.parentDocument = parent_document
        if design_type is not None:
            self.designType = design_type
        if active_edit_object is not None:
            self.activeEditObject = active_edit_object
        if timeline is not None:
            self.timeline = timeline
        if user_parameters is not None:
            self.userParameters = user_parameters
        if all_parameters is not None:
            self.allParameters = all_parameters
        self._tokens = dict(tokens or {})
        self._all_components = list(all_components) if all_components is not None else [self.rootComponent]

    @property
    def allComponents(self):
        # A counted+iterable collection on the DESIGN, as in the live API - Component has no
        # allComponents attribute, so a fake must not offer one anywhere else.
        return _NamedCollection(self._all_components)

    @property
    def isRootComponentActive(self):
        # The measured pair: a null activeOccurrence and root-active are ONE state - unless a test
        # declares the worst case in which they disagree.
        return (self.activeOccurrence is None if self._root_active_reads is None
                else self._root_active_reads)

    def activateRootComponent(self):
        if self._root_activate_ok and not self._root_activate_lies:
            self.activeOccurrence = None
        return self._root_activate_ok

    def findEntityByToken(self, token):
        # Live returns a SWIG BaseVector, not a list - len/bool/index/iterate behave list-like
        # (live-verified), so never assert isinstance(result, list). A token can answer with SEVERAL
        # entities (measured: splitting a face makes the pre-split token resolve to BOTH
        # survivors), so a tokens entry may be a LIST and is handed back as-is.
        e = self._tokens.get(token)
        if isinstance(e, (list, tuple)):
            return list(e)
        if e is not None:
            return [e]
        if _api_facts.BEHAVIOR["find_entity_token_empty_on_miss"]:
            return []
        raise RuntimeError("3 : invalid argument token")

    def computeAll(self):
        if self._compute_raises:
            raise RuntimeError(self._compute_raises)
        self._computes += 1
        return True


@fusion_fake(factory_for="MakeDesign")
def make_source_document(urn):
    """The `parentDesign` a component in the document with lineage id `urn` answers.

    An entity's SOURCE DOCUMENT is read through parentComponent -> parentDesign -> parentDocument ->
    dataFile.id (``_common.native_identity``), and that id is what separates two entities in two
    x-ref'd documents whose document-local entityTokens collide. Pass ``urn=None`` for a NEVER-SAVED
    document: it carries no dataFile at all, so the chain stops one hop short.

    Hand a DIFFERENT urn to each component standing for a different source document; hand the SAME
    one to components of a single document. The two hops are FakeFusionDocument and FakeDataFile
    (defined below), so the chain a tool walks is the shared document world's."""
    return MakeDesign(parent_document=FakeFusionDocument(
        data_file=(FakeDataFile(file_id=urn) if urn is not None else None)))


# The plain answers an occurrence gives before anything is modelled in it, measured by
# occurrence-plain-reads-valid-and-lit. The `in` gate is what keeps a key the generated facts file
# does not carry yet from breaking the import; the literal beside it is the fake's own standing
# answer until the republish lands.
_OCC_PLAIN_VALID = (_api_facts.BEHAVIOR["occurrence_plain_is_valid"]
                    if "occurrence_plain_is_valid" in _api_facts.BEHAVIOR else True)
_OCC_PLAIN_LIT = (_api_facts.BEHAVIOR["occurrence_plain_is_lit"]
                  if "occurrence_plain_is_lit" in _api_facts.BEHAVIOR else True)
_OCC_APPEARANCE_NONE = (_api_facts.BEHAVIOR["occurrence_appearance_none_by_default"]
                        if "occurrence_appearance_none_by_default" in _api_facts.BEHAVIOR else True)
_OCC_NO_OPACITY = (_api_facts.BEHAVIOR["occurrence_has_no_opacity"]
                   if "occurrence_has_no_opacity" in _api_facts.BEHAVIOR else True)


@fusion_fake(live_type="Occurrence",
             facts=("shape-dump-design-world",
                    "occurrence-plain-reads-referenced-false-empty-collections",
                    "occurrence-plain-reads-valid-and-lit",
                    "shape-dump-appearance-world"))
class FakeOccurrence:
    """One assembly occurrence: the component it places, its fullPathName, its name.

    ``raises`` models the UNRESOLVED EXTERNAL REFERENCE state as this fake's declared WORST CASE,
    which no measurement row carries: the occurrence is present in the tree and EVERY read on it
    throws. ``component`` is the read the resolvers detect that state on, and ``fullPathName``
    throws with it - which is why code that names an occurrence in an error must fall back to the
    string it was handed. A fake that raises only on ``component`` lets a missing fullPathName guard
    survive its own test.

    ``name`` keeps reading in that state: it is the one identity a caller can still publish.

    ``transform2`` is what the occurrence PLACES its component with (a FakeMatrix3D) and
    ``assemblyContext`` the occurrence placing THIS one, or None where the chain ends at the root -
    the pair a placement ladder walks. ``isGroundToParent`` is the ground-to-parent lock, which sits
    on a NESTED instance as readily as a top-level one, and ``childOccurrences`` is the level below
    this one - the pair a design-wide census walks and asks. All four answer through ``raises`` like
    every other read here, and holding a new read to that contract is what stops a caller reading a
    placement or a lock off an occurrence whose component will not load.

    ``raises_on`` narrows the same worst case to ONE read - {property name: message} - for the row
    that is otherwise readable while a single property declines: a lock flag that will not answer,
    a path that will not read, a child collection that will not enumerate. Like ``raises`` it is a
    DECLARED worst case, not a shape any measurement row carries.

    ``isActive`` is whether this instance is the active EDIT TARGET and ``activate()`` makes it so,
    answering the bool its caller gates on; ``activate_lies`` is the answer-true-and-never-flip
    state an activation read-back catches, and ``activate_ok`` False the refusal said out loud.

    ``transform`` is the LOCAL placement, relative to the parent component, composing no ancestor -
    the read a world-vs-local pair is told apart by, and the fallback a build whose ``transform2``
    declines falls to. Both are SETTABLE, since a free move writes one back and reads it again.
    ``isGroundToParent`` is settable for the same reason; ``ground_set_ok`` False is the write the
    platform refuses and ``ground_lies`` the one it accepts and never applies - the pair a
    read-back gate separates.

    ``joints`` is the joint membership a move warns about, ``isGrounded`` the legacy UI Ground/Fix
    flag, ``bRepBodies`` the bodies this instance places, ``isReferencedComponent`` whether the
    instance places an EXTERNAL component and ``boundingBox`` its own box. Measured: a plain local
    occurrence answers isReferencedComponent False and two EMPTY collections, so those are the
    defaults here - a read that DECLINES is ``raises_on``, never a missing member.

    ``valid`` and ``light_bulb_on`` are ``isValid`` (whether the instance still stands for a live
    object) and ``isLightBulbOn`` (its own visibility bulb), PLAIN attributes so an isolate walk that
    writes the bulb and reads it back drives the shared fake. Their defaults are READ from
    ``BEHAVIOR["occurrence_plain_is_valid"]`` / ``["occurrence_plain_is_lit"]``
    (occurrence-plain-reads-valid-and-lit: a plain local occurrence answers True to both), through
    an ``in`` gate so the module still imports while the generated facts file is one republish
    behind the row. A read that DECLINES is ``raises_on``. ``bodies_bounding_box`` is what
    ``boundingBox2(entityTypes)`` answers - the BODIES-ONLY box, which the plain ``boundingBox``
    (construction geometry included) must not stand in for; pass None for the read that answers
    NOTHING, which is what an instance placing no body gives. Left unset the occurrence carries no
    boundingBox2 at all, the state a body_aabb fallback is tested on.

    ``isolated`` is ``isIsolated``, the isolate lock. Measured
    (occurrence-plain-reads-valid-and-lit): a plain local occurrence answers the bool False, so that
    is the default rather than an absent member; the read goes through ``raises_on`` like every
    other one here and the write lands, so an isolate walk that sets the lock and reads it back
    drives the shared fake.

    ``appearance`` is the override applied to this instance - None until one is, measured by
    shape-dump-appearance-world, which also measured that Occurrence carries NO ``opacity`` member
    at all; both are read from BEHAVIOR through the same ``in`` gate as the flags above.

    ONE class, so a test can point ``adsk.fusion.Occurrence`` at it and the shared occurrence
    resolver's isinstance check passes on a handle it resolved.
    """

    _UNSET = object()

    if _OCC_NO_OPACITY:
        opacity = _absent_member("opacity")

    def __init__(self, path="Comp:1", component=None, raises=None, transform2=None,
                 assembly_context=None, ground_to_parent=None, children=(), raises_on=None,
                 is_active=False, activate_ok=True, activate_lies=False, transform=None,
                 joints=None, grounded=None, bodies=None, bounding_box=None, entity_token=None,
                 ground_set_ok=True, ground_lies=False, referenced=None, delete_ok=True,
                 derived=False, document_reference=None, valid=_OCC_PLAIN_VALID,
                 light_bulb_on=_OCC_PLAIN_LIT, bodies_bounding_box=_UNSET, isolated=False):
        self.isValid = valid
        self.isLightBulbOn = light_bulb_on
        self._isolated = bool(isolated)
        if _OCC_APPEARANCE_NONE:
            self.appearance = None
        if bodies_bounding_box is not FakeOccurrence._UNSET:
            self.boundingBox2 = lambda _entity_types, _bb=bodies_bounding_box: _bb
        self._path = path
        self._component = component
        self._raises = raises
        self._transform2 = transform2
        self._transform = transform
        self._assembly_context = assembly_context
        self._ground_to_parent = ground_to_parent
        self._children = _NamedCollection(list(children))
        self._raises_on = dict(raises_on or {})
        self._is_active = is_active
        self._activate_ok = activate_ok
        self._activate_lies = activate_lies
        self._grounded = grounded
        self._bounding_box = bounding_box
        self._ground_set_ok = ground_set_ok
        self._ground_lies = ground_lies
        self._grounds = []
        # The ORDINARY live answers (measured: occurrence-plain-reads-referenced-false-empty-
        # collections - a plain local occurrence reads False and two empty collections). A read that
        # DECLINES is raises_on, never an absent member: the live type carries all three.
        self._joints = _NamedCollection(list(joints or ()))
        self._bodies = _NamedCollection(list(bodies or ()))
        self._referenced = False if referenced is None else referenced
        # An instance a derive created answers True here; every ordinary one answers False, so this
        # is the plain live answer rather than a member set only when a test asks.
        self._derived = bool(derived)
        # The xref link this instance places its component through, None where it places a local
        # one; `raises_on` is how the read that DECLINES is modelled, as for every other read here.
        self._document_reference = document_reference
        # Set only when asked: every live occurrence has a token, but two DISTINCT ones can share it
        # (document-local), so a test that cares about identity chooses the token.
        if entity_token is not None:
            self.entityToken = entity_token
        self.name = path.split("+")[-1]
        self._delete_ok = delete_ok
        self._deleted = False

    def deleteMe(self):
        # A delete the platform REFUSES answers False and leaves the instance placed, which is the
        # only state a caller re-reading the tree can tell from a delete that landed.
        if self._delete_ok:
            self._deleted = True
        return self._delete_ok

    def _read(self, prop, value):
        if self._raises:
            raise RuntimeError(self._raises)
        if prop in self._raises_on:
            raise RuntimeError(self._raises_on[prop])
        return value

    @property
    def fullPathName(self):
        return self._read("fullPathName", self._path)

    @property
    def component(self):
        return self._read("component", self._component)

    @property
    def isIsolated(self):
        return self._read("isIsolated", self._isolated)

    @isIsolated.setter
    def isIsolated(self, value):
        self._isolated = value

    # Each read below goes through _read, so `raises`/`raises_on` govern it like every
    # other read here; each takes a setter so a scenario SUBCLASS can assign it in its own __init__.
    @property
    def isGrounded(self):
        return self._read("isGrounded", self._grounded)

    @isGrounded.setter
    def isGrounded(self, value):
        self._grounded = value

    @property
    def boundingBox(self):
        return self._read("boundingBox", self._bounding_box)

    @boundingBox.setter
    def boundingBox(self, value):
        self._bounding_box = value

    @property
    def joints(self):
        return self._read("joints", self._joints)

    @joints.setter
    def joints(self, value):
        self._joints = value

    @property
    def bRepBodies(self):
        return self._read("bRepBodies", self._bodies)

    @bRepBodies.setter
    def bRepBodies(self, value):
        self._bodies = value

    @property
    def isReferencedComponent(self):
        return self._read("isReferencedComponent", self._referenced)

    @isReferencedComponent.setter
    def isReferencedComponent(self, value):
        self._referenced = value

    @property
    def isDerived(self):
        return self._read("isDerived", self._derived)

    @isDerived.setter
    def isDerived(self, value):
        self._derived = value

    @property
    def documentReference(self):
        return self._read("documentReference", self._document_reference)

    @documentReference.setter
    def documentReference(self, value):
        self._document_reference = value

    @property
    def transform(self):
        return self._read("transform", self._transform)

    @transform.setter
    def transform(self, value):
        self._transform = value

    @property
    def transform2(self):
        return self._read("transform2", self._transform2)

    @transform2.setter
    def transform2(self, value):
        self._transform2 = value

    @property
    def assemblyContext(self):
        return self._read("assemblyContext", self._assembly_context)

    @property
    def isGroundToParent(self):
        return self._read("isGroundToParent", self._ground_to_parent)

    @isGroundToParent.setter
    def isGroundToParent(self, value):
        # ``ground_set_ok`` False RAISES, which is the refusal the tool's try/except reports;
        # ``ground_lies`` takes the assignment silently and never flips, which only a read-back catches.
        self._grounds.append(bool(value))
        if not self._ground_set_ok:
            raise RuntimeError("3 : this occurrence cannot be grounded to its parent")
        if not self._ground_lies:
            self._ground_to_parent = bool(value)

    @property
    def childOccurrences(self):
        return self._read("childOccurrences", self._children)

    @childOccurrences.setter
    def childOccurrences(self, value):
        self._children = value

    @property
    def isActive(self):
        return self._read("isActive", self._is_active)

    def activate(self):
        if self._activate_ok and not self._activate_lies:
            self._is_active = True
        return self._activate_ok


@fusion_fake(factory_for="FakeOccurrence")
def make_occurrence(path="Comp:1", component=None, raises=None, transform2=None,
                    assembly_context=None, ground_to_parent=None, children=(), raises_on=None,
                    transform=None, joints=None, grounded=None, bodies=None, bounding_box=None,
                    entity_token=None, referenced=None, delete_ok=True, derived=False,
                    document_reference=None, valid=_OCC_PLAIN_VALID, light_bulb_on=_OCC_PLAIN_LIT,
                    bodies_bounding_box=FakeOccurrence._UNSET, isolated=False):
    """An occurrence placing `component` at assembly path `path`, with the placement matrix
    ``transform2`` and the occurrence ``assembly_context`` that places it, the ground-to-parent lock
    ``ground_to_parent`` and the nested ``children`` a census descends into. Pass ``raises`` to model
    an unresolved external reference, where every read but ``name`` throws that message, or
    ``raises_on`` = {property: message} for the row where only that one read declines;
    ``delete_ok`` False is the deleteMe the platform refuses. ``valid``/``light_bulb_on``/
    ``bodies_bounding_box``/``isolated`` pass through to FakeOccurrence."""
    return FakeOccurrence(path, component, raises, transform2, assembly_context,
                          ground_to_parent, children, raises_on, transform=transform,
                          joints=joints, grounded=grounded, bodies=bodies,
                          bounding_box=bounding_box, entity_token=entity_token,
                          referenced=referenced, delete_ok=delete_ok, derived=derived,
                          document_reference=document_reference, valid=valid,
                          light_bulb_on=light_bulb_on, bodies_bounding_box=bodies_bounding_box,
                          isolated=isolated)


@fusion_fake(factory_for="FakeOccurrence")
def make_placed_occurrence(path, pos=None, unreadable="transform unreadable"):
    """One occurrence of a component named for `path`'s leaf, at WORLD translation `pos` (cm) - or,
    with `pos` None, one whose transform2 AND transform both decline, since an origin read falls
    from the first to the second and blinding one alone leaves a readable pose."""
    comp = MakeComp(name=path.split("+")[-1].split(":")[0])
    if pos is None:
        return make_occurrence(path=path, component=comp,
                               raises_on={"transform2": unreadable, "transform": unreadable})
    return make_occurrence(path=path, component=comp, transform2=FakeMatrix3D(t=pos))


# ── timeline world ────────────────────────────────────────────────────────

@fusion_fake(live_type="TimelineObject",
             facts=("shape-dump-timeline-world", "enum-feature-health-states"))
class FakeTimelineObject:
    """One timeline entry: the name/index a feature is addressed by, the `entity` it wraps, the
    health pair a compute verdict reads, the group/suppress/rolled flags, and rollTo() - which
    answers the bool its caller gates on and flips isRolledBack only when it succeeded."""
    def __init__(self, name="Feature1", index=0, entity=None, health=None, message="",
                 is_group=False, suppressed=False, rolled_back=False, parent_group=None,
                 roll_ok=True):
        self.name = name
        self.index = index
        self.entity = entity
        self.healthState = (_api_facts.ENUMS["fusion.FeatureHealthStates"][
            "HealthyFeatureHealthState"] if health is None else health)
        self.errorOrWarningMessage = message
        self.isGroup = is_group
        self.isSuppressed = suppressed
        self.isRolledBack = rolled_back
        self.parentGroup = parent_group
        self._roll_ok = roll_ok
        self._rolls = []

    def rollTo(self, roll_before):
        self._rolls.append(bool(roll_before))
        if self._roll_ok:
            self.isRolledBack = bool(roll_before)
        return self._roll_ok


@fusion_fake(live_type="Timeline", facts=("shape-dump-timeline-world", "basefeature-edit-scope",
                                          "timeline-move-past-either-end-answers-true"))
class FakeTimeline:
    """design.timeline: the counted walk, markerPosition, and the four moves. A move lands the
    marker where it says; a move PAST either end answers TRUE and leaves the marker where it was
    (measured), so the bool is no signal that the marker moved - a caller that needs to know reads
    markerPosition back. `move_ok` False is the move that REFUSES, the only False here. `raises`
    models the open base-feature scope, in which the measured read (design.timeline.count) throws;
    every read here throws with it, so a caller cannot lean on one that was never measured."""
    def __init__(self, items=(), marker=None, raises=None, move_ok=True):
        self._items = list(items)
        self._marker = len(self._items) if marker is None else marker
        self._raises = raises
        self._move_ok = move_ok
        self._moves = []

    def _read(self, value):
        if self._raises:
            raise RuntimeError(self._raises)
        return value

    @property
    def count(self):
        return self._read(len(self._items))

    def item(self, i):
        return self._read(_NamedCollection(self._items).item(i))

    @property
    def markerPosition(self):
        return self._read(self._marker)

    @markerPosition.setter
    def markerPosition(self, value):
        # Settable live, and the roll a relation edit brackets its write with; the assignments land
        # in _moves so a test can read back WHERE the marker was driven, not just where it ended.
        self._moves.append(value)
        self._marker = value

    def _move(self, position):
        if not self._move_ok:
            return False
        self._moves.append(position)
        if 0 <= position <= len(self._items):
            self._marker = position
        return True

    def moveToBeginning(self):
        return self._move(0)

    def moveToEnd(self):
        return self._move(len(self._items))

    def movetoNextStep(self):
        return self._move(self._marker + 1)

    def moveToPreviousStep(self):
        return self._move(self._marker - 1)


@fusion_fake(live_type="Feature",
             facts=("shape-dump-timeline-world", "enum-feature-health-states"))
class FakeFeature:
    """A timeline feature at the surface EVERY feature shares: Feature is abstract and no
    construction returns one (an extrude answers ExtrudeFeature), so this stands for a concrete
    feature read through the base members - name, the health pair, the bodies/faces a result read
    walks, its timelineObject, and deleteMe answering the bool its caller gates on."""
    def __init__(self, name="Extrude1", health=None, message="", bodies=(), faces=(),
                 suppressed=False, timeline_object=None, entity_token=None, delete_ok=True):
        self.name = name
        self.healthState = (_api_facts.ENUMS["fusion.FeatureHealthStates"][
            "HealthyFeatureHealthState"] if health is None else health)
        self.errorOrWarningMessage = message
        self.bodies = _NamedCollection(list(bodies))
        self.faces = _NamedCollection(list(faces))
        self.isSuppressed = suppressed
        self.timelineObject = timeline_object
        # Set only when asked: a feature whose token does not read is its own tested state.
        if entity_token is not None:
            self.entityToken = entity_token
        self._delete_ok = delete_ok
        self._deletes = 0

    def deleteMe(self):
        self._deletes += 1
        return self._delete_ok


@fusion_fake(live_type="BaseFeature",
             facts=("shape-dump-timeline-world", "basefeature-edit-scope"))
class FakeBaseFeature:
    """One base feature and its EDIT SCOPE: startEdit/finishEdit answer the bool the caller gates
    on and open/close the scope, which is what makes this feature invisible to its own collection
    while it is open. _starts/_finishes count the calls, so a scope never opened reads apart from
    one opened and closed."""
    def __init__(self, name="BaseFeature1", start_ok=True, finish_ok=True):
        self.name = name
        self._start_ok, self._finish_ok = start_ok, finish_ok
        self._open = False
        self._starts = self._finishes = 0

    def startEdit(self):
        self._starts += 1
        if self._start_ok:
            self._open = True
        return self._start_ok

    def finishEdit(self):
        self._finishes += 1
        if self._finish_ok:
            self._open = False
        return self._finish_ok


@fusion_fake(live_type="BaseFeatures",
             facts=("shape-dump-timeline-world", "basefeature-edit-scope"))
class FakeBaseFeatures:
    """component.features.baseFeatures: add() answers the new base feature, and a base feature whose
    edit scope is OPEN is INVISIBLE here until finishEdit makes it appear - the measured read is
    count (BEHAVIOR['open_base_feature_hidden']), and item/itemByName walk that same visible set.
    `made` is the base feature add() hands back, for a caller that must hold the one it opens; a
    FALSY `made` is the add that opened no scope, and it joins no walk."""
    def __init__(self, features=(), made=None):
        self._features = list(features)
        self._made = made

    def _visible(self):
        hide = _api_facts.BEHAVIOR["open_base_feature_hidden"]
        return [f for f in self._features if not (hide and getattr(f, "_open", False))]

    @property
    def count(self):
        return len(self._visible())

    def item(self, i):
        return _NamedCollection(self._visible()).item(i)

    def itemByName(self, name):
        return _NamedCollection(self._visible()).itemByName(name)

    def add(self):
        feature = (self._made if self._made is not None
                   else FakeBaseFeature("BaseFeature%d" % (len(self._features) + 1)))
        if feature:
            self._features.append(feature)
        return feature


@fusion_fake(live_type="Features", facts=("shape-dump-timeline-world",))
class FakeFeatures:
    """component.features: the counted/by-name lookup over the features themselves, plus the
    baseFeatures collection a base-feature scope is opened from. A per-kind sub-collection
    (extrudeFeatures, holeFeatures, ...) is attached by the caller after construction."""
    def __init__(self, features=(), base_features=None):
        self._coll = _NamedCollection(list(features))
        self.baseFeatures = FakeBaseFeatures() if base_features is None else base_features

    @property
    def count(self):
        return self._coll.count

    def item(self, i):
        return self._coll.item(i)

    def itemByName(self, name):
        return self._coll.itemByName(name)


@fusion_fake(factory_for="FakeTimeline")
def make_timeline(*names, marker=None, raises=None):
    """A Timeline holding one entry per name, indexed in order - the walk a health rollup reads and
    a marker move steps through. `marker` defaults to the end; `raises` models the open
    base-feature scope."""
    items = [FakeTimelineObject(name=n, index=i) for i, n in enumerate(names)]
    return FakeTimeline(items, marker=marker, raises=raises)


# ── parameter world ───────────────────────────────────────────────────────
#
# The three parameter answers measured by parameter-favorite-maker-text-value-and-fresh-appearances.
# The `in` gate keeps the import working while the generated facts file is one republish behind the
# row that emits the keys; the literal beside each is the fake's standing answer until it lands.
_USER_PARAM_NO_CREATED_BY = (_api_facts.BEHAVIOR["user_parameter_has_no_created_by"]
                             if "user_parameter_has_no_created_by" in _api_facts.BEHAVIOR else True)
_TEXT_PARAM_VALUE_RAISES = (_api_facts.BEHAVIOR["text_parameter_value_raises"]
                            if "text_parameter_value_raises" in _api_facts.BEHAVIOR else True)
_MODEL_PARAM_HAS_MAKER = (_api_facts.BEHAVIOR["model_parameter_created_by_answers_maker"]
                          if "model_parameter_created_by_answers_maker" in _api_facts.BEHAVIOR
                          else True)


@fusion_fake(live_type="UserParameter",
             facts=("shape-dump-timeline-world",
                    "parameter-favorite-maker-text-value-and-fresh-appearances"))
class FakeUserParameter:
    """One user parameter as a parameter row reads it: name, expression, the value in DATABASE units
    (cm/radians), unit, comment and isFavorite, plus deleteMe answering the bool its caller gates
    on. It carries NO createdBy/role while BEHAVIOR["user_parameter_has_no_created_by"] says the
    live type has none, so the model-parameter owner reads decline on it as they do live.

    `text_value` given makes this a TEXT parameter: `value` then RAISES the way the live read does
    (BEHAVIOR["text_parameter_value_raises"]) and textValue carries the string, which is the pair a
    caller falling back from one to the other is judged on. Both flags are read through an `in` gate
    so the module imports while the generated facts file is one republish behind the row."""

    if _USER_PARAM_NO_CREATED_BY:
        createdBy = _absent_member("createdBy")

    def __init__(self, name="d1", expression="10 mm", value=1.0, unit="mm", comment="",
                 favorite=False, delete_ok=True, text_value=None):
        self.name = name
        self.expression = expression
        self._value = value
        self._text = text_value
        self.unit = unit
        self.comment = comment
        self.isFavorite = favorite
        if text_value is not None:
            self.textValue = text_value
        self._delete_ok = delete_ok
        self._deleted = False

    @property
    def value(self):
        if self._text is not None and _TEXT_PARAM_VALUE_RAISES:
            raise RuntimeError("3 : Parameter is not numeric type")
        return self._value

    @value.setter
    def value(self, number):
        self._value = number

    def deleteMe(self):
        if self._delete_ok:
            self._deleted = True
        return self._delete_ok


@fusion_fake(live_type="UserParameters", facts=("shape-dump-timeline-world",))
class FakeUserParameters:
    """design.userParameters: the counted/by-name protocol (itemByName None on a miss) plus add(),
    which answers the new parameter. A parameter whose deleteMe() SUCCEEDED is gone from every read
    here, so a delete read-back sees what live sees."""
    def __init__(self, parameters=()):
        self._parameters = list(parameters)
        self._added = []

    def _live(self):
        return [p for p in self._parameters if not getattr(p, "_deleted", False)]

    @property
    def count(self):
        return len(self._live())

    def item(self, i):
        return _NamedCollection(self._live()).item(i)

    def itemByName(self, name):
        return _NamedCollection(self._live()).itemByName(name)

    def add(self, name, value_input, unit="", comment=""):
        self._added.append((name, value_input, unit, comment))
        expression = getattr(value_input, "stringValue", None)
        param = FakeUserParameter(name=name, unit=unit, comment=comment,
                                  expression=expression if isinstance(expression, str) else "")
        self._parameters.append(param)
        return param


@fusion_fake(live_type="ModelParameter",
             facts=("shape-dump-assembly-world-2",
                    "parameter-favorite-maker-text-value-and-fresh-appearances"))
class FakeModelParameter:
    """One MODEL parameter as a parameter row reads it: the name/expression/value/unit/comment and
    the isFavorite flag every Parameter carries, plus the two members a model parameter answers and
    a user parameter has no equivalent of - `createdBy`, the entity that made it, and `role`, the
    slot it fills on that entity.

    `owner` is what createdBy answers. MEASURED (parameter-favorite-maker-text-value-and-fresh-
    appearances): createdBy on a model parameter never declines and never reads None - each one
    answers the entity that made it, a Sketch for a sketch dimension and an ExtrudeFeature for an
    extrude - so while BEHAVIOR["model_parameter_created_by_answers_maker"] holds (read through an
    `in` gate, the key being one republish out) the default here is a maker rather than nothing. The
    DECLINE belongs to UserParameter, which carries no createdBy member at all; FakeUserParameter is
    that shape.

    `tracks_expression` makes an `expression` ASSIGNMENT recompute `value` in Fusion's DATABASE
    units (cm for a length, radians for an angle) the way a live parameter re-evaluates - the pair a
    caller writing an expression and re-reading the value is judged on. Without it `expression` is a
    plain attribute and `value` stays where it was put, which is the swallowed write."""

    _DATABASE_UNITS = {"mm": 0.1, "cm": 1.0, "in": 2.54, "deg": math.pi / 180.0}

    def __init__(self, name="d195", owner=None, role="Distance", expression="5 mm",
                 value=0.5, unit="mm", comment="", favorite=False, tracks_expression=False):
        self.name = name
        self._tracks = tracks_expression
        self._expression = expression
        self.value = value
        self.unit = unit
        self.comment = comment
        self.isFavorite = favorite
        self.role = role
        if owner is not None:
            self.createdBy = owner
        elif _MODEL_PARAM_HAS_MAKER:
            self.createdBy = FakeFeature("Extrude1")

    @property
    def expression(self):
        return self._expression

    @expression.setter
    def expression(self, text):
        self._expression = text
        if self._tracks:
            number, _, unit = (text or "").strip().rpartition(" ")
            self.value = float(number) * FakeModelParameter._DATABASE_UNITS[unit]
