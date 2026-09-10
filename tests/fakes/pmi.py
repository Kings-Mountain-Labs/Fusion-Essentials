# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The PMI world: annotations, the two authoring collections, segments and values."""

from tests.fakes.design import MakeComp
from tests.fakes.scaffold import _NamedCollection, fusion_fake


@fusion_fake(live_type="PMISegmentVector", facts=("shape-dump-pmi-world",))
class FakePMISegmentVector:
    """A note's `segments`: a SWIG vector, which answers size() where a Python list answers len()
    and carries none of a list's own count/index/sort/reverse/extend/remove/copy.

    The items are held PRIVATELY for exactly that reason - a list SUBCLASS would hand those
    inherited names out past the shape sweep, which reads a conftest class's own body and cannot see
    what `list` brings. `count` is the hazard: on a list it is a bound METHOD, so a caller reading
    it as a collection count gets a truthy object instead of the number the live vector has no way
    to give at all."""

    def __init__(self, segments=()):
        self._segments = list(segments)

    def size(self):
        return len(self._segments)

    def __len__(self):
        return len(self._segments)

    def __iter__(self):
        return iter(self._segments)

    def __getitem__(self, index):
        return self._segments[index]


@fusion_fake(live_type="PMITextSegment", facts=("shape-dump-pmi-world",))
class FakePMITextSegment:
    """One text run of a note: the `text` and the objectType suffix segments_markup dispatches on.
    create() takes the text, the document-free factory the live class carries."""

    def __init__(self, text=""):
        self.text = text
        self.objectType = "adsk::fusion::PMITextSegment"

    @classmethod
    def create(cls, text=""):
        return cls(text)


@fusion_fake(live_type="PMISymbolSegment", facts=("shape-dump-pmi-world",))
class FakePMISymbolSegment:
    """One {symbol} run: the PMISymbolTypes member segments_markup re-encodes back into a token."""

    def __init__(self, symbol_type=None):
        self.pmiSymbolType = symbol_type
        self.objectType = "adsk::fusion::PMISymbolSegment"

    @classmethod
    def create(cls, symbol_type=None):
        return cls(symbol_type)


@fusion_fake(live_type="PMILineBreakSegment", facts=("shape-dump-pmi-world",))
class FakePMILineBreakSegment:
    """The newline between two lines of a note. create() takes NO argument (measured)."""

    def __init__(self):
        self.objectType = "adsk::fusion::PMILineBreakSegment"

    @classmethod
    def create(cls):
        return cls()


@fusion_fake(live_type="PMIGeometricValueTolerance", facts=("shape-dump-pmi-world",))
class FakePMIGeometricValueTolerance:
    """The bounds on one hole/thread callout value.

    Every set*() records (name, args) on the private _calls walk and answers `accept` - the bool
    build_tolerance gates on, so `accept=False` is the platform declining the construction. The
    read-back side is what tolerance_record publishes: a bound is present only when this fake was
    given it, so an absent half reads its has* flag False rather than a zero that looks measured.
    create() takes NO argument (measured)."""

    def __init__(self, accept=True, upper=None, lower=None, hole_fit=None, shaft_fit=None):
        object.__setattr__(self, "_calls", [])
        object.__setattr__(self, "_accept", accept)
        self.toleranceType = 0
        self.hasUpperTolerance = upper is not None
        self.hasLowerTolerance = lower is not None
        self.upperTolerance = 0.0 if upper is None else upper
        self.lowerTolerance = 0.0 if lower is None else lower
        self.hasToleranceClass = hole_fit is not None
        self.hasShaftToleranceClass = shaft_fit is not None
        self.toleranceClassDeviation = (hole_fit or " ")[:1].strip()
        self.toleranceClassGrade = (hole_fit or " ")[1:].strip()
        self.shaftToleranceClassDeviation = (shaft_fit or " ")[:1].strip()
        self.shaftToleranceClassGrade = (shaft_fit or " ")[1:].strip()
        self.hasTolerances = any(v is not None for v in (upper, lower, hole_fit, shaft_fit))

    @classmethod
    def create(cls):
        return cls()

    def __getattr__(self, name):
        # Every setSymmetric/setDeviation/setLimits*/setMAX/setMIN answers one bool and records the
        # call, so the codec's choice of setter is what a test reads rather than a stored value.
        if name.startswith("set"):
            def call(*args):
                self._calls.append((name, args))
                return self._accept
            return call
        raise AttributeError(name)


