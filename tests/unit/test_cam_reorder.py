"""Unit tests for ``cam_reorder`` — reorder a CAM operation/folder/pattern before or after another.

The adsk.cam API is mocked; what we pin is the tool's OWN logic: resolving BOTH the moving entity and
the reference entity by name across the setups (operations + folders + patterns, walked recursively —
since allOperations omits folders/patterns), calling OperationBase.moveBefore / moveAfter, turning a
False return into an error, and the guards (no CAM, position not before/after, entity/reference missing,
moving an entity relative to itself).
"""

import json

from conftest import (FakeCAMFolder, FakeOperation, FakeSetup, _NamedCollection, load_tool,
                      make_cam)

cr = load_tool("cam_reorder")


# ── the movable scenario layer over the shared CAM tree fakes ────────────────

class _Movable:
    """A CAM tree item that really MOVES: moveBefore/moveAfter re-seat it in the destination
    parent's own child list, which is what the tool re-reads the order off."""

    _COLL = "operations"     # the parent collection this kind of item lives in

    def __init__(self, *args, allow=True, **kwargs):
        super().__init__(*args, **kwargs)
        self._allow = allow
        self.moved = None    # ('before'|'after', other)
        self._home = None    # the python list its parent collection holds
        self._parent = None

    def _relocate(self, other, before):
        if not self._allow:
            return False
        self.moved = ("before" if before else "after", other)
        if self._home is not None:
            self._home.remove(self)
        dest = getattr(other._parent, self._COLL)._items
        if dest is other._home:
            at = dest.index(other)
            dest.insert(at if before else at + 1, self)
        else:
            # a different collection of the same parent - the two share no ordered list
            dest.append(self)
        self._home, self._parent = dest, other._parent
        return True

    def moveBefore(self, other):
        return self._relocate(other, True)

    def moveAfter(self, other):
        return self._relocate(other, False)


