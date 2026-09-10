"""Unit tests for ``design_edit_timeline.py`` - marker rollback, suppress, groups, the irreversible
discard after the marker, and the attributes carried by the entity a timeline item wraps.

The fakes mirror the live member names and return shapes: Timeline (markerPosition get/set,
moveToBeginning/moveToEnd/movetoNextStep/moveToPreviousStep/deleteAllAfterMarker each returning a
bool, item/count/timelineGroups), TimelineObject (isSuppressed get/set, rollTo(rollBefore) -> bool,
isRolledBack/isGroup/index/name/entity), TimelineGroup (deleteMe(deleteGroupAndContents) -> bool,
isCollapsed, count) and TimelineGroups (add(startIndex, endIndex) -> group or None). Every
collection defines __len__, so an EMPTY one reads FALSY exactly as an adsk collection does - a
truthiness guard on one is the defect these tests can catch.

The attribute side: an entity (``_entity``) carrying an Attributes collection (itemByName/add,
where add on an existing group/name updates it in place), attributes built by ``_attribute``
(groupName/name/value + deleteMe() -> bool, and a value that can no longer be read once deleted),
and FakeAttributeVector for what Design.findAttributes hands back - len()/[i] with NO .count.
"""

import types

import pytest

from conftest import FakeTimeline as _SharedTimeline
from conftest import FakeTimelineObject as _SharedTimelineObject
from conftest import _NamedCollection, error_message, load_tool, make_design, payload

et = load_tool("design_edit_timeline")


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeTimelineObject(_SharedTimelineObject):
    """One timeline item. isRolledBack is DERIVED from the marker, as live: an item at or after the
    marker position is not being computed. So a rollTo that returns true without repositioning the
    marker leaves the item in its previous state, exactly as it would in Fusion."""

    def __init__(self, name, index, health=0, suppressed=False, is_group=False,
                 roll_returns=True, suppress_takes=True, rename="ok", reports_rolled=True,
                 entity=None):
        self._holder = None                     # the group this item belongs to, once grouped
        self._suppressed = suppressed
        self._reports_rolled = reports_rolled   # False models an item whose isRolledBack is None
        self._roll_returns = roll_returns
        self.timeline = None
        # The base SEEDS name/isSuppressed through these setters; the refusal modes arm afterwards,
        # so a mode never fires on the construction write.
        self._rename, self._suppress_takes = "ok", True
        super().__init__(name=name, index=index, entity=entity, health=health, is_group=is_group,
                         suppressed=suppressed)
        self._rename, self._suppress_takes = rename, suppress_takes
        self.roll_calls = []

    @property
    def _hidden(self):
        """True when the timeline enumeration does not expose this object: a member of a COLLAPSED
        group. .index and .rollTo both fail on it live."""
        return self._holder is not None and bool(self._holder.isCollapsed)

    @property
    def index(self):
        if self._hidden:
            raise RuntimeError("2 : InternalValidationError : res >= 0")
        return self._index

    @index.setter
    def index(self, value):
        self._index = value

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        if self._rename == "raise":
            raise RuntimeError("read-only")
        if self._rename == "ok":
            self._name = value

    @property
    def isRolledBack(self):
        if not self._reports_rolled:
            return None
        return self.timeline is not None and self.index >= self.timeline.markerPosition

    @isRolledBack.setter
    def isRolledBack(self, value):
        # the base seeds a flag here; this item derives the answer from the marker instead
        pass

    @property
    def isSuppressed(self):
        return self._suppressed

    @isSuppressed.setter
    def isSuppressed(self, value):
        if self._suppress_takes:
            self._suppressed = bool(value)

    def rollTo(self, rollBefore):
        self.roll_calls.append(rollBefore)
        if self._hidden:
            raise RuntimeError("3 : Associated feature is invalid.")
        if not self._roll_returns:
            return False
        if self.timeline is not None:
            self.timeline.markerPosition = self.index if rollBefore else self.index + 1
        return True


class FakeTimelineGroup(FakeTimelineObject):
    """A group: a TimelineObject that also carries members, isCollapsed and
    deleteMe(deleteGroupAndContents). The collapse state decides which side of the pair the timeline
    enumeration exposes, so exactly one of the two can be read at a time - see _hidden."""

    def __init__(self, name, index, members=(), collapsed=True, delete_returns=True,
                 delete_removes=True, rename="ok"):
        super().__init__(name, index, is_group=True, rename=rename)
        self._members = list(members)
        self.isCollapsed = collapsed
        for m in self._members:
            m._holder = self
        self._delete_returns = delete_returns
        self._delete_removes = delete_removes
        self.groups = None
        self.delete_calls = []

    @property
    def _hidden(self):
        """An EXPANDED group is absent from the timeline enumeration - its members show through
        instead - and its own .index and .rollTo fail, the mirror image of a collapsed group's
        members. Its .count stays readable in both states."""
        return not self.isCollapsed

    @property
    def count(self):
        return len(self._members)

    def item(self, i):
        return self._members[i] if 0 <= i < len(self._members) else None

    def __len__(self):
        return len(self._members)

    def deleteMe(self, deleteGroupAndContents):
        self.delete_calls.append(deleteGroupAndContents)
        if not self._delete_returns:
            return False
        if self._delete_removes:
            self.groups._items.remove(self)
        return True


class FakeTimelineGroups(_NamedCollection):
    """The timelineGroups collection: the shared walk plus add(startIndex, endIndex) -> group or
    None. __len__ makes an empty collection FALSY, as an adsk collection is."""

    def __init__(self, timeline, groups=(), add_returns="auto", add_lands=True):
        super().__init__(groups)
        self._timeline = timeline
        for g in self._items:
            g.groups = self
        self._add_returns = add_returns
        self._add_lands = add_lands
        self.add_calls = []

    def __len__(self):
        return len(self._items)

    def add(self, startIndex, endIndex):
        self.add_calls.append((startIndex, endIndex))
        group = self._add_returns
        if group == "auto":
            members = [o for o in self._timeline._items if startIndex <= o.index <= endIndex]
            group = FakeTimelineGroup(f"Group{len(self._items) + 1}", startIndex, members=members)
        if group is None:
            return None
        group.groups = self
        if self._add_lands:
            self._items.append(group)
        return group


