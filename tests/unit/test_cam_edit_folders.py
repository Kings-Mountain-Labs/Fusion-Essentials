"""Unit tests for ``cam_edit_folders`` — interrogate / create / rename CAM folders + move operations in.

The adsk.cam API is mocked; what we pin is the tool's OWN action dispatch + logic: listing a setup's
folders (with their contained operation/pattern/subfolder counts), creating a folder
(CAMFolders.addFolder), renaming one, and moving named operations INTO a folder (OperationBase.moveInto).
Plus the guards (no CAM, setup/folder not found, unknown action, an operation that doesn't exist).

Pattern CREATION is intentionally NOT here — the API refuses it ("Strategy is not exposed"); this tool
is folders + moving existing ops, and says so.
"""

import json

from conftest import FakeCAMFolder, FakeOperation, FakeSetup, _NamedCollection, load_tool, make_cam

cf = load_tool("cam_edit_folders")


# ── the movable CAM tree ─────────────────────────────────────────────────────

def _adopt(*collections):
    """Tell each child which collection it currently sits in, so a move can take it out again."""
    for coll in collections:
        for child in coll:
            child._home = coll._items


class _Movable:
    """A CAM item that really MOVES: moveInto takes it out of the collection it sits in and adds it
    to the destination's matching child collection, which is the membership the tool reads back."""

    _COLL = "operations"     # the destination collection this kind of item joins

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.moved_into = None
        self._home = None

    def moveInto(self, parent):
        self.moved_into = parent
        if self._home is not None:
            self._home.remove(self)
        self._home = getattr(parent, self._COLL)._items
        self._home.append(self)
        return True


class _Op(_Movable, FakeOperation):
    pass


class _Folder(_Movable, FakeCAMFolder):
    _COLL = "folders"

    def __init__(self, name, ops=(), patterns=(), folders=()):
        super().__init__(name, ops=ops, patterns=patterns, folders=folders)
        _adopt(self.operations, self.patterns, self.folders)


class _Folders(_NamedCollection):
    """A setup's CAMFolders: the shared walk plus addFolder, which appends the new folder and hands
    it back - `added` is the mutation record."""

    def __init__(self, folders=()):
        super().__init__(folders)
        self.added = []

    def addFolder(self, name):
        f = _Folder(name)
        self._items.append(f)
        self.added.append(f)
        f._home = self._items
        return f


class _Setup(FakeSetup):
    """A setup whose folders collection takes addFolder, and whose children know where they sit."""

    def __init__(self, name, ops=(), folders=()):
        super().__init__(name, ops=ops)
        self.folders = _Folders(folders)
        _adopt(self.operations, self.folders)


def _install(monkeypatch, setups=None):
    # default: one setup with 2 loose ops and 1 folder containing 1 op
    if setups is None:
        folder = _Folder("Holes", ops=[_Op("Drill1")])
        setups = [_Setup("Setup1", ops=[_Op("Face1"), _Op("Adaptive1")], folders=[folder])]
    cam = make_cam(*setups)
    monkeypatch.setattr(cf, "get_cam", lambda: (cam, None))
    return cam


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# ── guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    def test_no_cam(self, monkeypatch):
        monkeypatch.setattr(cf, "get_cam", lambda: (None, "no CAM data"))
        res = cf.handler(action="list", setup="Setup1")
        assert res["isError"] is True and "cam" in res["message"].lower()

    def test_unknown_action(self, monkeypatch):
        _install(monkeypatch)
        res = cf.handler(action="teleport", setup="Setup1")
        assert res["isError"] is True and "action" in res["message"].lower()

    def test_setup_not_found(self, monkeypatch):
        _install(monkeypatch)
        res = cf.handler(action="list", setup="Ghost")
        assert res["isError"] is True and "Ghost" in res["message"]


# ── list ─────────────────────────────────────────────────────────────────────

class TestList:
    def test_lists_folders_with_contents(self, monkeypatch):
        _install(monkeypatch)
        out = _payload(cf.handler(action="list", setup="Setup1"))
        assert out["folder_count"] == 1
        f = out["folders"][0]
        assert f["name"] == "Holes" and f["operations"] == 1


# ── create ───────────────────────────────────────────────────────────────────