@fusion_fake(factory_for="FakePMIGeometricValueTolerance")
def make_pmi_tolerance(upper=None, lower=None, hole_fit=None, shaft_fit=None):
    """A tolerance already carrying bounds - `hole_fit`/`shaft_fit` as one string ('H7', 'h6'),
    split into the deviation letter and the grade the live object reads them back as."""
    return FakePMIGeometricValueTolerance(upper=upper, lower=lower, hole_fit=hole_fit,
                                          shaft_fit=shaft_fit)


@fusion_fake(live_type="PMIGeometricValue", facts=("shape-dump-pmi-world",))
class FakePMIGeometricValue:
    """One numeric field of a hole/thread callout, in DATABASE units (cm; radians for an angle).
    `has_value` False is the field carrying no number - the state value_record publishes nothing
    for, and the one a zero would be mistaken for. create() takes NO argument (measured)."""

    def __init__(self, value=0.0, tolerance=None, overridden=False, has_value=True):
        self.hasValue = has_value
        self.value = value
        self.isOverriddenValue = overridden
        self.tolerance = tolerance

    @classmethod
    def create(cls):
        return cls()


@fusion_fake(factory_for="FakePMIGeometricValue")
def make_pmi_value(value=0.0, tolerance=None, overridden=False, has_value=True):
    """A hole-callout value holding `value` (cm, or radians for an angle) and its bounds."""
    return FakePMIGeometricValue(value, tolerance, overridden, has_value)


@fusion_fake(live_type="PMIDisplaySettings", facts=("shape-dump-pmi-world",))
class FakePMIDisplaySettings:
    """A callout's number formatting - precision, the PMIUnitTypes member, and the three bools
    display_record publishes. A fresh one reads None on each, so a knob nothing wrote is visibly
    unset rather than a default that looks measured. create() takes NO argument (measured)."""

    def __init__(self, precision=None, unit_type=None, leading_zeros=None, trailing_zeros=None,
                 unit_abbreviation=None):
        self.precision = precision
        self.unitType = unit_type
        self.hasLeadingZeros = leading_zeros
        self.hasTrailingZeros = trailing_zeros
        self.hasUnitAbbreviation = unit_abbreviation

    @classmethod
    def create(cls):
        return cls()


class _NoteBase:
    """The members a leader note and a hole/thread note BOTH carry: identity, the health flags
    annotation_record publishes, the leader/text format knobs every read-back gate writes, and the
    delete/refresh pair.

    ``declines`` names the properties whose WRITE raises and ``swallows`` the ones whose write is
    dropped while the read keeps answering its old value - the two platform shapes the read-back
    gates exist for, both DECLARED worst cases rather than measured ones. ``object_type`` overrides
    the objectType suffix, which is how the kind label is driven (an imported kind has no shape dump
    of its own, so a fake standing for one carries this surface and that label).

    Not a live type itself: the two classes below are what the shape sweep runs on."""

    _SUFFIX = "PMILeaderLineNote"

    def __init__(self, name="Note1", object_type=None, text="TEXT", visible=True,
                 out_of_date=False, suppressed=False, warning="", segments=None,
                 leader_extension=None, perpendicular=False, deletable=True, delete_ok=True,
                 valid=None, declines=(), swallows=(), plane=None, text_point=None,
                 light_bulb_on=True, up_to_date_ok=True):
        object.__setattr__(self, "_declines", set())
        object.__setattr__(self, "_swallows", set())
        self.name = name
        self.objectType = object_type or ("adsk::fusion::" + self._SUFFIX)
        self.plainText = text
        self.isVisible = visible
        self.isOutOfDate = out_of_date
        self.isSuppressed = suppressed
        self.errorOrWarningMessage = warning
        self.isLightBulbOn = light_bulb_on
        self.isDeletable = deletable
        self.isPerpendicularLine = perpendicular
        self.horizontalAlignment = None
        self.verticalAlignment = None
        self.leaderLineExtension = leader_extension
        self.segments = segments
        self.plane = plane
        self.annotationTextPoint = text_point
        self.annotationTargetPoint = None
        if valid is not None:
            self.isValid = valid
        object.__setattr__(self, "_delete_ok", delete_ok)
        object.__setattr__(self, "_deleted", False)
        object.__setattr__(self, "_up_to_date_ok", up_to_date_ok)
        object.__setattr__(self, "_declines", set(declines))
        object.__setattr__(self, "_swallows", set(swallows))

    def __setattr__(self, key, value):
        if key in getattr(self, "_declines", ()):
            raise RuntimeError(f"'{key}' is read-only on this annotation")
        if key in getattr(self, "_swallows", ()):
            return
        object.__setattr__(self, key, value)

    def _seed(self, key, value):
        """Set a member as the platform's OWN state, past the declines/swallows gates."""
        object.__setattr__(self, key, value)

    def deleteMe(self):
        # A delete the platform REFUSES answers False and leaves the annotation in place.
        if self._delete_ok:
            object.__setattr__(self, "_deleted", True)
        return self._delete_ok

    def markUpToDate(self):
        # The bool and the flag are separate answers: `up_to_date_ok` False leaves isOutOfDate set,
        # which is the state a caller trusting the bool alone reports as refreshed.
        if self._up_to_date_ok:
            self.isOutOfDate = False
        return self._up_to_date_ok


