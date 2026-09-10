# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The mesh world: a MeshBody and its two meshes, the component collection, and the calculator."""

import types

import live_api_facts as _api_facts

from tests.fakes.scaffold import _EntityProxy, _NamedCollection, _absent_member, fusion_fake


@fusion_fake(live_type="TriangleMesh", facts=("shape-dump-mesh-world",))
class _FakeTriangleMesh:
    """MeshBody.displayMesh - the triangle/vertex census a mesh feature is judged on. Held as ONE
    object per body so a feature fake can mutate the counts in place the way a real repair does.

    The three flat arrays a tessellation hands to addByTriangleMeshData are set only when given, so
    a display mesh whose geometry does not read stays a testable state."""
    def __init__(self, tri, nodes, coords=None, node_indices=None, normals=None):
        self.triangleCount = tri
        self.nodeCount = nodes
        if coords is not None:
            self.nodeCoordinatesAsDouble = list(coords)
        if node_indices is not None:
            self.nodeIndices = list(node_indices)
        if normals is not None:
            self.normalVectorsAsDouble = list(normals)


@fusion_fake(factory_for="_NamedCollection")
def make_face_groups(spec):
    """A MeshBody.faceGroups collection: an int builds that many groups with tempIds 1..n, a
    sequence names the tempIds. The tempId is the read a caller compares across a generation."""
    ids = list(range(1, spec + 1)) if isinstance(spec, int) else list(spec)
    return _NamedCollection([types.SimpleNamespace(tempId=t) for t in ids])


@fusion_fake(live_type="PolygonMesh", facts=("shape-dump-mesh-world",))
class _FakePolygonMesh:
    """MeshBody.mesh - the flat x,y,z node coordinates a smooth moves and the flat normal
    components a reverse negates, plus the polygon/node census a mesh record publishes."""
    def __init__(self, coords, normals, polygons=None, nodes=None):
        self.nodeCoordinatesAsDouble = list(coords)
        self.normalVectorsAsDouble = list(normals)
        if polygons is not None:
            self.polygonCount = polygons
        if nodes is not None:
            self.nodeCount = nodes


@fusion_fake(live_type="MeshBody",
             facts=("shape-dump-mesh-world", "meshbody-volume-open-returns-zero",
                    "meshbody-assembly-context-proxy",
                    "meshbody-delete-answers-true-and-removes",
                    "meshbody-facegroups-counted-before-generation",
                    "facegroup-generation-reads-carry-no-verdict",
                    "meshbody-area-and-boundingbox-open-mesh"))