class TestCreate:
    def test_create_folder(self, monkeypatch):
        cam = _install(monkeypatch)
        out = _payload(cf.handler(action="create", setup="Setup1", name="Finishing"))
        assert out["folder"] == "Finishing"
        assert cam.setups.item(0).folders.itemByName("Finishing") is not None

    def test_create_requires_name(self, monkeypatch):
        _install(monkeypatch)
        res = cf.handler(action="create", setup="Setup1")
        assert res["isError"] is True and "name" in res["message"].lower()


# ── rename ───────────────────────────────────────────────────────────────────

class TestRename:
    def test_rename_folder(self, monkeypatch):
        cam = _install(monkeypatch)
        out = _payload(cf.handler(action="rename", setup="Setup1", folder="Holes", new_name="Drilling"))
        assert out["renamed"] is True and out["to"] == "Drilling"
        assert cam.setups.item(0).folders.itemByName("Drilling") is not None

    def test_rename_unknown_folder(self, monkeypatch):
        _install(monkeypatch)
        res = cf.handler(action="rename", setup="Setup1", folder="Nope", new_name="X")
        assert res["isError"] is True and "Nope" in res["message"]


# ── move operations into a folder ───────────────────────────────────────────

class TestMove:
    def test_move_ops_into_folder(self, monkeypatch):
        cam = _install(monkeypatch)
        out = _payload(cf.handler(action="move", setup="Setup1", folder="Holes",
                                  operations=["Face1", "Adaptive1"]))
        assert out["moved"] == 2
        # the ops left the setup's own list and the Holes folder now carries them
        setup = cam.setups.item(0)
        holes = setup.folders.itemByName("Holes")
        assert setup.operations.itemByName("Face1") is None
        face = holes.operations.itemByName("Face1")
        assert face is not None and face.moved_into is holes

    def test_move_unknown_operation(self, monkeypatch):
        _install(monkeypatch)
        res = cf.handler(action="move", setup="Setup1", folder="Holes", operations=["Ghost"])
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_move_requires_ops_and_folder(self, monkeypatch):
        _install(monkeypatch)
        res = cf.handler(action="move", setup="Setup1", folder="Holes")
        assert res["isError"] is True
        assert "'operations' (names to move into it)" in res["message"]

    def test_duplicate_move_target_name_is_refused(self, monkeypatch):
        # "Drill1" exists in TWO folders of the same setup - moving by that name must REFUSE with
        # both paths, never silently move whichever the walk met first.
        f1 = _Folder("Rough", ops=[_Op("Drill1")])
        f2 = _Folder("Finish", ops=[_Op("Drill1")])
        dest = _Folder("Holes")
        setup = _Setup("Setup1", ops=[], folders=[f1, f2, dest])
        monkeypatch.setattr(cf, "get_cam", lambda: (make_cam(setup), None))
        res = cf.handler(action="move", setup="Setup1", folder="Holes", operations=["Drill1"])
        assert res["isError"] is True and "ambiguous" in res["message"].lower()
        assert "Setup1 / Rough / Drill1" in res["message"]
        assert "Setup1 / Finish / Drill1" in res["message"]

    def test_folder_into_folder_move_resolves(self, monkeypatch):
        # allOperations (the prior lookup) omits folders entirely, so moving a FOLDER into
        # another folder needs _find_op to walk .folders too, not just flat operations.
        inner = _Folder("Inner")
        outer = _Folder("Outer")
        setup = _Setup("Setup1", ops=[], folders=[inner, outer])
        cam = make_cam(setup)
        monkeypatch.setattr(cf, "get_cam", lambda: (cam, None))
        out = _payload(cf.handler(action="move", setup="Setup1", folder="Outer",
                                  operations=["Inner"]))
        assert out["moved"] == 1
        assert inner.moved_into is outer


