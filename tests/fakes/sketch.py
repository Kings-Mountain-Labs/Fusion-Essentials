# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The sketch world: profiles, the per-kind curve collections, the sketch and its points."""

import types

from tests.fakes.geometry import FakePoint
from tests.fakes.scaffold import _NamedCollection, fusion_fake


@fusion_fake(live_type="Profile", facts=("shape-dump-design-world",))
class Profile:
    """A sketch profile: the boundingBox and profileLoops a region walk reads, the
    areaProperties() a profile handle's locator re-finds it by, and its entityToken. `tag` names
    the instance in a failure message and is not a live member. `centroid` takes a FakePoint or an
    (x, y, z) tuple; `loops` takes loop objects or a bare count."""
    def __init__(self, tag=None, bbox=None, loops=(), area=None, centroid=None,
                 entity_token=None, parent_sketch=None):
        self._tag = tag
        self._area = area
        self._centroid = FakePoint(*centroid) if isinstance(centroid, tuple) else centroid
        self.boundingBox = bbox
        self.profileLoops = _NamedCollection([None] * loops if isinstance(loops, int)
                                             else list(loops))
        self.entityToken = entity_token
        self.parentSketch = parent_sketch

    def __repr__(self):
        return f"Profile({self._tag!r})"

    def areaProperties(self, accuracy=None):
        return types.SimpleNamespace(area=self._area, centroid=self._centroid)


def make_sketch_curve(token="curve0", length=1.0, is_closed=None):
    """One sketch curve: entityToken + length (cm), the pair a curve-edit read-back keys on. Only a
    fitted spline reports isClosed live, so it is set only when given. Attach the in-place edit
    methods a test drives (trim/extend/breakCurve/split) to the returned object."""
    curve = types.SimpleNamespace(entityToken=token, length=length, isConstruction=False)
    if is_closed is not None:
        curve.isClosed = is_closed
    return curve


_CURVE_KINDS = ("sketchLines", "sketchArcs", "sketchCircles", "sketchEllipses",
                "sketchEllipticalArcs", "sketchConicCurves",
                "sketchFittedSplines", "sketchControlPointSplines", "sketchFixedSplines")


@fusion_fake(live_type="SketchCurves", facts=("shape-dump-timeline-world",))
class SketchCurves:
    """A sketch's curves: the per-kind sub-collections a '<type>:<index>' ref indexes, with the flat
    count/item/iteration DERIVED from them - so a curve added to one, and a sub-collection a
    subclass swapped in, are both in the flat walk. A collection-level factory a test drives
    (sketchArcs.addFillet, sketchLines.addDistanceChamfer) is attached to the sub-collection.

    It HOLDS a _NamedCollection rather than being one: live SketchCurves carries no itemByName.
    `_items` is the flat walk's own tail, where a test lands a curve without naming its kind."""
    def __init__(self, lines=(), arcs=(), circles=(), ellipses=(), splines=()):
        self._items = []
        self.sketchLines = _NamedCollection(lines)
        self.sketchArcs = _NamedCollection(arcs)
        self.sketchCircles = _NamedCollection(circles)
        self.sketchEllipses = _NamedCollection(ellipses)
        self.sketchEllipticalArcs = _NamedCollection()
        self.sketchConicCurves = _NamedCollection()
        self.sketchFittedSplines = _NamedCollection(splines)
        self.sketchControlPointSplines = _NamedCollection()
        self.sketchFixedSplines = _NamedCollection()

    def _flat(self):
        # Rebuilt per call, so a kind a subclass swapped in and a curve a factory grew are both in
        # the walk. A curve landed in a kind AND in the tail is still one curve, so the tail
        # contributes only what no kind holds. The shared collection, so the measured
        # out-of-range raise lives in one place.
        kinded = [c for kind in _CURVE_KINDS for c in getattr(self, kind, ())]
        return _NamedCollection(kinded + [c for c in self._items
                                          if not any(c is k for k in kinded)])

    @property
    def count(self):
        return self._flat().count

    def item(self, i):
        return self._flat().item(i)

    def __iter__(self):
        return iter(self._flat())


@fusion_fake(live_type="Sketch",
             facts=("shape-dump-design-world", "sketch-profiles-under-compute-deferred"))
class Sketch:
    """A sketch: its name, sketchCurves, sketchPoints and the parentComponent a feature must be
    built in. `profiles` are the closed regions a blind profiles.item(0) indexes and
    `is_compute_deferred` the flag whose True makes those regions the pre-deferral ones.
    `is_visible` is the browser bulb a tool hides a helper sketch behind."""
    def __init__(self, name="Sketch1", curves=None, points=(), profiles=(),
                 is_compute_deferred=False, parent_component=None, is_visible=True):
        self.name = name
        self.sketchCurves = SketchCurves() if curves is None else curves
        self.sketchPoints = _NamedCollection(points)
        self.profiles = _NamedCollection(profiles)
        self.isComputeDeferred = is_compute_deferred
        self.parentComponent = parent_component
        self.isVisible = is_visible


@fusion_fake(live_type="SketchPoint", facts=("shape-dump-timeline-world",))
class FakeSketchPoint:
    """One point a sketch owns - the 'point:<index>' address space sketchPoints indexes - carrying
    its 2D `geometry`. There is no isConstruction: SketchPoint carries none, so a point never reads
    as construction the way a curve does."""
    def __init__(self, geometry=None):
        self.geometry = geometry


@fusion_fake(factory_for="Sketch")
def make_sketch(name="Sketch1", lines=(), arcs=(), circles=(), ellipses=(), splines=(), points=(),
                profiles=(), is_compute_deferred=False, parent_component=None, is_visible=True):
    """A Sketch whose curves are grouped into the per-kind sub-collections. Members come from
    make_sketch_curve."""
    curves = SketchCurves(lines, arcs, circles, ellipses, splines)
    return Sketch(name, curves, points, profiles, is_compute_deferred, parent_component,
                  is_visible)


def sketch_curves_edit(collection, add=(), remove=()):
    """Apply a curve add/remove to the per-kind sub-collection a '<type>:<index>' ref indexes - the
    one place a sketch curve lives, since the flat sketchCurves walk derives from the kinds."""
    for curve in remove:
        collection._items.remove(curve)
    for curve in add:
        collection._items.append(curve)