class FakeTimeline(_SharedTimeline):
    """Timeline: markerPosition (get AND set), timelineGroups, and deleteAllAfterMarker beside the
    shared walk. A marker move here CLAMPS to the ends instead of refusing past them, and __len__
    makes an EMPTY timeline FALSY, as an adsk collection is."""

    def __init__(self, items=(), marker=None, groups=(), moves_return=True,
                 delete_returns=True, delete_removes=True, marker_sticks=True):
        super().__init__(items, marker=marker)
        for o in self._items:
            o.timeline = self
        self._moves_return = moves_return
        self._delete_returns = delete_returns
        self._delete_removes = delete_removes
        self._marker_sticks = marker_sticks
        self.timelineGroups = FakeTimelineGroups(self, groups)
        self.delete_calls = 0

    def item(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def __len__(self):
        return len(self._items)

    @property
    def markerPosition(self):
        return self._marker

    @markerPosition.setter
    def markerPosition(self, value):
        if self._marker_sticks:
            self._marker = value

    def _move(self, target):
        if not self._moves_return:
            return False
        if self._marker_sticks:
            self._marker = target
        return True

    def movetoNextStep(self):
        return self._move(min(self._marker + 1, len(self._items)))

    def moveToPreviousStep(self):
        return self._move(max(self._marker - 1, 0))

    def deleteAllAfterMarker(self):
        self.delete_calls += 1
        if not self._delete_returns:
            return False
        if self._delete_removes:
            self._items = [o for o in self._items if o.index < self._marker]
        return True


def _attribute(owner, groupName, name, value, delete_returns=True, delete_removes=True):
    """One attribute: groupName/name/value plus deleteMe() -> bool.

    A deleted attribute's value can no longer be read - live, .value on a deleted Attribute raises
    "An API Object refers to a deleted Object", and dropping the value here fails the read the same
    way, so a value captured only AFTER the delete is lost. delete_removes=False models the other
    shape: a delete the platform reports as done while the attribute still reads back."""
    attr = types.SimpleNamespace(groupName=groupName, name=name, value=value, delete_calls=0)

    def _delete_me():
        attr.delete_calls += 1
        if delete_removes:
            owner._items.pop((groupName, name), None)
            del attr.value
        return delete_returns

    attr.deleteMe = _delete_me
    return attr


def _entity(attributes=None):
    """The feature object a timeline item wraps - the thing that really carries the attributes,
    since Timeline and TimelineObject expose none."""
    return types.SimpleNamespace(attributes=FakeAttributes() if attributes is None else attributes)


class FakeAttributes:
    """The attributes collection on a feature ENTITY: itemByName(groupName, name) -> attribute or
    None, and add(groupName, name, value), which updates an existing group/name pair rather than
    creating a second one. 'stores' models a platform that keeps a value other than the one given."""

    def __init__(self, add_lands=True, stores=None, add_raises=False, delete_returns=True,
                 delete_removes=True):
        self._items = {}
        self._add_lands = add_lands
        self._stores = stores
        self._add_raises = add_raises
        self._delete_returns = delete_returns
        self._delete_removes = delete_removes
        self.add_calls = []

    def itemByName(self, groupName, name):
        return self._items.get((groupName, name))

    def add(self, groupName, name, value):
        self.add_calls.append((groupName, name, value))
        if self._add_raises:
            raise RuntimeError("3 : InternalValidationError : attribute refused")
        stored = value if self._stores is None else self._stores
        existing = self._items.get((groupName, name))
        if existing is not None:
            existing.value = stored
            return existing
        attr = _attribute(self, groupName, name, stored, delete_returns=self._delete_returns,
                          delete_removes=self._delete_removes)
        if self._add_lands:
            self._items[(groupName, name)] = attr
        return attr


def _raising_item_by_name(groupName, name):
    """A replacement itemByName that raises instead of answering - a read-back that yields no
    verdict, distinct from a readable None."""
    raise RuntimeError("3 : An API Object refers to a deleted Object")


class FakeAttributeVector:
    """Design.findAttributes hands back an AttributeVector: len() and [i], with NO .count - reading
    .count raises, exactly as it does live."""

    def __init__(self, items):
        self._items = list(items)

    def __len__(self):
        return len(self._items)

    def __getitem__(self, i):
        return self._items[i]

    @property
    def count(self):
        raise AttributeError("'AttributeVector' object has no attribute 'count'")


def _find_attributes(timeline, group, name):
    """The design-wide search: every timeline entity carrying this group/name pair."""
    hits = []
    for i in range(0 if timeline is None else timeline.count):
        entity = getattr(timeline.item(i), "entity", None)
        attrs = getattr(entity, "attributes", None)
        found = attrs.itemByName(group, name) if attrs is not None else None
        if found is not None:
            hits.append(found)
    return FakeAttributeVector(hits)


def _tagged(names=("Sketch1", "Extrude1"), attributes=None):
    """A timeline whose items each wrap an entity carrying its OWN attributes collection; a supplied
    collection goes to the first item, which is the one the attribute tests target."""
    items = []
    for i, n in enumerate(names):
        attrs = attributes if (attributes is not None and i == 0) else FakeAttributes()
        items.append(FakeTimelineObject(n, i, entity=_entity(attrs)))
    return FakeTimeline(items)


def _timeline(names=("Sketch1", "Extrude1", "Fillet1"), **kw):
    items = [FakeTimelineObject(n, i) for i, n in enumerate(names)]
    return FakeTimeline(items, **kw)


@pytest.fixture
def wire(monkeypatch):
    """Install a design carrying `timeline` into BOTH seams the tool reads the design through - its
    own _common and the one _inputs resolves a FeatureRef against; returns the timeline."""
    def _wire(timeline):
        design = make_design()
        design.timeline = timeline
        design.findAttributes = lambda group, name: _find_attributes(timeline, group, name)
        monkeypatch.setattr(et._common, "design", lambda: design)
        monkeypatch.setattr(et._inputs._common, "design", lambda: design)
        return timeline
    return _wire


# ── roll: to a named item ────────────────────────────────────────────────────

class TestRollToFeature:
    def test_rolls_before_the_named_item(self, wire):
        tl = wire(_timeline(marker=3))
        out = payload(et.handler(action="roll", feature="Extrude1", to="before"))
        assert tl._items[1].roll_calls == [True]          # rollTo(rollBefore=True)
        assert out["marker_position"] == 1 and out["marker_position_before"] == 3
        assert out["rolled_back"] == 2                     # 3 items, marker at 1

    def test_rolls_after_the_named_item(self, wire):
        tl = wire(_timeline(marker=0))
        out = payload(et.handler(action="roll", feature="Extrude1", to="after"))
        assert tl._items[1].roll_calls == [False]
        assert out["marker_position"] == 2 and out["rolled_back"] == 1

    def test_roll_to_false_is_an_error(self, wire):
        items = [FakeTimelineObject("Extrude1", 0, roll_returns=False)]
        wire(FakeTimeline(items, marker=1))
        msg = error_message(et.handler(action="roll", feature="Extrude1"))
        assert "declined" in msg and "markerPosition is still 1" in msg

    def test_marker_that_did_not_land_is_an_error(self, wire):
        # rollTo returns true but the marker stays at the end, so the item asked to be rolled back
        # is still computed: a reported success the tool must NOT pass on.
        wire(_timeline(marker=3, marker_sticks=False))
        msg = error_message(et.handler(action="roll", feature="Extrude1", to="before"))
        assert "isRolledBack=False" in msg and "3 -> 3" in msg

    def test_a_rollto_that_raises_is_reported_not_swallowed(self, wire):
        # An expanded group raises "Associated feature is invalid." rather than returning false.
        group = FakeTimelineGroup("Group1", 1, collapsed=False)
        wire(FakeTimeline([FakeTimelineObject("Sketch1", 0), group]))
        msg = error_message(et.handler(action="roll", feature="Group1"))
        assert "Associated feature is invalid" in msg and "Rolling before" in msg

    def test_an_item_hidden_in_a_collapsed_group_names_the_group(self, wire):
        # A collapsed group's members are absent from timeline.item(), so a real feature reads as
        # missing. Saying it does not exist would send the caller looking for a typo.
        members = [FakeTimelineObject("Sketch1", 0), FakeTimelineObject("Extrude1", 1)]
        group = FakeTimelineGroup("Base", 0, members=members, collapsed=True)
        wire(FakeTimeline([group, FakeTimelineObject("Fillet1", 1)], groups=[group]))
        msg = error_message(et.handler(action="roll", feature="Extrude1"))
        assert "inside the collapsed timeline group 'Base'" in msg
        assert "No timeline object named" not in msg

    def test_a_genuinely_absent_name_still_lists_the_timeline(self, wire):
        members = [FakeTimelineObject("Sketch1", 0)]
        group = FakeTimelineGroup("Base", 0, members=members, collapsed=True)
        wire(FakeTimeline([group, FakeTimelineObject("Fillet1", 1)], groups=[group]))
        msg = error_message(et.handler(action="roll", feature="Chamfer9"))
        assert "no timeline feature named 'Chamfer9'" in msg
        assert "Base" in msg and "Fillet1" in msg      # what the timeline DOES hold

    def test_collapsed_group_rolls(self, wire):
        group = FakeTimelineGroup("Group1", 1, collapsed=True)
        wire(FakeTimeline([FakeTimelineObject("Sketch1", 0), group], marker=2))
        out = payload(et.handler(action="roll", feature="Group1"))
        assert group.roll_calls == [True] and out["marker_position"] == 1

    def test_place_word_rejected_without_a_feature(self, wire):
        wire(_timeline())
        msg = error_message(et.handler(action="roll", to="before"))
        assert "beginning" in msg and "'before'" in msg

    def test_step_word_rejected_with_a_feature(self, wire):
        wire(_timeline())
        msg = error_message(et.handler(action="roll", feature="Extrude1", to="end"))
        assert "to='before'" in msg and "'end'" in msg


# ── roll: the marker moves ───────────────────────────────────────────────────

class TestRollSteps:
    def test_beginning_and_end(self, wire):
        tl = wire(_timeline(marker=2))
        assert payload(et.handler(action="roll", to="beginning"))["marker_position"] == 0
        assert payload(et.handler(action="roll", to="end"))["marker_position"] == 3
        assert tl.markerPosition == 3

    def test_next_and_previous_steps(self, wire):
        wire(_timeline(marker=1))
        assert payload(et.handler(action="roll", to="next"))["marker_position"] == 2
        assert payload(et.handler(action="roll", to="previous"))["marker_position"] == 1

    def test_next_at_the_end_does_not_report_a_move(self, wire):
        # movetoNextStep returns true but the marker is already at the end - not a move to report.
        wire(_timeline(marker=3))
        msg = error_message(et.handler(action="roll", to="next"))
        assert "still 3" in msg and "next" in msg

    def test_end_that_lands_short_is_an_error(self, wire):
        wire(_timeline(marker=0, marker_sticks=False))
        msg = error_message(et.handler(action="roll", to="end"))
        assert "markerPosition is 0, not 3" in msg

    def test_declined_move_is_an_error(self, wire):
        wire(_timeline(marker=2, moves_return=False))
        msg = error_message(et.handler(action="roll", to="beginning"))
        assert "declined" in msg and "still at 2" in msg

    def test_empty_timeline_is_not_mistaken_for_a_missing_one(self, wire):
        # an adsk Timeline with no items reads FALSY - the handler must still see a timeline.
        wire(FakeTimeline([], marker=0))
        out = payload(et.handler(action="roll", to="beginning"))
        assert out["timeline_count"] == 0 and out["marker_position"] == 0


# ── suppress ─────────────────────────────────────────────────────────────────

class TestSuppress:
    def test_suppresses_and_reads_back(self, wire):
        tl = wire(_timeline())
        out = payload(et.handler(action="suppress", feature="Fillet1"))
        assert tl._items[2].isSuppressed is True
        assert out["is_suppressed"] is True and out["was_suppressed"] is False
        assert out["index"] == 2

    def test_unsuppresses(self, wire):
        items = [FakeTimelineObject("Fillet1", 0, suppressed=True)]
        wire(FakeTimeline(items))
        out = payload(et.handler(action="suppress", feature="Fillet1", suppressed=False))
        assert out["is_suppressed"] is False and out["was_suppressed"] is True

    def test_a_set_that_does_not_take_is_an_error(self, wire):
        items = [FakeTimelineObject("Fillet1", 0, suppress_takes=False)]
        wire(FakeTimeline(items))
        msg = error_message(et.handler(action="suppress", feature="Fillet1"))
        assert "did not take" in msg and "Fillet1@0" in msg

    def test_new_downstream_error_is_reported(self, wire):
        broken = FakeTimelineObject("Extrude1", 1)

        class _Breaking(FakeTimelineObject):
            """Suppressing this item is what breaks the downstream feature; `_armed` arms the hook
            after construction, so the base's own seeding write does not fire it."""
            _armed = False

            @FakeTimelineObject.isSuppressed.setter
            def isSuppressed(self, value):
                self._suppressed = bool(value)
                if self._armed:
                    broken.healthState = 2   # the downstream feature loses what this one produced

        target = _Breaking("Sketch1", 0)
        target._armed = True
        wire(FakeTimeline([target, broken]))
        out = payload(et.handler(action="suppress", feature="Sketch1"))
        assert out["timeline_errors_after"] == ["Extrude1"]
        assert "Extrude1" in out["note"]

    def test_preexisting_warning_surfaces_without_a_new_error(self, wire):
        wire(FakeTimeline([FakeTimelineObject("Sketch1", 0),
                           FakeTimelineObject("Extrude1", 1, health=1)]))
        out = payload(et.handler(action="suppress", feature="Sketch1"))
        assert out["timeline_warnings"] == ["Extrude1"]
        assert "timeline_errors_after" not in out

    def test_missing_feature_argument(self, wire):
        wire(_timeline())
        assert "needs 'feature'" in error_message(et.handler(action="suppress"))


# ── name resolution ──────────────────────────────────────────────────────────

class TestNameResolution:
    def test_repeated_name_is_refused_with_candidates(self, wire):
        tl = wire(FakeTimeline([FakeTimelineObject("Extrude1", 0), FakeTimelineObject("Extrude1", 2)]))
        msg = error_message(et.handler(action="suppress", feature="Extrude1"))
        # the shared timeline vocabulary, prefixed by THIS call's noun for the target
        assert msg.startswith("the object to suppress: ")
        assert "matches 2 timeline objects" in msg
        assert "Extrude1@0" in msg and "Extrude1@2" in msg
        assert [o.isSuppressed for o in tl._items] == [False, False]

    def test_name_at_index_targets_that_item(self, wire):
        a = FakeTimelineObject("Extrude1", 0)
        b = FakeTimelineObject("Extrude1", 1)
        wire(FakeTimeline([a, b]))
        payload(et.handler(action="suppress", feature="Extrude1@1"))
        assert b.isSuppressed is True and a.isSuppressed is False

    def test_name_at_index_mismatch_is_refused(self, wire):
        # 'Extrude1@0' pairs a name with an index no object carries together - refused as given,
        # never widened to the Extrude1 that sits at index 1, and the refusal names both halves so
        # the caller can see which one moved.
        tl = wire(FakeTimeline([FakeTimelineObject("Sketch1", 0), FakeTimelineObject("Extrude1", 1)]))
        msg = error_message(et.handler(action="suppress", feature="Extrude1@0"))
        assert "index 0 is 'Sketch1'" in msg and "'Extrude1' is at index 1" in msg
        assert tl._items[1].isSuppressed is False

    def test_partial_name_does_not_match(self, wire):
        # 'Extrude' is not 'Extrude1' - a loose match would target the wrong item.
        tl = wire(_timeline())
        msg = error_message(et.handler(action="suppress", feature="Extrude"))
        assert "no timeline feature named 'Extrude'" in msg and "Extrude1" in msg
        assert tl._items[1].isSuppressed is False

    def test_name_match_is_case_insensitive(self, wire):
        tl = wire(_timeline())
        payload(et.handler(action="suppress", feature="extrude1"))
        assert tl._items[1].isSuppressed is True

    def test_an_object_whose_name_carries_a_leading_space_is_addressable(self, wire):
        # Fusion names an occurrence-create timeline object ' InsProbe:1' (probe_fix_campaign.log
        # [F74]); the space is in no listing an agent reads, so the name it CAN type must resolve.
        tl = wire(FakeTimeline([FakeTimelineObject(" InsProbe:1", 0)]))
        payload(et.handler(action="suppress", feature="InsProbe:1"))
        assert tl._items[0].isSuppressed is True

    def test_a_padded_input_resolves_the_same_object(self, wire):
        # A REDUNDANCY check, and only red when BOTH layers lose it: this tool strips its own
        # 'feature' before dispatch AND the shared matcher strips the want it is handed. Single
        # mutants survive it by design - the WANT half is pinned where it lives, on the matcher
        # (test_inputs.TestTimelineNameWhitespace::test_the_WANT_side_is_stripped).
        tl = wire(_timeline())
        payload(et.handler(action="suppress", feature="  Extrude1  "))
        assert tl._items[1].isSuppressed is True


# ── groups ───────────────────────────────────────────────────────────────────

class TestGroup:
    def test_groups_a_range_and_names_it(self, wire):
        tl = wire(_timeline())
        out = payload(et.handler(action="group", feature="Sketch1", end_feature="Extrude1",
                                 name="Boss"))
        assert tl.timelineGroups.add_calls == [(0, 1)]
        assert out["group"] == "Boss" and out["member_count"] == 2
        assert out["start_index"] == 0 and out["end_index"] == 1 and out["groups"] == 1

    def test_groups_into_an_empty_collection(self, wire):
        # timelineGroups with no groups reads FALSY - a truthiness guard would refuse the first group.
        tl = wire(_timeline())
        assert not tl.timelineGroups                      # the falsy-empty shape live collections have
        out = payload(et.handler(action="group", feature="Sketch1", end_feature="Fillet1"))
        assert out["grouped"] is True and out["member_count"] == 3

    def test_reversed_range_is_refused(self, wire):
        wire(_timeline())
        msg = error_message(et.handler(action="group", feature="Fillet1", end_feature="Sketch1"))
        assert "comes after" in msg and "timeline order" in msg

    def test_a_group_inside_the_range_is_refused(self, wire):
        inner = FakeTimelineGroup("Group1", 1)
        wire(FakeTimeline([FakeTimelineObject("Sketch1", 0), inner,
                           FakeTimelineObject("Fillet1", 2)]))
        msg = error_message(et.handler(action="group", feature="Sketch1", end_feature="Fillet1"))
        assert "cannot hold another group" in msg and "Group1@1" in msg

    def test_a_range_overlapping_an_expanded_group_is_refused(self, wire):
        # An expanded group is absent from timeline.item() and only its MEMBERS enumerate, so the
        # nested-group walk cannot see it. add() does not refuse the overlap - the two groups end up
        # sharing members - so the tool must.
        members = [FakeTimelineObject("Sketch1", 0), FakeTimelineObject("Extrude1", 1)]
        group = FakeTimelineGroup("Base", 0, members=members, collapsed=False)
        tl = FakeTimeline(members + [FakeTimelineObject("Sketch2", 2),
                                     FakeTimelineObject("Extrude2", 3)], groups=[group])
        wire(tl)
        msg = error_message(et.handler(action="group", feature="Extrude1", end_feature="Extrude2"))
        assert "overlap the expanded group 'Base'" in msg and "0..1" in msg
        assert tl.timelineGroups.add_calls == []       # refused BEFORE add(), not rolled back after

    def test_a_range_clear_of_an_expanded_group_still_groups(self, wire):
        members = [FakeTimelineObject("Sketch1", 0), FakeTimelineObject("Extrude1", 1)]
        group = FakeTimelineGroup("Base", 0, members=members, collapsed=False)
        tl = FakeTimeline(members + [FakeTimelineObject("Sketch2", 2),
                                     FakeTimelineObject("Extrude2", 3)], groups=[group])
        wire(tl)
        out = payload(et.handler(action="group", feature="Sketch2", end_feature="Extrude2"))
        assert out["grouped"] is True and tl.timelineGroups.add_calls == [(2, 3)]

    def test_a_group_that_did_not_land_is_an_error(self, wire):
        # add() hands back a group the collection never took - the count is the ground truth.
        tl = _timeline()
        tl.timelineGroups = FakeTimelineGroups(tl, (), add_lands=False)
        wire(tl)
        msg = error_message(et.handler(action="group", feature="Sketch1", end_feature="Extrude1"))
        assert "still holds 0" in msg and "did not land" in msg

    def test_add_returning_null_is_an_error(self, wire):
        tl = _timeline()
        tl.timelineGroups = FakeTimelineGroups(tl, (), add_returns=None)
        wire(tl)
        msg = error_message(et.handler(action="group", feature="Sketch1", end_feature="Extrude1"))
        assert "returned no group" in msg and "nothing was grouped" in msg

    def test_a_rename_that_silently_does_nothing_is_warned(self, wire):
        tl = _timeline()
        tl.timelineGroups = FakeTimelineGroups(
            tl, (), add_returns=FakeTimelineGroup("Group1", 0, rename="ignore"))
        wire(tl)
        out = payload(et.handler(action="group", feature="Sketch1", end_feature="Extrude1",
                                 name="Boss"))
        assert out["name_warning"] == ("The group was created but its name reads 'Group1', not "
                                       "'Boss'.")
        assert out["group"] == "Group1"

    def test_a_rename_that_raises_is_warned(self, wire):
        tl = _timeline()
        tl.timelineGroups = FakeTimelineGroups(
            tl, (), add_returns=FakeTimelineGroup("Group1", 0, rename="raise"))
        wire(tl)
        out = payload(et.handler(action="group", feature="Sketch1", end_feature="Extrude1",
                                 name="Boss"))
        assert "read-only" in out["name_warning"] and out["group"] == "Group1"

    def test_missing_range_argument(self, wire):
        wire(_timeline())
        assert "needs 'feature'" in error_message(et.handler(action="group", feature="Sketch1"))


class TestUngroup:
    def test_removes_the_group_and_keeps_its_items(self, wire):
        group = FakeTimelineGroup("Boss", 0, members=[FakeTimelineObject("Sketch1", 0)])
        tl = FakeTimeline([FakeTimelineObject("Sketch1", 0)], groups=[group])
        wire(tl)
        out = payload(et.handler(action="ungroup", feature="Boss"))
        assert group.delete_calls == [False]              # contents kept, not deleted with the group
        assert out["ungrouped"] is True and out["groups"] == 0

    def test_delete_me_false_is_an_error(self, wire):
        group = FakeTimelineGroup("Boss", 0, delete_returns=False)
        wire(FakeTimeline([FakeTimelineObject("Sketch1", 0)], groups=[group]))
        msg = error_message(et.handler(action="ungroup", feature="Boss"))
        assert "declined" in msg and "Boss" in msg

    def test_a_group_that_survives_its_delete_is_an_error(self, wire):
        group = FakeTimelineGroup("Boss", 0, delete_removes=False)
        wire(FakeTimeline([FakeTimelineObject("Sketch1", 0)], groups=[group]))
        msg = error_message(et.handler(action="ungroup", feature="Boss"))
        assert "still holds 1" in msg and "was not removed" in msg

    def test_unknown_group_lists_the_groups(self, wire):
        wire(FakeTimeline([FakeTimelineObject("Sketch1", 0)],
                          groups=[FakeTimelineGroup("Boss", 0)]))
        msg = error_message(et.handler(action="ungroup", feature="Flange"))
        assert "No timeline group named 'Flange'" in msg and "Boss@0" in msg

    def test_repeated_group_name_is_refused(self, wire):
        wire(FakeTimeline([FakeTimelineObject("Sketch1", 0)],
                          groups=[FakeTimelineGroup("Boss", 0), FakeTimelineGroup("Boss", 2)]))
        msg = error_message(et.handler(action="ungroup", feature="Boss"))
        assert "names 2 timeline groups" in msg


# ── delete after the marker ──────────────────────────────────────────────────

class TestDeleteAfterMarker:
    def test_refuses_without_the_confirmation_and_deletes_nothing(self, wire):
        tl = wire(_timeline(marker=1))
        msg = error_message(et.handler(action="delete_after_marker"))
        assert "DISCARDS 2 timeline item(s)" in msg
        assert "Extrude1@1" in msg and "Fillet1@2" in msg
        assert tl.delete_calls == 0 and tl.count == 3

    def test_discards_after_the_marker_when_confirmed(self, wire):
        tl = wire(_timeline(marker=1))
        out = payload(et.handler(action="delete_after_marker",
                                 confirm_delete_after_marker=True))
        assert tl.count == 1 and out["deleted"] == 2
        assert out["discarded"] == ["Extrude1@1", "Fillet1@2"]
        assert out["timeline_count"] == 1 and out["marker_position"] == 1

    def test_the_preview_counts_a_collapsed_groups_members_not_the_one_entry(self, wire):
        # A collapsed group is ONE entry in the enumeration but takes every member with it, so a
        # count of entries understates the blast radius the confirmation gate exists to state.
        members = [FakeTimelineObject("Sketch1", 0), FakeTimelineObject("Extrude1", 1),
                   FakeTimelineObject("Sketch2", 2)]
        group = FakeTimelineGroup("Base", 0, members=members, collapsed=True)
        tl = FakeTimeline([group, FakeTimelineObject("Fillet1", 1)], groups=[group], marker=0)
        wire(tl)
        msg = error_message(et.handler(action="delete_after_marker"))
        assert "DISCARDS 4 timeline item(s)" in msg     # 3 inside the group + 1 loose, not 2 entries
        assert "Base@0 (3 items)" in msg and tl.delete_calls == 0

    def test_the_confirmed_payload_reports_members_and_entries_apart(self, wire):
        members = [FakeTimelineObject("Sketch1", 0), FakeTimelineObject("Extrude1", 1),
                   FakeTimelineObject("Sketch2", 2)]
        group = FakeTimelineGroup("Base", 0, members=members, collapsed=True)
        tl = FakeTimeline([group, FakeTimelineObject("Fillet1", 1)], groups=[group], marker=0)
        wire(tl)
        out = payload(et.handler(action="delete_after_marker", confirm_delete_after_marker=True))
        assert out["deleted"] == 4 and out["deleted_timeline_entries"] == 2

    def test_marker_at_the_end_is_refused(self, wire):
        tl = wire(_timeline(marker=3))
        msg = error_message(et.handler(action="delete_after_marker",
                                       confirm_delete_after_marker=True))
        assert "Nothing lies after the marker" in msg and tl.delete_calls == 0

    def test_declined_delete_is_an_error(self, wire):
        wire(_timeline(marker=1, delete_returns=False))
        msg = error_message(et.handler(action="delete_after_marker",
                                       confirm_delete_after_marker=True))
        assert "declined" in msg and "still holds 3" in msg

    def test_success_that_discarded_nothing_is_an_error(self, wire):
        # deleteAllAfterMarker returns true while the timeline keeps every item - a false success.
        wire(_timeline(marker=1, delete_removes=False))
        msg = error_message(et.handler(action="delete_after_marker",
                                       confirm_delete_after_marker=True))
        assert "still holds 3 item(s) of 3" in msg and "nothing was discarded" in msg


# ── attributes on the entity a timeline item wraps ───────────────────────────

class TestSetAttribute:
    def test_attaches_to_the_entity_and_reads_the_value_back(self, wire):
        tl = wire(_tagged())
        out = payload(et.handler(action="set_attribute", feature="Sketch1",
                                 attribute_group="shop", attribute_name="finish",
                                 attribute_value="anodized"))
        attrs = tl._items[0].entity.attributes
        assert attrs.add_calls == [("shop", "finish", "anodized")]
        assert attrs.itemByName("shop", "finish").value == "anodized"
        assert out["attribute_set"] is True and out["value"] == "anodized"
        # entity_type is the class of the object the timeline item wraps (live: Sketch, ExtrudeFeature)
        assert out["feature"] == "Sketch1" and out["entity_type"] == "SimpleNamespace"
        # nothing landed on the OTHER item's entity - the attribute is per-entity
        assert tl._items[1].entity.attributes.itemByName("shop", "finish") is None

    def test_design_matches_counts_the_vector_by_len_not_count(self, wire):
        # Design.findAttributes returns an AttributeVector: .count RAISES, so a count-based read
        # would silently report nothing instead of the design-wide match.
        wire(_tagged())
        out = payload(et.handler(action="set_attribute", feature="Sketch1",
                                 attribute_group="shop", attribute_name="finish",
                                 attribute_value="anodized"))
        assert out["design_matches"] == 1

    def test_a_first_time_set_reports_no_previous_value(self, wire):
        # Nothing was replaced, so there is no previous value to publish - a null one would read as
        # "there was an attribute here" to the caller.
        wire(_tagged())
        out = payload(et.handler(action="set_attribute", feature="Sketch1",
                                 attribute_group="shop", attribute_name="finish",
                                 attribute_value="anodized"))
        assert "previous_value" not in out

    def test_overwriting_reports_the_previous_value(self, wire):
        wire(_tagged())
        et.handler(action="set_attribute", feature="Sketch1", attribute_group="shop",
                   attribute_name="finish", attribute_value="raw")
        out = payload(et.handler(action="set_attribute", feature="Sketch1",
                                 attribute_group="shop", attribute_name="finish",
                                 attribute_value="anodized"))
        assert out["previous_value"] == "raw" and out["value"] == "anodized"

    def test_an_attribute_that_did_not_land_is_an_error(self, wire):
        # add() hands back an attribute the entity never took - itemByName is the ground truth.
        wire(_tagged(attributes=FakeAttributes(add_lands=False)))
        msg = error_message(et.handler(action="set_attribute", feature="Sketch1",
                                       attribute_group="shop", attribute_name="finish",
                                       attribute_value="anodized"))
        assert "reads no attribute 'shop/finish' back" in msg and "nothing was attached" in msg

    def test_a_value_that_did_not_take_is_an_error(self, wire):
        wire(_tagged(attributes=FakeAttributes(stores="raw")))
        msg = error_message(et.handler(action="set_attribute", feature="Sketch1",
                                       attribute_group="shop", attribute_name="finish",
                                       attribute_value="anodized"))
        assert "reads 'raw'" in msg and "did not take" in msg

    def test_an_add_that_raises_is_reported_not_swallowed(self, wire):
        wire(_tagged(attributes=FakeAttributes(add_raises=True)))
        res = et.handler(action="set_attribute", feature="Sketch1", attribute_group="shop",
                         attribute_name="finish", attribute_value="anodized")
        assert res["isError"] is True
        assert "attribute refused" in res["message"] and "'shop/finish'" in res["message"]

    def test_an_entity_without_attributes_is_refused(self, wire):
        wire(FakeTimeline([FakeTimelineObject("Sketch1", 0, entity=object())]))
        msg = error_message(et.handler(action="set_attribute", feature="Sketch1",
                                       attribute_group="shop", attribute_name="finish",
                                       attribute_value="anodized"))
        assert "carries no attributes collection" in msg

    def test_an_oversized_value_is_refused_naming_its_length(self, wire):
        tl = wire(_tagged())
        big = "x" * (et._ATTR_MAX_CHARS + 1)
        msg = error_message(et.handler(action="set_attribute", feature="Sketch1",
                                       attribute_group="shop", attribute_name="blob",
                                       attribute_value=big))
        assert f"{et._ATTR_MAX_CHARS + 1} characters" in msg
        assert tl._items[0].entity.attributes.add_calls == []      # refused BEFORE the mutation

    def test_a_value_at_the_bound_is_accepted(self, wire):
        wire(_tagged())
        out = payload(et.handler(action="set_attribute", feature="Sketch1",
                                 attribute_group="shop", attribute_name="blob",
                                 attribute_value="x" * et._ATTR_MAX_CHARS))
        assert len(out["value"]) == et._ATTR_MAX_CHARS

    def test_a_non_string_value_is_refused(self, wire):
        wire(_tagged())
        msg = error_message(et.handler(action="set_attribute", feature="Sketch1",
                                       attribute_group="shop", attribute_name="count",
                                       attribute_value=7))
        assert "takes a string" in msg and "int" in msg

    def test_a_regex_prefixed_group_is_refused(self, wire):
        # 're:' turns the attribute search into a regular expression, so a literal name carrying it
        # would be reported against a different search than the one that was asked for.
        tl = wire(_tagged())
        msg = error_message(et.handler(action="set_attribute", feature="Sketch1",
                                       attribute_group="re:shop", attribute_name="finish",
                                       attribute_value="anodized"))
        assert "regular expression" in msg and "attribute_group" in msg
        assert tl._items[0].entity.attributes.add_calls == []

    def test_the_regex_guard_matches_the_lowercase_prefix_exactly(self, wire):
        # 're:' is the prefix the attribute search reads as a regular expression; an upper-case
        # spelling is an ordinary literal name, so refusing it would refuse a legal group.
        tl = wire(_tagged())
        out = payload(et.handler(action="set_attribute", feature="Sketch1",
                                 attribute_group="RE:shop", attribute_name="finish",
                                 attribute_value="anodized"))
        assert out["attribute_group"] == "RE:shop"
        assert tl._items[0].entity.attributes.itemByName("RE:shop", "finish") is not None

    def test_missing_group_or_name_names_both_values(self, wire):
        wire(_tagged())
        msg = error_message(et.handler(action="set_attribute", feature="Sketch1",
                                       attribute_name="finish", attribute_value="anodized"))
        assert "attribute_group=''" in msg and "attribute_name='finish'" in msg

    def test_a_timeline_group_has_no_entity_to_tag(self, wire):
        group = FakeTimelineGroup("Boss", 0, collapsed=True)
        wire(FakeTimeline([group], groups=[group]))
        msg = error_message(et.handler(action="set_attribute", feature="Boss",
                                       attribute_group="shop", attribute_name="finish",
                                       attribute_value="anodized"))
        assert "timeline GROUP" in msg

    def test_a_repeated_feature_name_is_refused(self, wire):
        wire(_tagged(names=("Extrude1", "Extrude1")))
        msg = error_message(et.handler(action="set_attribute", feature="Extrude1",
                                       attribute_group="shop", attribute_name="finish",
                                       attribute_value="anodized"))
        assert "matches 2 timeline objects" in msg and "Extrude1@1" in msg

    def test_name_at_index_targets_that_entity(self, wire):
        tl = wire(_tagged(names=("Extrude1", "Extrude1")))
        payload(et.handler(action="set_attribute", feature="Extrude1@1", attribute_group="shop",
                           attribute_name="finish", attribute_value="anodized"))
        assert tl._items[1].entity.attributes.itemByName("shop", "finish") is not None
        assert tl._items[0].entity.attributes.itemByName("shop", "finish") is None

    def test_missing_feature_argument(self, wire):
        wire(_tagged())
        msg = error_message(et.handler(action="set_attribute", attribute_group="shop",
                                       attribute_name="finish", attribute_value="anodized"))
        assert "need 'feature'" in msg


class TestDeleteAttribute:
    def _seeded(self, wire, **kw):
        tl = wire(_tagged(attributes=FakeAttributes(**kw)))
        et.handler(action="set_attribute", feature="Sketch1", attribute_group="shop",
                   attribute_name="finish", attribute_value="anodized")
        return tl

    def test_deletes_and_confirms_the_attribute_is_gone(self, wire):
        tl = self._seeded(wire)
        out = payload(et.handler(action="delete_attribute", feature="Sketch1",
                                 attribute_group="shop", attribute_name="finish"))
        assert out["attribute_deleted"] is True
        # the value survives in the payload only because it is captured BEFORE the delete - a
        # deleted attribute cannot be read
        assert out["deleted_value"] == "anodized"
        assert tl._items[0].entity.attributes.itemByName("shop", "finish") is None
        assert out["design_matches"] == 0

    def test_an_attribute_that_survives_the_delete_is_an_error(self, wire):
        self._seeded(wire, delete_removes=False)
        msg = error_message(et.handler(action="delete_attribute", feature="Sketch1",
                                       attribute_group="shop", attribute_name="finish"))
        assert "was not deleted" in msg and "anodized" in msg

    def test_delete_me_false_with_the_attribute_gone_reports_the_boolean(self, wire):
        # The read-back is the ground truth: the attribute IS gone, so the call succeeded - but the
        # payload says the platform reported false rather than hiding it.
        self._seeded(wire, delete_returns=False)
        out = payload(et.handler(action="delete_attribute", feature="Sketch1",
                                 attribute_group="shop", attribute_name="finish"))
        assert out["attribute_deleted"] is True
        assert "deleteMe returned false" in out["note"]

    def test_an_unreadable_read_back_is_not_a_confirmed_delete(self, wire):
        # itemByName raising and itemByName reading None are the same answer through safe(): only
        # the second one says the attribute is gone, and the first must not be published as it.
        tl = self._seeded(wire)
        attrs = tl._items[0].entity.attributes
        attr = attrs.itemByName("shop", "finish")
        delete = attr.deleteMe

        def delete_then_blind():
            did = delete()
            attrs.itemByName = _raising_item_by_name
            return did
        attr.deleteMe = delete_then_blind
        msg = error_message(et.handler(action="delete_attribute", feature="Sketch1",
                                       attribute_group="shop", attribute_name="finish"))
        assert "reading it back raised" in msg and "UNCONFIRMED" in msg
        # the refusal has to leave the caller a way to settle it - repeating the call is the read:
        # an absent-attribute refusal IS the delete having landed.
        assert "delete_attribute call again" in msg and "'shop/finish' as absent" in msg

    def test_an_unreadable_lookup_before_the_delete_is_not_an_absent_attribute(self, wire):
        # the pre-delete lookup answers the same question as the read-back, so it needs the same
        # sentinel: swallowed into None, a collection that answers nothing reads as "carries no
        # attribute" - the absent verdict the read-back's own advice sends the caller here to read.
        tl = self._seeded(wire)
        tl._items[0].entity.attributes.itemByName = _raising_item_by_name
        msg = error_message(et.handler(action="delete_attribute", feature="Sketch1",
                                       attribute_group="shop", attribute_name="finish"))
        assert "whether it is there cannot be told" in msg and "nothing was deleted" in msg
        assert "carries no attribute" not in msg

    def test_a_read_back_of_none_still_confirms_the_delete(self, wire):
        # the other direction of the same gate: a readable None IS the attribute being gone.
        self._seeded(wire)
        out = payload(et.handler(action="delete_attribute", feature="Sketch1",
                                 attribute_group="shop", attribute_name="finish"))
        assert out["attribute_deleted"] is True

    def test_deleting_an_absent_attribute_is_refused(self, wire):
        wire(_tagged())
        msg = error_message(et.handler(action="delete_attribute", feature="Sketch1",
                                       attribute_group="shop", attribute_name="finish"))
        assert "carries no attribute 'shop/finish'" in msg and "nothing was deleted" in msg

    def test_deleting_from_the_wrong_feature_does_not_touch_the_tagged_one(self, wire):
        tl = self._seeded(wire)
        msg = error_message(et.handler(action="delete_attribute", feature="Extrude1",
                                       attribute_group="shop", attribute_name="finish"))
        assert "carries no attribute" in msg
        assert tl._items[0].entity.attributes.itemByName("shop", "finish").value == "anodized"


# ── guards shared by every action ────────────────────────────────────────────

class TestGuards:
    def test_no_active_design(self, monkeypatch):
        monkeypatch.setattr(et._common, "design", lambda: None)
        assert "No active design" in error_message(et.handler(action="roll", to="end"))

    def test_direct_modelling_design_has_no_timeline(self, wire):
        wire(None)
        assert "no timeline" in error_message(et.handler(action="roll", to="end"))

    def test_unknown_action_is_refused(self, wire):
        wire(_timeline())
        msg = error_message(et.handler(action="rewind"))
        assert "'action' must be one of" in msg and "delete_after_marker" in msg

    def test_unknown_to_is_refused(self, wire):
        wire(_timeline())
        assert "'to' must be one of" in error_message(et.handler(action="roll", to="halfway"))


class TestWiring:
    def test_declared_destructive_with_the_confirm_input(self):
        entry = et.item.to_dict()
        assert entry["annotations"]["destructiveHint"] is True
        assert entry["annotations"]["readOnlyHint"] is False
        props = entry["inputSchema"]["properties"]
        assert props["action"]["enum"] == list(et._ACTIONS)
        assert "set_attribute" in props["action"]["enum"]
        assert "delete_attribute" in props["action"]["enum"]
        assert props["to"]["enum"] == list(et._PLACES) + list(et._STEPS)
        assert props["confirm_delete_after_marker"]["type"] == "boolean"
        for prop in ("attribute_group", "attribute_name", "attribute_value"):
            assert props[prop]["type"] == "string"


class TestVerificationPathsBite:
    """The gates that catch a platform LIE - each returned true (or claimed success) while the
    timeline did not actually move."""

    def _items(self, **kw):
        return [FakeTimelineObject(n, i, **kw) for i, n in enumerate(("A", "B", "C"))]

    def test_rollto_that_returns_false_is_an_error(self, wire):
        items = self._items(roll_returns=False)
        wire(FakeTimeline(items, marker=3))
        res = et.handler(action="roll", to="before", feature="B")
        assert res["isError"] is True and "declined to roll" in res["message"]

    def test_rollto_that_left_the_item_in_the_wrong_state_is_an_error(self, wire):
        wire(FakeTimeline(self._items(), marker=3, marker_sticks=False))
        res = et.handler(action="roll", to="before", feature="B")
        assert res["isError"] is True and "isRolledBack" in res["message"]

    def test_an_unconfirmable_roll_that_did_not_move_the_marker_is_an_error(self, wire):
        # the item cannot report isRolledBack, so an unmoved marker is the only evidence left
        items = [FakeTimelineObject(n, i, reports_rolled=False)
                 for i, n in enumerate(("A", "B", "C"))]
        wire(FakeTimeline(items, marker=3, marker_sticks=False))
        res = et.handler(action="roll", to="before", feature="B")
        assert res["isError"] is True and "the roll is unconfirmed" in res["message"]

    def test_a_named_move_that_lands_on_the_wrong_index_is_an_error(self, wire):
        wire(FakeTimeline(self._items(), marker=3, marker_sticks=False))
        res = et.handler(action="roll", to="beginning")
        assert res["isError"] is True and "markerPosition is" in res["message"]

    def test_a_step_move_that_did_not_move_is_an_error(self, wire):
        wire(FakeTimeline(self._items(), marker=2, marker_sticks=False))
        res = et.handler(action="roll", to="previous")
        assert res["isError"] is True and "still" in res["message"]

    def test_a_suppress_that_did_not_take_is_an_error(self, wire):
        items = self._items(suppress_takes=False)
        wire(FakeTimeline(items, marker=3))
        res = et.handler(action="suppress", feature="B", suppressed=True)
        assert res["isError"] is True and "did not take" in res["message"]

    def test_delete_after_marker_that_removed_nothing_is_an_error(self, wire):
        wire(FakeTimeline(self._items(), marker=1, delete_removes=False))
        res = et.handler(action="delete_after_marker", confirm_delete_after_marker=True)
        assert res["isError"] is True and "still holds" in res["message"]
