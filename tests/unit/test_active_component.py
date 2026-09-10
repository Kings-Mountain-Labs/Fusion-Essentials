"""Unit tests for active-component targeting in sketch_create.py + model_extrude.py.

A component made active via model_create_component(activate=true) must receive new
sketches/bodies, not the root component - hardcoding design.rootComponent would leak geometry
into root and leave the activated component empty. Both tools route through the shared
target_component(design) helper, so each test here DRIVES THE HANDLER and reads which component
the new sketch / extrude feature actually landed in: the collections of BOTH components record
what they were handed, and root's staying empty is asserted beside the active one filling. The
helper's own behaviour (active wins, root fallback) is pinned in test_common.py.
"""

import types

import pytest

from conftest import MakeComp, MakeDesign, install, load_tool, payload

sk = load_tool("sketch_create")
ex = load_tool("model_extrude")


class _RecordingSketches:
    """A component's `sketches`: add(plane) lands a sketch and RECORDS it, so which component
    received the new sketch is readable after the handler returns."""

    def __init__(self, owner):
        self.owner = owner
        self.landed = []

    @property
    def count(self):
        return len(self.landed)

    def item(self, i):
        return self.landed[i]

    def itemByName(self, name):
        for s in self.landed:
            if s.name == name:
                return s
        return None

    def __iter__(self):
        return iter(self.landed)

    def add(self, plane):
        sketch = types.SimpleNamespace(name=f"{self.owner}Sketch{len(self.landed) + 1}",
                                       referencePlane=plane)
        self.landed.append(sketch)
        return sketch


class _RecordingExtrudes:
    """A component's `features.extrudeFeatures`: createInput/add record the component that built
    the feature, which is the only way to see WHERE an extrude landed."""

    def __init__(self, owner):
        self.owner = owner
        self.landed = []

    def createInput(self, profile, operation):
        return types.SimpleNamespace(profile=profile, operation=operation, isSolid=True,
                                     participantBodies=None,
                                     setDistanceExtent=lambda symmetric, distance: True)

    def add(self, inp):
        feature = types.SimpleNamespace(name=f"{self.owner}Extrude{len(self.landed) + 1}",
                                        isSolid=True,
                                        bodies=_body_collection(f"{self.owner}Body1"))
        self.landed.append(feature)
        return feature


def _body_collection(name):
    body = types.SimpleNamespace(name=name, isSolid=True, volume=1.0)
    return types.SimpleNamespace(count=1, item=lambda i, b=body: b)


def _origin_planes(owner):
    """One component's three origin planes, each named for its owner - the read that tells a sketch
    built in the ACTIVE component's frame from one built in root's."""
    return tuple(types.SimpleNamespace(name=f"{owner} {axes}") for axes in ("XY", "XZ", "YZ"))


def _two_component_design(build):
    """A design whose ACTIVE component is not its root, with `build(name)` attaching the recorder
    the tool under test writes through to each. Returns (design, root, active)."""
    root = MakeComp("Root", origin_planes=_origin_planes("Root"))
    active = MakeComp("Mast", origin_planes=_origin_planes("Mast"))
    for comp in (root, active):
        build(comp)
    design = MakeDesign(comp=root, all_components=[root, active])
    design.activeComponent = active
    return design, root, active


@pytest.fixture
def sketch_design(monkeypatch):
    """sketch_create's world: both components carry an xy origin plane and a recording sketches
    collection, and 'Mast' is the active one."""
    def build(comp):
        comp.sketches = _RecordingSketches(comp.name)

    design, root, active = _two_component_design(build)
    install(sk, design)
    # The frame is not what this file pins, so it is stubbed out - taking whatever the handler
    # hands it (the sketch, the design being read), so the stub cannot pass by swallowing a
    # signature mismatch.
    monkeypatch.setattr(sk, "sketch_world_frame", lambda *a, **k: None)
    return design, root, active


@pytest.fixture
def extrude_design():
    """model_extrude's world: both components carry a recording extrudeFeatures collection. The
    sketch is owned by NEITHER (its parentComponent does not read), which is the case that leaves
    target_component as the host - the seam this file pins."""
    profile = types.SimpleNamespace()
    sketch = types.SimpleNamespace(name="S1",
                                   profiles=types.SimpleNamespace(count=1,
                                                                  item=lambda i, p=profile: p),
                                   sketchCurves=types.SimpleNamespace(count=0))

    def build(comp):
        comp.sketches = MakeComp(comp.name).sketches
        comp.features = types.SimpleNamespace(extrudeFeatures=_RecordingExtrudes(comp.name))

    design, root, active = _two_component_design(build)
    active.sketches._items.append(sketch)
    install(ex, design)
    return design, root, active


class TestSketchCreateTargetsActiveComponent:
    def test_new_sketch_lands_in_the_active_component_not_root(self, sketch_design):
        _design, root, active = sketch_design
        res = sk.handler(plane="xy")
        assert payload(res)["sketch_name"] == "MastSketch1"
        assert [s.name for s in active.sketches.landed] == ["MastSketch1"]
        assert root.sketches.landed == [], "the sketch leaked into the root component"

    def test_the_plane_comes_from_the_active_component_too(self, sketch_design):
        # Each component owns its OWN xy origin plane, so a sketch built on root's plane is a
        # sketch in the wrong frame even when it lands in the right collection.
        _design, _root, active = sketch_design
        res = sk.handler(plane="xy")
        assert payload(res)["plane"] == "Mast XY"
        assert active.sketches.landed[0].referencePlane is active.xYConstructionPlane


class TestExtrudeTargetsActiveComponent:
    def test_new_feature_lands_in_the_active_component_not_root(self, extrude_design):
        _design, root, active = extrude_design
        res = ex.handler(sketch_name="S1", profile_index=0, distance=10)
        assert payload(res)["feature"] == "MastExtrude1"
        assert [f.name for f in active.features.extrudeFeatures.landed] == ["MastExtrude1"]
        assert root.features.extrudeFeatures.landed == [], (
            "the extrude was built into the root component")
