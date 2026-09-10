# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The appearance world: the library catalog, an appearance and its colour channels."""

from tests.fakes.scaffold import _NamedCollection, fusion_fake


@fusion_fake(live_type="Color", facts=("shape-dump-appearance-world",))
class FakeColor:
    """adsk.core.Color: the four components a colour write is compared on. create() is the live
    factory's (r, g, b, opacity) order."""

    def __init__(self, red=0, green=0, blue=0, opacity=255):
        self.red, self.green, self.blue, self.opacity = red, green, blue, opacity

    @classmethod
    def create(cls, r, g, b, o):
        return cls(r, g, b, o)


@fusion_fake(live_type="ColorProperty", facts=("shape-dump-appearance-world",))
class ColorProperty:
    """One colour channel of an appearance. Named for the live class because appearance_set picks
    the channel to write by ``type(p).__name__``.

    ``swallows`` is the channel that ACCEPTS the assignment and keeps its prior colour and
    ``read_raises`` the one whose value GETTER declines - the two shapes only a read-back tells
    from a landed colour, both DECLARED worst cases."""

    def __init__(self, prop_id, value=None, swallows=False, read_raises=None, read_only=False):
        object.__setattr__(self, "_swallows", swallows)
        object.__setattr__(self, "_read_raises", read_raises)
        object.__setattr__(self, "_read_only", read_only)
        object.__setattr__(self, "_value", value)
        self.id = prop_id
        self.name = prop_id

    @property
    def value(self):
        if self._read_raises:
            raise RuntimeError(self._read_raises)
        return self._value

    @value.setter
    def value(self, colour):
        if self._read_only:
            raise RuntimeError("this ColorProperty is texture-backed")
        if not self._swallows:
            object.__setattr__(self, "_value", colour)


@fusion_fake(live_type="Properties", facts=("shape-dump-appearance-world",))
class _Properties(_NamedCollection):
    """Appearance.appearanceProperties - the collection type the live read answers (MEASURED: it is
    a Properties; adsk carries no AppearanceProperties type at all)."""


@fusion_fake(live_type="Appearance", facts=("shape-dump-appearance-world",))
class FakeAppearance:
    """One appearance asset. ``id`` and ``name`` are independent axes because live NEITHER
    identifies an instance: two assets can share a name, and a copy KEEPS its source asset's id, so
    every override minted from one base shares that id and differs only by name.

    ``color_props`` names the ColorProperty ids the asset exposes, in order; pass ready-made
    ColorProperty objects for a channel that swallows or will not read."""

    def __init__(self, name="Appearance", color_props=("opaque_albedo",), appearance_id=None,
                 extra_props=()):
        self.name = name
        self.id = appearance_id if appearance_id is not None else f"asset:{name}"
        props = [p if isinstance(p, ColorProperty) else ColorProperty(p) for p in color_props]
        self.appearanceProperties = _Properties(props + list(extra_props))


@fusion_fake(live_type="Appearances", facts=("shape-dump-appearance-world",))
class FakeAppearances(_NamedCollection):
    """A design's or a library's appearances; each copy is recorded on the private _copied walk as
    (source, name, made).

    A name the collection already holds RAISES '3 : appearance name already exists in document' and
    adds nothing (measure row appearance-duplicate-name-raises-and-occurrence-write-fans-out); every
    caller here reaches addByCopy through safe(), which turns that raise into the None its
    look-up-first reuse path re-checks on. The copy keeping its source's id is measured by
    shape-dump-appearance-world."""

    def __init__(self, items=()):
        super().__init__(items)
        self._copied = []

    def addByCopy(self, base, name):
        if self.itemByName(name) is not None:
            raise RuntimeError("3 : appearance name already exists in document")
        # A copy carries its SOURCE's channels, so an appearance minted off a base exposes the same
        # colour ids the base does - which is what an albedo write then looks for.
        colour, other = [], []
        for prop in (getattr(base, "appearanceProperties", None) or ()):
            if isinstance(prop, ColorProperty):
                colour.append(prop.id)
            else:
                other.append(type(prop)())
        made = FakeAppearance(name, color_props=colour or ("opaque_albedo",),
                              appearance_id=getattr(base, "id", None), extra_props=other)
        self._items.append(made)
        self._copied.append((base, name, made))
        return made


@fusion_fake(live_type="MaterialLibrary", facts=("shape-dump-appearance-world",))
class FakeMaterialLibrary:
    """One loaded library: its name and id, plus the two entry collections a catalog read walks.
    Each collection is set only when given, so a library whose entries do not read stays a testable
    state."""

    def __init__(self, name="Library", materials=None, appearances=None, native=True):
        self.name = name
        self.id = f"lib:{name}"
        self.isNative = native
        if materials is not None:
            self.materials = _NamedCollection(list(materials))
        if appearances is not None:
            self.appearances = FakeAppearances(list(appearances))


@fusion_fake(live_type="MaterialLibraries", facts=("shape-dump-appearance-world",))
class FakeMaterialLibraries(_NamedCollection):
    """app.materialLibraries - the loaded-library catalog find_library resolves one name out of."""


@fusion_fake(factory_for="FakeMaterialLibrary")
def make_material_library(name="Library", materials=(), appearances=()):
    """One library holding `materials` (name-only objects) and `appearances` (FakeAppearance)."""
    return FakeMaterialLibrary(name, materials=list(materials), appearances=list(appearances))