class MeshBody:
    """Matches type(entity).__name__ == 'MeshBody' - the shared mesh fake every mesh-feature tool
    test builds on (repair, shell, smooth, separate, reverse_normal).

    `volume` on a mesh that is NOT closed RETURNS 0.0 rather than raising - that reading comes from
    live_api_facts.BEHAVIOR["meshbody_volume_open_raises"], so the platform fact lives in one place
    and a change to it flips every mesh test at once. An open mesh encloses nothing, so the 0.0 is
    the API's ANSWER: it is comparable at both ends of a repair, it carries no sign for a reverse to
    flip, and it makes a percentage undefined for a smooth.

    `isValid` stays True after a feature consumes the body - the measured lie - so a handler that
    trusts it instead of a fresh collection walk cannot pass on that flag; ``go_stale`` drops the
    identity reads a consumed body no longer answers.

    Every harness knob is PRIVATE, so the public surface is only what a live MeshBody answers and
    test_fake_shapes_exist sweeps it against the measured dump. The readability switches model the
    reads that fail INDEPENDENTLY, each one making exactly one published field null: `_dead` (an
    invalidated wrapper - every read raises), `_counts_readable` (displayMesh), `_volume_readable`,
    `_closed_readable`, `_mesh_readable` (the PolygonMesh behind both the coordinates and the
    normals). A test flips one on the instance to break that read mid-flight.

    `area` (cm2), `bbox` and the polygon-mesh census `polygons`/`mesh_nodes` are set only when given,
    so a mesh whose surface area, box or polygon census does not read stays a testable state; live,
    all of them read on an OPEN mesh too. `face_groups` DEFAULTS to the ONE group whose tempId reads
    0 that a mesh publishes before anything segments it (measured); an int builds that many groups
    with tempIds 1..n, a sequence names the tempIds outright, and None is the mesh whose faceGroups
    does not read at all. `deletes` is what deleteMe() answers - see the method."""
    def __init__(self, name="Scan1", tri=12, nodes=8, is_closed=True, volume=1.0, coords=(),
                 normals=(), token=None, parent=None, counts_readable=True, volume_readable=True,
                 closed_readable=True, mesh_readable=True, lifts=True, area=None, bbox=None,
                 face_groups=(0,), polygons=None, mesh_nodes=None, deletes=True):
        self.name = name
        self.isValid = True
        self._polygons = polygons
        self._mesh_nodes = mesh_nodes
        self._deletes = deletes
        if area is not None:
            self.area = area
        if bbox is not None:
            self.boundingBox = bbox
        if face_groups is not None:
            self.faceGroups = make_face_groups(face_groups)
        # A NATIVE body reads assemblyContext None and nativeObject None; createForAssemblyContext
        # mints the proxy that answers both. `lifts` False models a lift handing nothing back.
        self.assemblyContext = None
        self.nativeObject = None
        self._lifts = lifts
        self._dead = False
        self._display = _FakeTriangleMesh(tri, nodes)
        self._is_closed = is_closed
        self._volume_cm3 = volume
        self._coords = list(coords)
        self._normals = list(normals)
        self._counts_readable = counts_readable
        self._volume_readable = volume_readable
        self._closed_readable = closed_readable
        self._mesh_readable = mesh_readable
        self.entityToken = token or f"MTOK::{name}"
        self.parentComponent = parent

    def _live(self):
        if self._dead:
            raise RuntimeError("3 : object is no longer valid")

    @property
    def displayMesh(self):
        self._live()
        if not self._counts_readable:
            raise RuntimeError("3 : the display mesh is unavailable")
        return self._display

    @property
    def mesh(self):
        self._live()
        if not self._mesh_readable:
            raise RuntimeError("3 : the polygon mesh is unavailable")
        return _FakePolygonMesh(self._coords, self._normals, self._polygons, self._mesh_nodes)

    @property
    def isClosed(self):
        self._live()
        if not self._closed_readable:
            raise RuntimeError("3 : the watertight flag of this body is unavailable")
        return self._is_closed

    @isClosed.setter
    def isClosed(self, value):
        self._is_closed = value

    @property
    def isOriented(self):
        self._live()
        return True

    @property
    def volume(self):
        self._live()
        if not self._volume_readable:
            raise RuntimeError("3 : the volume of this body is unavailable")
        if self._is_closed:
            return self._volume_cm3
        if _api_facts.BEHAVIOR["meshbody_volume_open_raises"]:
            raise RuntimeError("3 : the volume of this body is unavailable")
        return 0.0

    def createForAssemblyContext(self, occurrence):
        """This body IN `occurrence` - a _MeshProxy VIEW onto it (measured
        meshbody-assembly-context-proxy / meshbody-proxy-token-differs), or None when `lifts`
        is off."""
        return _MeshProxy(self, occurrence) if self._lifts else None

    def deleteMe(self):
        """Drop this body from its parent component's meshBodies, answering the bool live returns.

        `deletes` False is a DECLARED state, not a measured one: it stands for a delete that reports
        failure and leaves the body in the collection, so mesh_delete's refusal branch has something
        to run against. Every construction measure_api probes answers True and removes the body
        (meshbody-delete-answers-true-and-removes), and an already-deleted wrapper RAISES."""
        if not self._deletes:
            return False
        items = getattr(getattr(self.parentComponent, "meshBodies", None), "_items", None)
        if items is not None and self in items:
            items.remove(self)
        return True


@fusion_fake(live_type="MeshBody", facts=("shape-dump-mesh-world",
                                          "meshbody-assembly-context-proxy",
                                          "meshbody-proxy-token-differs"))