class TestNestedFolderTargets:
    def test_nested_folder_can_be_renamed_and_receive_an_operation(self, monkeypatch):
        inner = _Folder("Inner")
        op = _Op("Face1")
        setup = _Setup("Setup1", ops=[op], folders=[_Folder("Outer", folders=[inner])])
        _install(monkeypatch, [setup])
        _payload(cf.handler(action="rename", setup="Setup1", folder="Inner", new_name="Finish"))
        out = _payload(cf.handler(action="move", setup="Setup1",
                                  folder="Finish", operations=["Face1"]))
        assert inner.name == "Finish" and op.moved_into is inner and out["moved"] == 1

    def test_duplicate_nested_destinations_refuse_before_move(self, monkeypatch):
        left, right, op = _Folder("Finish"), _Folder("Finish"), _Op("Face1")
        setup = _Setup("Setup1", ops=[op], folders=[_Folder("A", folders=[left]),
                                                   _Folder("B", folders=[right])])
        _install(monkeypatch, [setup])
        result = cf.handler(action="move", setup="Setup1", folder="Finish", operations=["Face1"])
        assert result["isError"] is True and "ambiguous" in result["message"].lower()
        assert op.moved_into is None
        result = cf.handler(action="rename", setup="Setup1", folder="Finish", new_name="Changed")
        assert result["isError"] is True and left.name == right.name == "Finish"
        _payload(cf.handler(action="move", setup="Setup1", folder="Finish#2",
                            operations=["Face1"]))
        assert op.moved_into is right


class _RefusingOp(_Op):
    """moveInto() returns False - Fusion refused the move (e.g. not allowed for this op type)."""
    def moveInto(self, parent):
        return False


class TestMoveRefused:
    def test_moveinto_false_surfaces_as_an_error_not_a_silent_success(self, monkeypatch):
        refusing = _RefusingOp("Locked1")
        folder = _Folder("Holes")
        setup = _Setup("Setup1", ops=[refusing], folders=[folder])
        cam = make_cam(setup)
        monkeypatch.setattr(cf, "get_cam", lambda: (cam, None))
        res = cf.handler(action="move", setup="Setup1", folder="Holes", operations=["Locked1"])
        assert res["isError"] is True and "Locked1" in res["message"]

    def test_moves_completed_before_a_refusal_are_reported_and_kept(self, monkeypatch):
        moved_ok = _Op("Face1")
        refusing = _RefusingOp("Locked1")
        folder = _Folder("Holes")
        setup = _Setup("Setup1", ops=[moved_ok, refusing], folders=[folder])
        cam = make_cam(setup)
        monkeypatch.setattr(cf, "get_cam", lambda: (cam, None))
        res = cf.handler(action="move", setup="Setup1", folder="Holes",
                         operations=["Face1", "Locked1"])
        assert res["isError"] is True
        assert "Face1" in res["message"]       # names what moved before the refusal
        assert moved_ok.moved_into is folder   # that earlier move actually took


# ── the create and the move are READ BACK off the collection ─────────────────

class _StuckOp(_Op):
    """moveInto answers TRUE and the item stays exactly where it was - the swallowed move that only
    a re-read of the destination's membership catches."""
    def moveInto(self, parent):
        self.moved_into = parent
        return True


class _StrandedFolders(_Folders):
    """addFolder hands back a real folder that never joins the setup's own collection."""
    def addFolder(self, name):
        f = _Folder(name)
        self.added.append(f)
        return f


class _NamelessFolders(_Folders):
    """The created folder joins, but its name will not read - nothing to match membership on."""
    def addFolder(self, name):
        f = super().addFolder(name)
        del f.name
        return f


class _RenamingFolders(_Folders):
    """The setup takes the folder under a name of its OWN - what the payload must publish."""
    def addFolder(self, name):
        return super().addFolder(name + "_1")


class _DedupingFolders(_Folders):
    """addFolder hands back the folder that already carries that name (matched without case) rather
    than making a new one, so the collection does not grow."""
    def addFolder(self, name):
        existing = next((f for f in self if f.name.lower() == name.lower()), None)
        return existing if existing is not None else super().addFolder(name)


class _SwappingFolders(_Folders):
    """One folder joins the setup's collection and a DIFFERENT one is handed back - the returned
    folder's own name is not among the names the setup carries."""
    def addFolder(self, name):
        super().addFolder(name)
        return _Folder(name + "_stray")