@fusion_fake(live_type="PMILeaderLineNote", facts=("shape-dump-pmi-world",))
class FakePMILeaderLineNote(_NoteBase):
    """A Fusion-authored leader note: the shared note surface plus its own annotation-plane and
    target-point writers. ``plane_ok`` False is the setAnnotationPlane the platform DECLINES (a
    bool, not a raise) and ``target_ok`` False the same for setAnnotationTargetPoint; both record
    what they were handed on the private _plane_calls / _target_calls walks."""

    _SUFFIX = "PMILeaderLineNote"

    def __init__(self, *args, plane_ok=True, target_ok=True, **kwargs):
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "_plane_ok", plane_ok)
        object.__setattr__(self, "_target_ok", target_ok)
        object.__setattr__(self, "_plane_calls", [])
        object.__setattr__(self, "_target_calls", [])

    def setAnnotationPlane(self, *args):
        self._plane_calls.append(args)
        return self._plane_ok

    def setAnnotationTargetPoint(self, point):
        self._target_calls.append(point)
        if self._target_ok:
            self.annotationTargetPoint = point
        return self._target_ok


@fusion_fake(live_type="PMIHoleThreadNote", facts=("shape-dump-pmi-world",))
class FakePMIHoleThreadNote(_NoteBase):
    """A Fusion-authored hole/thread callout: the shared note surface plus the flags and the
    PMIGeometricValue fields apply_hole_flags / apply_hole_values write and read back.

    ``values`` is {wire key -> FakePMIGeometricValue} and installs only the fields it names, since a
    field a hole shape does not carry is the state apply_hole_values refuses on. The bools and the
    display settings are set only when a test asks, for the same reason."""

    _SUFFIX = "PMIHoleThreadNote"

    def __init__(self, *args, values=None, flags=None, quantity=None, display=None, **kwargs):
        super().__init__(*args, **kwargs)
        # _seed, not setattr: a field named in `swallows` is seeded as the platform's own state and
        # only the WRITE that follows is dropped.
        for key, value in dict(values or {}).items():
            self._seed(_pmi_value_attr(key), value)
        for key, value in dict(flags or {}).items():
            self._seed(key, value)
        if quantity is not None:
            self.quantity = quantity
        if display is not None:
            self.primaryDisplaySettings = display


# wire key -> the PMIHoleThreadNote property carrying it, mirroring _pmi.HOLE_VALUE_ATTR so a hole
# fake is seeded in the same vocabulary the tool writes through.
_HOLE_VALUE_ATTR = {"diameter": "diameter", "radius": "radius", "depth": "depth",
                    "counterbore_diameter": "counterboreDiameter",
                    "counterbore_radius": "counterboreRadius",
                    "counterbore_depth": "counterboreDepth",
                    "countersink_diameter": "countersinkDiameter",
                    "countersink_angle_deg": "countersinkAngle",
                    "thread_depth": "threadDepth"}


def _pmi_value_attr(key):
    """The hole-note property a wire value key names, the key itself when it is already one."""
    return _HOLE_VALUE_ATTR.get(key, key)


# The default add() result: the collection's own note. An explicit add_result= (None included) is
# what a test asks for instead, so "add created nothing" stays a distinct, requestable state.
_ABSENT_ADD_RESULT = object()