class _MeshProxy(_EntityProxy):
    """See ``MeshBody.createForAssemblyContext``. A VIEW onto the native: every read but the three
    that make a proxy a proxy goes to it, so a flip on the native (deleteMe, isClosed, a readability
    switch) is visible through this proxy the way it is live."""

    def __init__(self, native, occurrence=None):
        _EntityProxy.__init__(self, native)          # explicit: zero-arg super() and __class__ clash
        # One token PER PLACEMENT, MEASURED: a proxy's token differs from its native's and from the
        # other placement's, so a handle minted in one instance cannot resolve to the other.
        object.__setattr__(self, "_token", f"MPROXY::{getattr(occurrence, 'name', None)}"
                                           f"::{getattr(native, 'entityToken', None)}")
        object.__setattr__(self, "_occurrence", occurrence)

    @property
    def __class__(self):
        # A proxy IS a MeshBody live, and mesh code isinstance-checks adsk.fusion.MeshBody.
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


class _MeshBodies(_NamedCollection):
    """A component's meshBodies: count / item(i) / iteration, and - unlike bRepBodies - NO
    itemByName, so a mesh is resolved by iterate-and-match. The lookup is dropped from this class
    exactly while BEHAVIOR["meshbodies_has_itembyname"] says the live collection lacks it, so a
    resolver that reaches for it fails here the way it fails live."""

    if not _api_facts.BEHAVIOR["meshbodies_has_itembyname"]:
        itemByName = _absent_member("itemByName")


# -- the mesh calculator: the tessellation a BRep body is saved as a mesh through ------------


# setQuality writes surfaceTolerance alone and leaves the other three knobs at zero (measured on
# the mesh-calculator row).
_SET_QUALITY_WRITES_SURFACE_TOLERANCE_ONLY = (
    _api_facts.BEHAVIOR["mesh_set_quality_writes_surface_tolerance_only"]
    if "mesh_set_quality_writes_surface_tolerance_only" in _api_facts.BEHAVIOR else True)


@fusion_fake(live_type="TriangleMeshCalculator", facts=("shape-dump-mesh-calculator-quality",))
class FakeTriangleMeshCalculator:
    """meshManager.createMeshCalculator(): the level-of-detail knob and the tessellation itself.

    MEASURED: a FRESH calculator reads all four tolerance knobs as 0.0 - it carries none of its own
    - and setQuality answers True and writes surfaceTolerance ALONE, leaving the other three at
    zero. What number it lands there is the BODY's, so `tolerance` is this fake's stand-in for it.
    ``quality_ok`` False is the quality the platform declines, and ``calculate_raises`` the message
    a degenerate body's tessellation throws with; the requested member lands on the private
    _quality."""

    maxNormalDeviation = 0.0
    maxAspectRatio = 0.0
    maxSideLength = 0.0

    def __init__(self, mesh=None, quality_ok=True, calculate_raises=None, tolerance=0.01):
        self.surfaceTolerance = 0.0
        self._mesh = mesh
        self._quality_ok = quality_ok
        self._calculate_raises = calculate_raises
        self._tolerance = tolerance
        self._quality = None

    def setQuality(self, quality):
        if not self._quality_ok:
            return False
        self._quality = quality
        self.surfaceTolerance = self._tolerance
        if not _SET_QUALITY_WRITES_SURFACE_TOLERANCE_ONLY:
            self.maxNormalDeviation = self.maxAspectRatio = self.maxSideLength = self._tolerance
        return True

    def calculate(self):
        if self._calculate_raises:
            raise RuntimeError(self._calculate_raises)
        return self._mesh


@fusion_fake(live_type="MeshManager", facts=("shape-dump-mesh-calculator-quality",))
class FakeMeshManager:
    """BRepBody.meshManager. createMeshCalculator() hands out the ONE calculator this manager owns
    (a fresh one when none was given), so a caller can read back what the tessellation was run at;
    passing calculator=None is the factory that ANSWERS NOTHING."""

    _UNSET = object()

    def __init__(self, calculator=_UNSET):
        self._calculator = (FakeTriangleMeshCalculator() if calculator is FakeMeshManager._UNSET
                            else calculator)

    def createMeshCalculator(self):
        return self._calculator


@fusion_fake(factory_for="FakeMeshManager")
def make_mesh_manager(mesh=None, quality_ok=True, calculate_raises=None):
    """A meshManager whose calculator answers `mesh` - the pair save_as_mesh tessellates through."""
    return FakeMeshManager(FakeTriangleMeshCalculator(mesh, quality_ok=quality_ok,
                                                      calculate_raises=calculate_raises))