class TestLyingReturns:
    def test_a_create_that_never_joined_the_setup_errors(self, monkeypatch):
        # addFolder returning a folder is not the same as the SETUP carrying one; without the
        # re-list this is a published created:true over a setup with no new folder.
        setup = _Setup("Setup1")
        setup.folders = _StrandedFolders([])
        monkeypatch.setattr(cf, "get_cam", lambda: (make_cam(setup), None))
        res = cf.handler(action="create", setup="Setup1", name="Milling")
        assert res["isError"] is True
        assert "did not take" in res["message"]
        assert "re-lists 0 folder(s)" in res["message"] and "against 0 before" in res["message"]

    def test_a_create_whose_folder_has_no_readable_name_errors(self, monkeypatch):
        setup = _Setup("Setup1")
        setup.folders = _NamelessFolders([])
        monkeypatch.setattr(cf, "get_cam", lambda: (make_cam(setup), None))
        res = cf.handler(action="create", setup="Setup1", name="Milling")
        assert res["isError"] is True and "reads back as None" in res["message"]

    def test_a_create_that_never_reached_the_setup_under_its_own_name_errors(self, monkeypatch):
        # the folder handed back and the folder the setup carries are not the same one: the list
        # GREW, so the count alone passes - what catches it is that the returned folder's own name
        # is not among the names the setup re-lists, and the payload publishes that name.
        setup = _Setup("Setup1")
        setup.folders = _SwappingFolders([])
        monkeypatch.setattr(cf, "get_cam", lambda: (make_cam(setup), None))
        res = cf.handler(action="create", setup="Setup1", name="Milling")
        assert res["isError"] is True and "did not take" in res["message"]
        assert "reads back as 'Milling_stray'" in res["message"]
        assert "re-lists 1 folder(s) (Milling)" in res["message"]

    def test_a_create_that_handed_back_an_existing_folder_errors(self, monkeypatch):
        # the exact boundary of the growth check: the returned folder's name IS in the re-listed
        # folders either way, so only the LENGTH separates a folder that was made from one that was
        # already there - without the count this reports created:true over a folder it did not make.
        setup = _Setup("Setup1")
        setup.folders = _DedupingFolders([_Folder("Milling")])
        monkeypatch.setattr(cf, "get_cam", lambda: (make_cam(setup), None))
        res = cf.handler(action="create", setup="Setup1", name="milling")
        assert res["isError"] is True and "did not take" in res["message"]
        assert "re-lists 1 folder(s)" in res["message"] and "against 1 before" in res["message"]

    def test_one_more_folder_in_the_list_is_what_makes_a_create(self, monkeypatch):
        # the other side of the same boundary: the collection GREW by one and the returned folder is
        # in it, so the create is confirmed and 'folder_count' is that re-read count
        _install(monkeypatch)
        out = _payload(cf.handler(action="create", setup="Setup1", name="Finishing"))
        assert out["created"] is True and out["folder"] == "Finishing"
        assert out["folder_count"] == 2          # the pre-existing Holes plus this one

    def test_the_published_folder_name_is_the_setups_own(self, monkeypatch):
        setup = _Setup("Setup1")
        setup.folders = _RenamingFolders([])
        monkeypatch.setattr(cf, "get_cam", lambda: (make_cam(setup), None))
        out = _payload(cf.handler(action="create", setup="Setup1", name="Milling"))
        assert out["folder"] == "Milling_1", "the name the folder landed under, not the request"

    def test_a_move_that_left_the_folder_empty_errors(self, monkeypatch):
        # moveInto returning true while the destination's membership is unchanged
        stuck = _StuckOp("Face1")
        folder = _Folder("Holes")
        setup = _Setup("Setup1", ops=[stuck], folders=[folder])
        monkeypatch.setattr(cf, "get_cam", lambda: (make_cam(setup), None))
        res = cf.handler(action="move", setup="Setup1", folder="Holes", operations=["Face1"])
        assert res["isError"] is True
        assert "did not take" in res["message"]
        assert "re-lists 0 item(s)" in res["message"] and "against 0 before this move" in res["message"]

    def test_a_repeated_operation_is_not_counted_as_a_second_move(self, monkeypatch):
        # the same name listed twice in one call: the second moveInto answers true over an
        # operation the first one already put in the folder, and the folder holds no more under
        # that name than it did - without that compare 'moved' counts one operation twice.
        setup = _Setup("Setup1", ops=[_Op("Face1")], folders=[_Folder("Holes")])
        monkeypatch.setattr(cf, "get_cam", lambda: (make_cam(setup), None))
        out = _payload(cf.handler(action="move", setup="Setup1", folder="Holes",
                                  operations=["Face1", "Face1"]))
        assert out["moved"] == 1 and out["operations"] == ["Face1"]
        assert out["unattributed"] == ["Face1"]

    def test_a_move_into_a_folder_that_holds_the_same_name_still_counts(self, monkeypatch):
        # the destination already carries an operation named 'Drill1' and the one being moved is
        # another: the folder holding that NAME is not the item being moved, so what says the move
        # landed is that the folder now holds one MORE item under it.
        loose = _Op("Drill1")
        setup = _Setup("Setup1", ops=[loose], folders=[_Folder("Holes", ops=[_Op("Drill1")])])
        monkeypatch.setattr(cf, "get_cam", lambda: (make_cam(setup), None))
        out = _payload(cf.handler(action="move", setup="Setup1", folder="Holes",
                                  operations=["Drill1#1"]))
        assert out["moved"] == 1 and out["operations"] == ["Drill1"]
        assert "unattributed" not in out
        assert setup.folders.itemByName("Holes").operations.count == 2

    def test_an_operation_already_in_the_folder_is_landed_not_refused(self, monkeypatch):
        # the requested end state already holds: the operation is in 'Holes' before the call and
        # reads back there after it. Nothing joined the folder, so nothing is counted as moved -
        # but calling that a no-take would refuse exactly what was asked for.
        setup = _Setup("Setup1", folders=[_Folder("Holes", ops=[_Op("Face1")])])
        monkeypatch.setattr(cf, "get_cam", lambda: (make_cam(setup), None))
        out = _payload(cf.handler(action="move", setup="Setup1", folder="Holes",
                                  operations=["Face1"]))
        assert out["moved"] == 0 and out["operations"] == []
        assert out["unattributed"] == ["Face1"]
        assert "1 item(s) named 'Face1' before that move and 1 after" in out["note"]
        assert "was not measured" in out["note"]

    def test_a_swallowed_move_under_a_name_the_folder_holds_is_not_called_already_there(
            self, monkeypatch):
        # the folder already lists a DIFFERENT item under the moved item's name, and this move is
        # swallowed: the count under 'Drill1' is 1 before and 1 after, exactly what an item that
        # was already in the folder reads. The membership cannot say which of the two happened, so
        # the payload discloses that rather than publishing an end state it did not measure.
        stuck = _StuckOp("Drill1")
        holes = _Folder("Holes", ops=[_Op("Drill1")])
        setup = _Setup("Setup1", ops=[stuck], folders=[holes])
        monkeypatch.setattr(cf, "get_cam", lambda: (make_cam(setup), None))
        out = _payload(cf.handler(action="move", setup="Setup1", folder="Holes",
                                  operations=["Drill1#1"]))
        assert out["moved"] == 0 and out["operations"] == []
        assert out["unattributed"] == ["Drill1"]
        assert "already_there" not in out
        assert "1 item(s) named 'Drill1' before that move and 1 after" in out["note"]
        assert "was not measured" in out["note"]
        # the item never left the setup, and the folder holds only the Drill1 it already had
        assert setup.operations.count == 1 and holes.operations.count == 1

    def test_a_move_that_stalls_partway_names_what_had_landed(self, monkeypatch):
        # the first op really moves, the second is swallowed: the error must credit the first
        real, stuck = _Op("Face1"), _StuckOp("Adaptive1")
        folder = _Folder("Holes")
        setup = _Setup("Setup1", ops=[real, stuck], folders=[folder])
        monkeypatch.setattr(cf, "get_cam", lambda: (make_cam(setup), None))
        res = cf.handler(action="move", setup="Setup1", folder="Holes",
                         operations=["Face1", "Adaptive1"])
        assert res["isError"] is True
        assert "Move of 'Adaptive1'" in res["message"]
        assert "Moved so far: Face1" in res["message"]
        assert folder.operations.itemByName("Face1") is not None

    def test_the_published_operation_names_come_from_the_destination(self, monkeypatch):
        # 'operations' is what the folder carries the moved items under, read back off it
        cam = _install(monkeypatch)
        out = _payload(cf.handler(action="move", setup="Setup1", folder="Holes",
                                  operations=["face1"]))
        assert out["moved"] == 1 and out["operations"] == ["Face1"]
        holes = cam.setups.item(0).folders.itemByName("Holes")
        assert holes.operations.count == 2       # the pre-existing Drill1 plus Face1