@fusion_fake(live_type="PMILeaderLineNotes", facts=("shape-dump-pmi-world",))
class FakePMILeaderLineNotes(_NamedCollection):
    """component.pmiAnnotations.leaderLineNotes: createInput(target) hands out `note_input` and
    records the target on it, and add(input) LANDS `note` in this collection and answers it - so a
    caller's count read after the add grows the way the live one does. ``add_result`` None is the
    add that creates nothing; the input and the added object are the private _input / _added."""

    def __init__(self, items=(), note=None, note_input=None, add_result=_ABSENT_ADD_RESULT):
        super().__init__(items)
        self._note = note if note is not None else FakePMILeaderLineNote()
        self._input = note_input if note_input is not None else FakePMILeaderLineNoteInput()
        self._added = None
        self._add_result = add_result

    def createInput(self, target):
        self._input.target = target
        return self._input

    def add(self, note_input):
        self._added = note_input
        made = self._note if self._add_result is _ABSENT_ADD_RESULT else self._add_result
        if made is not None:
            self._items.append(made)
        return made


@fusion_fake(live_type="PMIHoleThreadNotes", facts=("shape-dump-pmi-world",))
class FakePMIHoleThreadNotes(FakePMILeaderLineNotes):
    """component.pmiAnnotations.holeThreadNotes - the same create/add protocol as the leader notes,
    over a hole callout and its own input (createInput takes a LIST of cylindrical faces)."""

    def __init__(self, items=(), note=None, note_input=None):
        super().__init__(items,
                         note=note if note is not None else FakePMIHoleThreadNote(),
                         note_input=(note_input if note_input is not None
                                     else FakePMIHoleThreadNoteInput()))


@fusion_fake(live_type="PMILeaderLineNoteInput", facts=("shape-dump-pmi-world",))
class FakePMILeaderLineNoteInput:
    """The input createInput hands back: the segments and format knobs pmi_create writes onto it
    before add(). ``plane_ok`` False is the setAnnotationPlane the platform DECLINES and
    ``plane_raises`` the message it throws instead; the calls land on the private _plane_calls."""

    def __init__(self, leader_extension=0.5, plane_ok=True, plane_raises=None):
        self.segments = None
        self.leaderLineExtension = leader_extension
        self.horizontalAlignment = None
        self.verticalAlignment = None
        self.isPerpendicularLine = False
        self.annotationTextPoint = None
        self.annotationTargetPoint = None
        self._plane_ok = plane_ok
        self._plane_raises = plane_raises
        self._plane_calls = []

    def setAnnotationPlane(self, *args):
        if self._plane_raises:
            raise RuntimeError(self._plane_raises)
        self._plane_calls.append(args)
        return self._plane_ok


@fusion_fake(live_type="PMIHoleThreadNoteInput", facts=("shape-dump-pmi-world",))
class FakePMIHoleThreadNoteInput:
    """The hole/thread callout's input: the faces createInput was handed plus the flag and display
    knobs pmi_create writes before add(). It carries NO setAnnotationPlane - the live input has
    none, so a caller reaching for one fails here the way it fails live."""

    def __init__(self):
        self.segments = None
        self.leaderLineExtension = 0.5
        self.horizontalAlignment = None
        self.verticalAlignment = None
        self.isPerpendicularLine = False
        self.faces = None


@fusion_fake(live_type="PMIAnnotations", facts=("shape-dump-pmi-world",))
class FakePMIAnnotations(_NamedCollection):
    """component.pmiAnnotations: the flat count/item walk every PMI read runs, plus the two
    authoring collections a create goes through. Both collections are always present, since a live
    component answers both; a collection that will not enumerate is `raises` on this one."""

    def __init__(self, items=(), leader_notes=None, hole_notes=None, raises=None,
                 item_raises=None):
        super().__init__(items, raises=raises, item_raises=item_raises)
        self.leaderLineNotes = (leader_notes if leader_notes is not None
                                else FakePMILeaderLineNotes())
        self.holeThreadNotes = (hole_notes if hole_notes is not None
                                else FakePMIHoleThreadNotes())


@fusion_fake(factory_for="FakePMIAnnotations")
def make_pmi_component(name="Root", annotations=(), leader_notes=None, hole_notes=None,
                       raises=None, item_raises=None):
    """A MakeComp carrying a pmiAnnotations collection over `annotations` - the shape
    _pmi.walk_annotations reads one component through."""
    comp = MakeComp(name)
    comp.pmiAnnotations = FakePMIAnnotations(annotations, leader_notes=leader_notes,
                                             hole_notes=hole_notes, raises=raises,
                                             item_raises=item_raises)
    return comp