class _Container(_Movable):
    """Wires every child to the list its collection holds, so a move can re-seat it there."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for coll in (self.operations, self.folders, self.patterns):
            for child in coll:
                child._home, child._parent = coll._items, self


class Operation(_Movable, FakeOperation):
    pass


class Setup(_Container, FakeSetup):
    pass


class CAMFolder(_Container, FakeCAMFolder):
    _COLL = "folders"


def _install(monkeypatch, setups=None):
    if setups is None:
        ops = [Operation("Face1"), Operation("Adaptive1"), Operation("Drill1")]
        fol = CAMFolder("Holes", ops=[Operation("Bore1")])
        setups = [Setup("Setup1", ops=ops, folders=[fol])]
    cam = make_cam(*setups)
    monkeypatch.setattr(cr, "get_cam", lambda: (cam, None))
    return cam


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _named(cam, name):
    # helper to fetch an entity for assertions
    s = cam.setups.item(0)
    for coll in (s.operations, s.folders):
        hit = coll.itemByName(name)
        if hit is not None:
            return hit
    for f in s.folders:
        hit = f.operations.itemByName(name)
        if hit is not None:
            return hit
    return None


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_no_cam(self, monkeypatch):
        monkeypatch.setattr(cr, "get_cam", lambda: (None, "no CAM data"))
        res = cr.handler(entity="Face1", position="after", reference="Adaptive1")
        assert res["isError"] is True and "cam" in res["message"].lower()

    def test_bad_position(self, monkeypatch):
        _install(monkeypatch)
        res = cr.handler(entity="Face1", position="sideways", reference="Adaptive1")
        assert res["isError"] is True and "position" in res["message"].lower()

    def test_entity_not_found(self, monkeypatch):
        _install(monkeypatch)
        res = cr.handler(entity="Ghost", position="after", reference="Adaptive1")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_reference_not_found(self, monkeypatch):
        _install(monkeypatch)
        res = cr.handler(entity="Face1", position="after", reference="Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_entity_equals_reference(self, monkeypatch):
        _install(monkeypatch)
        res = cr.handler(entity="Face1", position="after", reference="Face1")
        assert res["isError"] is True
        assert "nothing to reorder" in res["message"]


# ── reorder ──────────────────────────────────────────────────────────────────

class TestReorder:
    def test_move_after(self, monkeypatch):
        cam = _install(monkeypatch)
        out = _payload(cr.handler(entity="Face1", position="after", reference="Drill1"))
        face = _named(cam, "Face1")
        assert face.moved == ("after", _named(cam, "Drill1"))
        assert out["moved"] == "Face1" and out["position"] == "after" and out["reference"] == "Drill1"

    def test_move_before(self, monkeypatch):
        cam = _install(monkeypatch)
        cr.handler(entity="Drill1", position="before", reference="Face1")
        assert _named(cam, "Drill1").moved[0] == "before"

    def test_reorder_nested_entity(self, monkeypatch):
        # the moving entity can be inside a folder (Bore1) — resolved by the recursive walk
        cam = _install(monkeypatch)
        out = _payload(cr.handler(entity="Bore1", position="before", reference="Face1"))
        assert _named(cam, "Bore1").moved[0] == "before"
        assert out["moved"] == "Bore1"

    def test_move_declined_is_error(self, monkeypatch):
        # moveBefore/After returning False (an illegal move) must be a hard error, not a false ok
        s = Setup("Setup1", ops=[Operation("A", allow=False), Operation("B")])
        _install(monkeypatch, [s])
        res = cr.handler(entity="A", position="after", reference="B")
        assert res["isError"] is True and ("not allowed" in res["message"].lower()
                                           or "declin" in res["message"].lower())


# ── a SETUP is rootless: its ordered row is the document's own setups collection ─────────────────

class MovableSetup(Setup):
    """A Setup that really moves inside cam.setups - the one ordered list the document's setups
    live in, and the list the tool re-reads through _cam_common.setup_names."""

    def _relocate(self, other, before):
        if not self._allow:
            return False
        self.moved = ("before" if before else "after", other)
        row = self._home
        row.remove(self)
        at = row.index(other)
        row.insert(at if before else at + 1, self)
        return True


class _StuckSetup(MovableSetup):
    """The setup move Fusion allows and does not make - the swallowed write a bool cannot catch."""
    def moveAfter(self, other):
        return True


def _install_setups(monkeypatch, *setups):
    """Install a document whose SETUPS are the movable row, each wired to that one list."""
    cam = make_cam(*setups)
    for s in setups:
        s._home = cam.setups._items
    monkeypatch.setattr(cr, "get_cam", lambda: (cam, None))
    return cam


class TestSetupOrder:
    """Setup order is the order a job runs in - turning before milling on a mill-turn - and creation
    order was the only way to reach it. The move is judged by re-reading cam.setups, like every other
    kind: an allowed move that did not land is an error."""

    def test_a_setup_moves_inside_the_documents_setups_and_the_order_is_read_back(self, monkeypatch):
        _install_setups(monkeypatch, MovableSetup("Mill"), MovableSetup("Turn"),
                        MovableSetup("Flip"))
        out = _payload(cr.handler(entity="Turn", position="before", reference="Mill"))
        assert out["order"] == ["Turn", "Mill", "Flip"]
        assert out["moved"] == "Turn" and out["entity_index"] == 0 and out["reference_index"] == 1
        assert "the document's setups" in out["note"]

    def test_a_setup_move_the_platform_allows_but_does_not_make_is_an_error(self, monkeypatch):
        # the same gate every other kind runs: moveBefore answering true is not the setups
        # collection having changed.
        _install_setups(monkeypatch, _StuckSetup("Mill"), MovableSetup("Turn"))
        res = cr.handler(entity="Mill", position="after", reference="Turn")
        assert res["isError"] is True and "did not land" in res["message"]
        assert "the document's setups" in res["message"]


# ── the order is RE-READ, never echoed ───────────────────────────────────────

class _StuckMover(Operation):
    """moveBefore/moveAfter answer TRUE and leave the tree exactly as it was - the swallowed move
    nothing but a re-read of the order can catch."""
    def moveBefore(self, other):
        return True
    def moveAfter(self, other):
        return True


class _WrongSideMover(Operation):
    """The move lands, but on the OTHER side of the reference - the off-by-one a bool gate passes."""
    def moveAfter(self, other):
        return self._relocate(other, True)


class _VanishingMover(Operation):
    """The move answers true and takes the item out of the tree - it resolves no more."""
    def moveAfter(self, other):
        self._home.remove(self)
        self._home = None
        return True


class _AllOperationsOnly(_Movable):
    """A container that answers only allOperations - the walk's degraded path. Its children are in
    the tree, but the parent exposes no per-kind collection to re-read an order off, which no
    shared CAM fake models (FakeCAMFolder always answers all three)."""

    _COLL = "patterns"

    def __init__(self, name, ops=()):
        super().__init__()
        self.name = name
        self.allOperations = _NamedCollection(list(ops))
        for child in self.allOperations:
            child._home, child._parent = self.allOperations._items, self


class TestLyingMove:
    def test_a_move_that_left_the_order_alone_errors(self, monkeypatch):
        # the bool says Fusion allowed it and the sibling order is untouched: only the re-read of
        # the order separates that from a real move, and it must be an error, never a false ok.
        s = Setup("Setup1", ops=[_StuckMover("Face1"), Operation("Drill1")])
        _install(monkeypatch, [s])
        res = cr.handler(entity="Face1", position="after", reference="Drill1")
        assert res["isError"] is True
        assert "did not land" in res["message"]
        # the refusal names the order it read and the one the request asks for
        assert "gives ['Face1', 'Drill1']" in res["message"]
        assert "leaves ['Drill1', 'Face1']" in res["message"]

    def test_a_swallowed_move_that_was_already_on_the_asked_side_errors(self, monkeypatch):
        # the entity ALREADY sits after the reference, just not next to it: a gate that only asks
        # which SIDE of the reference it ended on passes this swallowed move. What was asked for is
        # a landing NEXT to 'Drill1', and the re-read order does not show one.
        s = Setup("Setup1", ops=[Operation("Drill1"), Operation("Adaptive1"), _StuckMover("Face1")])
        _install(monkeypatch, [s])
        res = cr.handler(entity="Face1", position="after", reference="Drill1")
        assert res["isError"] is True and "did not land" in res["message"]
        assert "gives ['Drill1', 'Adaptive1', 'Face1']" in res["message"]
        assert "leaves ['Drill1', 'Face1', 'Adaptive1']" in res["message"]

    def test_a_move_landing_on_the_wrong_side_of_the_reference_errors(self, monkeypatch):
        # the exact boundary of the before/after placement: the item really moved and sits ADJACENT
        # to the reference, one index off on the wrong side. 'after' asked for index 1, it took 0.
        s = Setup("Setup1", ops=[Operation("Drill1"), _WrongSideMover("Face1")])
        _install(monkeypatch, [s])
        res = cr.handler(entity="Face1", position="after", reference="Drill1")
        assert res["isError"] is True and "did not land" in res["message"]
        assert "gives ['Face1', 'Drill1']" in res["message"]
        assert "leaves ['Drill1', 'Face1']" in res["message"]

    def test_an_adjacent_landing_on_the_asked_side_passes(self, monkeypatch):
        # the other side of that same boundary: index ref+1 for 'after' is exactly what was asked
        s = Setup("Setup1", ops=[Operation("Drill1"), Operation("Face1")])
        _install(monkeypatch, [s])
        out = _payload(cr.handler(entity="Face1", position="after", reference="Drill1"))
        assert out["entity_index"] == 1 and out["reference_index"] == 0

    def test_an_item_that_left_the_tree_is_not_reported_as_moved(self, monkeypatch):
        s = Setup("Setup1", ops=[_VanishingMover("Face1"), Operation("Drill1")])
        _install(monkeypatch, [s])
        res = cr.handler(entity="Face1", position="after", reference="Drill1")
        assert res["isError"] is True and "did not land" in res["message"]
        assert "gives ['Drill1']" in res["message"]

    def test_an_ordinal_address_is_read_back_by_the_node_it_named(self, monkeypatch):
        # two operations share a name, so the caller addresses one as 'Face1#2'. That address counts
        # over the WALK's order, which the move itself changes - re-resolving the string after the
        # move would check a different node and call this landed move a no-take. The row it leaves
        # carries two 'Face1', so the order is published unverified rather than refused.
        drill, first, second = Operation("Drill1"), Operation("Face1"), Operation("Face1")
        s = Setup("Setup1", ops=[drill, first, second])
        _install(monkeypatch, [s])
        out = _payload(cr.handler(entity="Face1#2", position="before", reference="Drill1"))
        assert second.moved == ("before", drill) and first.moved is None
        assert out["order"] == ["Face1", "Drill1", "Face1"]
        assert out["order_unverified"] is True and "entity_index" not in out

    def test_two_spellings_of_one_item_are_refused(self, monkeypatch):
        # the by-name resolve is case-insensitive, so two different spellings can name ONE item -
        # which has no order to land in relative to itself.
        _install(monkeypatch)
        res = cr.handler(entity="Face1", position="after", reference="face1")
        assert res["isError"] is True and "SAME item" in res["message"]
        assert "Setup1 / Face1" in res["message"]


class TestPublishedOrder:
    def test_the_payload_publishes_the_reread_order_not_the_request(self, monkeypatch):
        cam = _install(monkeypatch)
        out = _payload(cr.handler(entity="Face1", position="after", reference="Drill1"))
        # ops began [Face1, Adaptive1, Drill1]; Face1 now sits last, read off the tree
        assert out["order"] == ["Adaptive1", "Drill1", "Face1"]
        assert out["entity_index"] == 2 and out["reference_index"] == 1
        assert "order_unverified" not in out
        assert _named(cam, "Face1").moved[0] == "after"

    def test_the_published_names_are_the_trees_own_spelling(self, monkeypatch):
        # the resolver is case-insensitive, so 'moved'/'reference' must carry the names the tree
        # answers with rather than the spelling the caller happened to type
        _install(monkeypatch)
        out = _payload(cr.handler(entity="face1", position="before", reference="drill1"))
        assert out["moved"] == "Face1" and out["reference"] == "Drill1"

    def test_two_items_with_no_shared_order_publish_none_and_say_so(self, monkeypatch):
        # an operation moved relative to a FOLDER: a parent holds operations and folders in two
        # separate collections, so there is no one list to place them in - and the payload must say
        # the order was not read rather than claim a landing it never saw.
        s = Setup("Setup1", ops=[Operation("Face1")], folders=[CAMFolder("Holes")])
        _install(monkeypatch, [s])
        out = _payload(cr.handler(entity="Face1", position="before", reference="Holes"))
        assert out["order"] is None and out["order_unverified"] is True
        assert "could NOT be read back" in out["note"]
        assert "'Face1' is an item of kind operation" in out["note"]
        assert "'Holes' is an item of kind folder" in out["note"]
        assert "separate collections" in out["note"]
        assert "entity_index" not in out and "reference_index" not in out

    def test_a_row_holding_two_of_the_movers_name_publishes_the_order_unverified(self, monkeypatch):
        # the mover's move is swallowed and a SIBLING carries its name: the re-read row of names is
        # exactly the one the requested move leaves, so a name compare passes over a move that
        # never happened. Nothing in the row says which 'Face1' sits at that index, so the payload
        # publishes the re-read without an index rather than attributing the landing.
        stuck, twin = _StuckMover("Face1"), Operation("Face1")
        s = Setup("Setup1", ops=[stuck, twin, Operation("Drill1")])
        _install(monkeypatch, [s])
        out = _payload(cr.handler(entity="Face1#1", position="before", reference="Drill1"))
        assert stuck.moved is None                       # the move never happened
        assert out["order"] == ["Face1", "Face1", "Drill1"]
        assert out["order_unverified"] is True
        assert "entity_index" not in out and "reference_index" not in out
        assert out["moved"] == "Face1" and out["reference"] == "Drill1"
        assert "2 items named 'Face1'" in out["note"]

    def test_the_same_row_shape_with_unique_names_still_publishes_an_index(self, monkeypatch):
        # one item under the mover's name: the row of names now tells the mover from its siblings,
        # so the landing IS measured and the index is published.
        s = Setup("Setup1", ops=[Operation("Face1"), Operation("Adaptive1"), Operation("Drill1")])
        _install(monkeypatch, [s])
        out = _payload(cr.handler(entity="Face1", position="before", reference="Drill1"))
        assert out["order"] == ["Adaptive1", "Face1", "Drill1"]
        assert out["entity_index"] == 1 and out["reference_index"] == 2
        assert "order_unverified" not in out

    def test_a_parent_that_answers_no_collection_publishes_no_order(self, monkeypatch):
        # the two items ARE same-kind children of one parent, but that parent answers no
        # 'operations' collection to re-read - so there is no order to judge the landing by, and
        # the payload says the order was not read rather than passing OR failing the move on it.
        pat = _AllOperationsOnly("Pattern1", ops=[_StuckMover("A"), _StuckMover("B")])
        _install(monkeypatch, [Setup("Setup1", patterns=[pat])])
        out = _payload(cr.handler(entity="A", position="after", reference="B"))
        assert out["order"] is None and out["order_unverified"] is True
        assert "could NOT be read back" in out["note"]
        assert "no 'operations' collection" in out["note"]
