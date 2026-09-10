"""Unit tests for ``cam_post`` - create-or-reuse an NC Program for the scope, then post it to disk.

The adsk.cam API is mocked. What we pin is the tool's OWN logic: resolving the .cps post config,
refusing up front when nothing valid can post (via live_readiness), scoping document vs a named setup,
CREATE-OR-REUSE (an existing NC Program of the same name is updated, never duplicated), the output
parameters/operations/post reaching the program, orphan cleanup of a just-created program behind a
failed post, and - the honesty gate - success being contingent on a real file LANDING on disk
(postProcess returning true is not proof), including the partial case where the API flags failure but
a file appeared.

PostConfiguration.createFromContent and NCProgramPostProcessOptions.create are patched to inert
carriers; the fake NCPrograms collection acts on the real output folder the handler set via the
program's parameters, writing (or not writing) a file there.
"""

import itertools
import json
import os
import types

from conftest import (FakeCAMParameter, FakeCAMParameters, FakeOperation, FakeSetup,
                      _NamedCollection, _make_object_collection, load_tool, make_cam)

cp = load_tool("cam_post")
cc = load_tool("_cam_common")   # the readiness sentence the note is measured against is ITS build


# -- fakes ---------------------------------------------------------------------

def _unq(expr):
    s = str(expr)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


class _ChoiceParam(FakeCAMParameter):
    """A ChoiceParameterValue-shaped param: getChoices() -> (ok, names, values) out-params, and
    .value legal only as one of those values (the live binding rejects anything else)."""
    def __init__(self, name, names, values, current):
        super().__init__(name, value=current)
        legal = list(values)
        self.value.getChoices = lambda: (True, list(names), legal)


def _make_params(missing=()):
    """The output parameters an NC program exposes. `missing` drops names to simulate a program that
    lacks a parameter (e.g. no output-folder param)."""
    params = {
        "nc_program_name": FakeCAMParameter("nc_program_name"),
        "nc_program_output_folder": FakeCAMParameter("nc_program_output_folder"),
        "nc_program_comment": FakeCAMParameter("nc_program_comment"),
        "nc_program_openInEditor": FakeCAMParameter("nc_program_openInEditor", value=True),
        "nc_program_unit": _ChoiceParam("nc_program_unit", ["Document unit", "Inches", "Millimeters"],
                                        ["$doc", "$in", "$mm"], "$doc"),
    }
    for n in missing:
        params.pop(n, None)
    return FakeCAMParameters(list(params.values()))


class _NCInput:
    def __init__(self, missing=()):
        self.displayName = None
        self.operations = None
        self.parameters = _make_params(missing)


class _NCProgram:
    """MEASURED shape: .operations holds the SETUPS/folders assigned to the program, while
    .filteredOperations holds every Operation in that scope, posted or not."""
    def __init__(self, name, cam, missing=(), has_error=False):
        self.name = name
        self._cam = cam
        self.operations = None
        self.postConfiguration = None
        self.parameters = _make_params(missing)
        self.deleted = False
        self.hasError = has_error
        self.error = "toolpath fault" if has_error else None

    @property
    def filteredOperations(self):
        out = []
        for it in (self.operations or []):
            nested = getattr(it, "allOperations", None)
            out.extend([it] if nested is None else list(nested))
        return out
    def postProcess(self, options):
        return self._cam._do_post(self)
    def deleteMe(self):
        self.deleted = True
        self._cam._remove(self)
        return True


class _AmnesicProgram(_NCProgram):
    """Accepts the operations assignment and stores NOTHING - the swallowed scope write."""
    @property
    def operations(self):
        return []
    @operations.setter
    def operations(self, value):
        pass


class _UnfilterableProgram(_NCProgram):
    """filteredOperations RAISES - the read that answers nothing."""
    @property
    def filteredOperations(self):
        raise RuntimeError("filteredOperations is unavailable on this program")


class _PartialProgram(_NCProgram):
    """Keeps only the FIRST item of an assigned scope - a membership that disagrees with the
    request without being empty."""
    @property
    def operations(self):
        return self._ops
    @operations.setter
    def operations(self, value):
        self._ops = list(value or [])[:1]


class _UnreadableMembershipProgram(_NCProgram):
    """The operations assignment is accepted; reading them back raises."""
    @property
    def operations(self):
        raise RuntimeError("operations unavailable")
    @operations.setter
    def operations(self, value):
        pass


class _UndeletableProgram(_NCProgram):
    """deleteMe() DECLINES: it answers false and the program stays in the collection."""
    def deleteMe(self):
        return False


class _DeleteRaisesProgram(_NCProgram):
    """deleteMe() raises, leaving the program in the collection."""
    def deleteMe(self):
        raise RuntimeError("the program is in use")


class _SurvivesDeleteProgram(_NCProgram):
    """deleteMe() answers TRUE and the program still resolves in ncPrograms - the bool that is
    not the effect."""
    def deleteMe(self):
        return True


class _NCPrograms(_NamedCollection):
    """cam.ncPrograms: the shared counted/by-name walk plus createInput/add."""
    def __init__(self, cam, missing=(), has_error=False, program_class=None):
        super().__init__()
        self._cam = cam
        self._missing = missing
        self._has_error = has_error
        self._class = program_class or _NCProgram
        self.create_calls = 0
        self.add_calls = 0
    def createInput(self):
        self.create_calls += 1
        return _NCInput(self._missing)
    def add(self, nc_input):
        self.add_calls += 1
        prog = self._class(nc_input.displayName, self._cam, has_error=self._has_error)
        prog.parameters = nc_input.parameters       # the params the handler set on the input
        prog.operations = nc_input.operations
        self._items.append(prog)
        return prog


class _Op(FakeOperation):
    """An Operation carrying operationId - the identity the overwrite guard compares on. These
    fakes hold the guard's own premise: distinct objects sharing one operationId; the live
    stability of that id across fetches is CAM-1's measurement."""
    _seq = itertools.count(1)

    def __init__(self, name, operation_id=None, has_toolpath=True):
        # what the post EMITS: an operation in the program's scope with no toolpath is held and
        # not written out (measured - four held ops, one operation block in the file)
        super().__init__(name, has_toolpath=has_toolpath)
        self.operationId = next(self._seq) if operation_id is None else operation_id


class _ToolpathlessOp(_Op):
    """An operation whose hasToolpath flag cannot be read - the row that is counted as neither
    posted nor held-with-a-path."""
    @property
    def hasToolpath(self):
        raise RuntimeError("hasToolpath is unavailable on this object")

    @hasToolpath.setter
    def hasToolpath(self, value):
        pass


class _IdlessOp(_Op):
    """An operation whose operationId cannot be read - reading it raises, as a stale/invalid proxy
    does."""
    @property
    def operationId(self):
        raise RuntimeError("operationId is unavailable on this object")

    @operationId.setter
    def operationId(self, value):
        pass


def _Setup(name, ops=()):
    """A setup as the post's scope walk reads it."""
    return FakeSetup(name, ops=ops)


def _CAM(setups, writes=True, returns=True, existing=(), missing=(),
         program_error=False, program_class=None):
    """A CAM product whose ncPrograms post into the output folder the handler wrote onto the
    program's own parameters. `writes` False lands no file; 'failed' lands the post's own stub."""
    cam = make_cam(*setups)
    cam.personalPostFolder = "C:/nonexistent/personal"
    cam.genericPostFolder = "C:/nonexistent/generic"
    cam.posted = []

    def _do_post(program):
        cam.posted.append(program)
        folder = _unq(program.parameters.itemByName("nc_program_output_folder").expression)
        name = _unq(program.parameters.itemByName("nc_program_name").expression)
        if writes == "failed":
            # a failed post leaves only a '.failed' stub in the output folder (the real error is in
            # the post log, elsewhere) and postProcess returns False.
            with open(os.path.join(folder, str(name) + ".nc.failed"), "w") as f:
                f.write("%\n!Error: Failed to post data. See log for details.\n")
            return False
        if writes:
            with open(os.path.join(folder, str(name) + ".nc"), "w") as f:
                f.write("%\nO1000\nG0 X0 Y0\nM30\n%\n")
        return returns

    def _remove(program):
        if program in cam.ncPrograms._items:
            cam.ncPrograms._items.remove(program)

    cam._do_post = _do_post
    cam._remove = _remove
    cam.ncPrograms = _NCPrograms(cam, missing, program_error, program_class)
    for name in existing:
        cam.ncPrograms._items.append(
            (program_class or _NCProgram)(name, cam, missing, has_error=program_error))
    return cam


class _URL:
    """A fake adsk.core.URL: a full string plus its leaf name (the section after the last '/')."""
    def __init__(self, s):
        self._s = s
    @property
    def leafName(self):
        return self._s.rstrip("/").rsplit("/", 1)[-1]
    def toString(self):
        return self._s


class _FakePostLibrary:
    """A nested cloud/hub post library. `tree` maps a folder-url string to (child_folder_urls,
    asset_urls); `posts` maps an asset-url string to the PostConfiguration it loads."""
    def __init__(self, root, tree, posts):
        self._root = _URL(root)
        self._tree = tree
        self._posts = posts
    def urlByLocation(self, loc):
        return self._root
    def childFolderURLs(self, url):
        return list(self._tree.get(url.toString(), ([], []))[0])
    def childAssetURLs(self, url):
        return list(self._tree.get(url.toString(), ([], []))[1])
    def postConfigurationAtURL(self, url):
        return self._posts.get(url.toString())


def _cloud_lib_one_post():
    """A library with one post 'Generic Fanuc.cps' nested one folder deep, plus a sibling post."""
    root = "cloud://root"
    vendors = _URL("cloud://root/Vendors")
    fanuc = _URL("cloud://root/Vendors/Generic Fanuc.cps")
    haas = _URL("cloud://root/Vendors/Haas NGC.cps")
    tree = {
        "cloud://root": ([vendors], []),
        "cloud://root/Vendors": ([], [fanuc, haas]),
    }
    posts = {fanuc.toString(): object(), haas.toString(): object()}
    return _FakePostLibrary(root, tree, posts), fanuc.toString()


def _write_cps(tmp_path, name="fanuc.cps"):
    p = tmp_path / name
    p.write_text("// a fake post config\n")
    return p


def _install(monkeypatch, cam, valid=1):
    monkeypatch.setattr(cp, "get_cam", lambda: (cam, None))
    monkeypatch.setattr(cp, "live_readiness",
                        lambda: ({"valid": valid, "readiness": "ready to post."}, None))
    monkeypatch.setattr(cp.adsk.cam.PostConfiguration, "createFromContent",
                        lambda content: object(), raising=False)
    monkeypatch.setattr(cp.adsk.cam.NCProgramPostProcessOptions, "create",
                        lambda: object(), raising=False)
    monkeypatch.setattr(cp.adsk.core.ObjectCollection, "create",
                        _make_object_collection, raising=False)
    return cam


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


# -- guards --------------------------------------------------------------------

class TestGuards:
    def test_no_cam(self, monkeypatch):
        monkeypatch.setattr(cp, "get_cam", lambda: (None, "no CAM data"))
        res = cp.handler(output_folder="x", program_name="1", post="p")
        assert res["isError"] is True and "cam" in res["message"].lower()

    def test_requires_output_folder(self, monkeypatch, tmp_path):
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        res = cp.handler(post=str(_write_cps(tmp_path)), program_name="1")
        assert res["isError"] is True and "output_folder" in res["message"]

    def test_requires_program_name(self, monkeypatch, tmp_path):
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path))
        assert res["isError"] is True and "program_name" in res["message"]

    def test_post_config_not_found(self, monkeypatch, tmp_path):
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        res = cp.handler(post="no_such_post", output_folder=str(tmp_path), program_name="1")
        assert res["isError"] is True and "not found" in res["message"].lower()

    def test_bad_units_rejected(self, monkeypatch, tmp_path):
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                         program_name="1", units="furlongs")
        assert res["isError"] is True and "units" in res["message"].lower()

    def test_refuses_when_no_valid_toolpaths(self, monkeypatch, tmp_path):
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]), valid=0)
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path), program_name="1")
        assert res["isError"] is True and "valid" in res["message"].lower()

    def test_unknown_scope_is_error(self, monkeypatch, tmp_path):
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        res = cp.handler(scope="Ghost", post=str(_write_cps(tmp_path)),
                         output_folder=str(tmp_path), program_name="1")
        assert res["isError"] is True and "Ghost" in res["message"]

    def test_duplicate_scope_name_across_setups_is_refused(self, monkeypatch, tmp_path):
        # "Drill1" exists in TWO setups - posting that scope must REFUSE with both setup paths and
        # post NOTHING, never post whichever setup's op the walk met first.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Drill1")]),
                                          _Setup("S2", [_Op("Drill1")])]))
        res = cp.handler(scope="Drill1", post=str(_write_cps(tmp_path)),
                         output_folder=str(tmp_path), program_name="1")
        assert res["isError"] is True and "ambiguous" in res["message"].lower()
        assert "S1 / Drill1" in res["message"] and "S2 / Drill1" in res["message"]
        assert cam.posted == [] and cam.ncPrograms.count == 0    # no program, no post


# -- post config resolution ----------------------------------------------------

class TestPostResolution:
    def test_resolves_post_by_name_in_personal_folder(self, monkeypatch, tmp_path):
        posts = tmp_path / "posts"
        posts.mkdir()
        _write_cps(posts, "generic fanuc.cps")
        out = tmp_path / "out"
        out.mkdir()
        cam = _CAM([_Setup("S1", [_Op("Face1")])])
        cam.personalPostFolder = str(posts)
        _install(monkeypatch, cam)
        data = _payload(cp.handler(post="generic fanuc", output_folder=str(out), program_name="1"))
        assert data["posted"] is True
        assert data["post_config"].endswith("generic fanuc.cps")


# -- post_scope: cloud/hub team post library -----------------------------------

class TestCloudPostScope:
    def test_local_scope_default_returns_ready_post_configuration(self, monkeypatch, tmp_path):
        # post_scope defaults to local and loads a .cps via createFromContent,
        # returning a ready PostConfiguration (not a path) - the handler does not load it.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        cps = _write_cps(tmp_path)
        pc, label, err = cp._resolve_post_config(cam, str(cps), "local")
        assert err is None and pc is not None
        assert label.endswith("fanuc.cps")

    def test_cloud_resolves_post_by_name_and_posts(self, monkeypatch, tmp_path):
        # THE cloud bite: a name matches an asset url leafName in a nested folder, loads via
        # postConfigurationAtURL, and the handler posts a file with that PostConfiguration.
        lib, fanuc_url = _cloud_lib_one_post()
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        monkeypatch.setattr(cp, "_post_library", lambda: lib)
        data = _payload(cp.handler(post="Generic Fanuc", post_scope="cloud",
                                   output_folder=str(tmp_path), program_name="1001"))
        assert data["posted"] is True and data["post_scope"] == "cloud"
        assert data["post_config"] == fanuc_url          # the label is the matched post url
        prog = cam.ncPrograms.item(0)
        assert prog.postConfiguration is not None         # the loaded PostConfiguration was assigned

    def test_cloud_post_not_found_lists_candidates(self, monkeypatch, tmp_path):
        lib, _ = _cloud_lib_one_post()
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        monkeypatch.setattr(cp, "_post_library", lambda: lib)
        res = cp.handler(post="No Such Post", post_scope="cloud",
                         output_folder=str(tmp_path), program_name="1")
        assert res["isError"] is True
        assert "no cloud post named" in res["message"].lower()
        assert "Generic Fanuc.cps" in res["message"]      # the available posts are listed

    def test_cloud_ambiguous_name_refused(self, monkeypatch, tmp_path):
        # Two posts with the same leaf name in different folders - refuse rather than grab one.
        a = _URL("cloud://root/A/Fanuc.cps")
        b = _URL("cloud://root/B/Fanuc.cps")
        tree = {
            "cloud://root": ([_URL("cloud://root/A"), _URL("cloud://root/B")], []),
            "cloud://root/A": ([], [a]),
            "cloud://root/B": ([], [b]),
        }
        lib = _FakePostLibrary("cloud://root", tree, {a.toString(): object(), b.toString(): object()})
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        monkeypatch.setattr(cp, "_post_library", lambda: lib)
        res = cp.handler(post="Fanuc", post_scope="cloud",
                         output_folder=str(tmp_path), program_name="1")
        assert res["isError"] is True and "ambiguous" in res["message"].lower()


# -- post_scope='fusion': the post library this installation SHIPS ---------------------------------

def _fusion_lib(names, root="fusion://root"):
    """A flat shipped-library fake: one asset url per post name directly under the root."""
    urls = [_URL(root + "/" + n) for n in names]
    return _FakePostLibrary(root, {root: ([], urls)},
                            {u.toString(): object() for u in urls})


class TestFusionPostScope:
    """The lathe and mill-turn posts live in the library this installation ships; post_scope='fusion'
    is the scope that resolves them."""

    def test_fusion_resolves_a_shipped_post_by_case_folded_name(self, monkeypatch, tmp_path):
        # THE fusion bite: the name matches an asset leafName under Fusion360LibraryLocation, the
        # PostConfiguration loads from its url, and the file lands.
        lib = _fusion_lib(["fanuc turning.cps", "haas turning.cps"])
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        monkeypatch.setattr(cp, "_post_library", lambda: lib)
        data = _payload(cp.handler(post="Fanuc Turning", post_scope="fusion",
                                   output_folder=str(tmp_path), program_name="1001"))
        assert data["posted"] is True and data["post_scope"] == "fusion"
        assert data["post_config"] == "fusion://root/fanuc turning.cps"
        assert cam.ncPrograms.item(0).postConfiguration is not None

    def test_a_name_that_is_another_shipped_post_s_prefix_does_not_resolve(self, monkeypatch,
                                                                            tmp_path):
        # The match is EXACT, not containment. Shipped names nest ('acramatic.cps' beside
        # 'acramatic 850sx turning.cps'), so a substring match would hand a caller asking for one
        # post the G-code of another.
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        monkeypatch.setattr(cp, "_post_library", lambda: _fusion_lib(["centroid turning.cps"]))
        res = cp.handler(post="centroid", post_scope="fusion",
                         output_folder=str(tmp_path), program_name="1")
        assert res["isError"] is True
        assert "no fusion post named 'centroid'" in res["message"].lower()

    def test_fusion_ambiguous_name_refused(self, monkeypatch, tmp_path):
        a = _URL("fusion://root/A/fanuc turning.cps")
        b = _URL("fusion://root/B/fanuc turning.cps")
        tree = {
            "fusion://root": ([_URL("fusion://root/A"), _URL("fusion://root/B")], []),
            "fusion://root/A": ([], [a]),
            "fusion://root/B": ([], [b]),
        }
        lib = _FakePostLibrary("fusion://root", tree,
                               {a.toString(): object(), b.toString(): object()})
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        monkeypatch.setattr(cp, "_post_library", lambda: lib)
        res = cp.handler(post="fanuc turning", post_scope="fusion",
                         output_folder=str(tmp_path), program_name="1")
        assert res["isError"] is True and "ambiguous fusion post" in res["message"].lower()
        assert cam.posted == [] and cam.ncPrograms.count == 0

    def test_a_fusion_miss_names_the_scope_and_counts_the_posts_it_did_not_list(self, monkeypatch,
                                                                                 tmp_path):
        # The shipped library answers with hundreds of names, so the listing is capped - and the
        # boundary is the last name listed beside the COUNT of the ones that are not.
        names = ["post %03d.cps" % i for i in range(cp._NAMES_LISTED + 5)]
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        monkeypatch.setattr(cp, "_post_library", lambda: _fusion_lib(names))
        res = cp.handler(post="lathe wizard", post_scope="fusion",
                         output_folder=str(tmp_path), program_name="1")
        assert res["isError"] is True
        assert "no fusion post named 'lathe wizard'" in res["message"].lower()
        assert names[cp._NAMES_LISTED - 1] in res["message"]      # the last name listed
        assert names[cp._NAMES_LISTED] not in res["message"]      # the first one left out
        assert "(+5 more not listed)" in res["message"]           # and they are counted, not dropped

    def test_the_default_scope_stays_local_when_the_shipped_library_holds_the_name_too(
            self, monkeypatch, tmp_path):
        # post_scope defaults to local: a name the shipped library also answers to must still
        # resolve to the .cps on disk, or the fourth scope would quietly re-aim every existing call.
        posts = tmp_path / "posts"
        posts.mkdir()
        cps = _write_cps(posts, "generic fanuc.cps")
        out = tmp_path / "out"
        out.mkdir()
        cam = _CAM([_Setup("S1", [_Op("Face1")])])
        cam.personalPostFolder = str(posts)
        _install(monkeypatch, cam)
        monkeypatch.setattr(cp, "_post_library", lambda: _fusion_lib(["Generic Fanuc.cps"]))
        data = _payload(cp.handler(post="generic fanuc", output_folder=str(out), program_name="1"))
        assert data["post_scope"] == "local"
        assert data["post_config"] == str(cps).replace("\\", "/")

    def test_the_fusion_cap_reaches_the_post_the_team_cap_stops_at(self, monkeypatch, tmp_path):
        # The shipped library holds more posts than a team library's cap admits, so the walk that
        # serves it needs a bound of its own: the wanted post sits at exactly the index the team cap
        # refuses to append.
        team_cap = cp._POST_MAX_ASSETS["cloud"]
        names = ["filler %04d.cps" % i for i in range(team_cap)] + ["fanuc turning.cps"]
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        monkeypatch.setattr(cp, "_post_library", lambda: _fusion_lib(names))
        data = _payload(cp.handler(post="fanuc turning", post_scope="fusion",
                                   output_folder=str(tmp_path), program_name="1001"))
        assert data["posted"] is True and data["post_config"].endswith("fanuc turning.cps")
        res = cp.handler(post="fanuc turning", post_scope="cloud",
                         output_folder=str(tmp_path), program_name="1002")
        assert res["isError"] is True and "capped" in res["message"]


# -- create-new vs reuse -------------------------------------------------------

class TestCreateOrReuse:
    def test_creates_program_when_none_exists(self, monkeypatch, tmp_path):
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        data = _payload(cp.handler(post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="1001"))
        assert data["program_reused"] is False and data["nc_program"] == "created"
        assert cam.ncPrograms.add_calls == 1 and cam.ncPrograms.create_calls == 1
        assert cam.ncPrograms.count == 1                    # exactly one program now exists

    def test_reuses_existing_program_not_duplicated(self, monkeypatch, tmp_path):
        # THE reuse bite: a program already named 'JOB1' must be UPDATED in place, not add()-ed again.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], existing=["JOB1"]))
        data = _payload(cp.handler(post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="JOB1"))
        assert data["program_reused"] is True and data["nc_program"] == "reused"
        assert cam.ncPrograms.add_calls == 0                # never created a second one
        assert cam.ncPrograms.count == 1                    # still exactly one program named JOB1
        prog = cam.ncPrograms.itemByName("JOB1")
        assert prog.postConfiguration is not None           # post config was (re)applied
        assert prog.operations is not None                  # operations were (re)assigned

    def test_output_params_reach_the_program(self, monkeypatch, tmp_path):
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        _payload(cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                            program_name="7", program_comment="rev A", units="mm"))
        prog = cam.ncPrograms.item(0)
        params = prog.parameters
        assert _unq(params.itemByName("nc_program_name").expression) == "7"
        assert _unq(params.itemByName("nc_program_output_folder").expression) == \
            str(tmp_path).replace("\\", "/")
        assert _unq(params.itemByName("nc_program_comment").expression) == "rev A"
        assert params.itemByName("nc_program_openInEditor").value.value is False   # headless
        assert params.itemByName("nc_program_unit").value.value == "$mm"

    def test_operations_collection_carries_the_target_setup(self, monkeypatch, tmp_path):
        s1 = _Setup("Setup1", [_Op("Face1")])
        cam = _install(monkeypatch, _CAM([s1]))
        _payload(cp.handler(scope="Setup1", post=str(_write_cps(tmp_path)),
                            output_folder=str(tmp_path), program_name="9"))
        prog = cam.ncPrograms.item(0)
        assert prog.operations == [s1]                      # a plain LIST of exactly the named setup


# -- as-is mode: an EXISTING program posts exactly as stored, no config writes -------------------

class TestAsIsMode:
    def _configured_existing(self, cam, name, folder, post_config=None):
        """An 'existing' fake program pre-configured as if a prior configure call had run: its own
        output-folder/name params set and a postConfiguration assigned - what as-is mode reads."""
        prog = cam.ncPrograms.itemByName(name)
        prog.parameters.itemByName("nc_program_output_folder").expression = str(folder).replace("\\", "/")
        prog.parameters.itemByName("nc_program_name").expression = name
        prog.postConfiguration = post_config if post_config is not None else object()
        return prog

    def test_as_is_triggers_for_existing_program_with_no_config_knobs(self, monkeypatch, tmp_path):
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], existing=["JOB1"]))
        prog = self._configured_existing(cam, "JOB1", tmp_path)
        data = _payload(cp.handler(program_name="JOB1"))
        assert data["mode"] == "as_is" and data["posted"] is True
        assert data["program_reused"] is True and data["file_count"] == 1
        assert data["files"][0]["file_path"].endswith("JOB1.nc")
        assert os.path.isfile(data["files"][0]["file_path"])

    def test_as_is_writes_no_operations_or_post_config_or_params(self, monkeypatch, tmp_path):
        # THE as-is bite: operations/postConfiguration/output params are untouched - only postProcess runs.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], existing=["JOB1"]))
        sentinel_post = object()
        prog = self._configured_existing(cam, "JOB1", tmp_path, post_config=sentinel_post)
        prog.operations = None                      # never (re)assigned by a prior configure call
        name_param_before = prog.parameters.itemByName("nc_program_name").expression
        data = _payload(cp.handler(program_name="JOB1"))
        assert prog.operations is None               # as-is never sets .operations
        assert prog.postConfiguration is sentinel_post   # as-is never reassigns postConfiguration
        assert prog.parameters.itemByName("nc_program_name").expression == name_param_before
        assert "params_applied" not in data and "post_scope" not in data and "units" not in data

    def test_as_is_refuses_explicit_non_document_units_before_posting(self, monkeypatch, tmp_path):
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], existing=["JOB1"]))
        self._configured_existing(cam, "JOB1", tmp_path)
        result = cp.handler(program_name="JOB1", units="mm")
        assert result["isError"] is True and "as-is" in result["message"]
        assert cam.posted == []

    def test_as_is_not_triggered_when_scope_given_even_as_document(self, monkeypatch, tmp_path):
        # 'scope' was NOT omitted (an explicit "document" still counts as given) - falls to the
        # configure path, which then requires output_folder/post like a fresh configuration would.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], existing=["JOB1"]))
        self._configured_existing(cam, "JOB1", tmp_path)
        res = cp.handler(scope="document", program_name="JOB1")
        assert res["isError"] is True and "output_folder" in res["message"]

    def test_as_is_missing_stored_output_folder_param_is_error(self, monkeypatch, tmp_path):
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], existing=["JOB1"],
                                         missing=("nc_program_output_folder",)))
        res = cp.handler(program_name="JOB1")
        assert res["isError"] is True and "nc_program_output_folder" in res["message"]

    def test_as_is_refuses_when_no_valid_toolpaths(self, monkeypatch, tmp_path):
        # The live_readiness gate applies to as-is too - a stale program still writes wrong G-code.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], existing=["JOB1"]), valid=0)
        self._configured_existing(cam, "JOB1", tmp_path)
        res = cp.handler(program_name="JOB1")
        assert res["isError"] is True and "valid" in res["message"].lower()


class TestBothArmsPublishTheSameCounts:
    """The held/posted counts come off ONE builder, so what a count MEANS cannot depend on which
    arm posted."""

    def _stored(self, cam, name, folder, setup):
        prog = cam.ncPrograms.itemByName(name)
        prog.parameters.itemByName("nc_program_output_folder").expression = str(folder).replace("\\", "/")
        prog.parameters.itemByName("nc_program_name").expression = name
        prog.postConfiguration = object()
        prog.operations = [setup]
        return prog

    def test_as_is_publishes_the_held_and_posted_counts(self, monkeypatch, tmp_path):
        # Both arms publish the counts: a caller comparing two posts of one program would
        # otherwise read a held/posted count from the configured arm and nothing from this one.
        s1 = _Setup("S1", [_Op("Face1"), _Op("Drill1", has_toolpath=False)])
        cam = _install(monkeypatch, _CAM([s1], existing=["JOB1"]))
        self._stored(cam, "JOB1", tmp_path, s1)
        data = _payload(cp.handler(program_name="JOB1"))
        assert data["mode"] == "as_is"
        assert data["program_operation_count"] == 2 and data["posted_operations"] == 1
        assert data["program_item_count"] == 1

    def test_the_configured_arm_reads_the_same_two_counts(self, monkeypatch, tmp_path):
        s1 = _Setup("Setup1", [_Op("Face1"), _Op("Drill1", has_toolpath=False)])
        _install(monkeypatch, _CAM([s1]))
        data = _payload(cp.handler(scope="Setup1", post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        assert data["program_operation_count"] == 2 and data["posted_operations"] == 1

    def test_as_is_publishes_no_membership_verdict(self, monkeypatch, tmp_path):
        # as-is assigned no scope, so there is nothing to compare the program's membership with -
        # a verdict here would report a comparison nobody made.
        s1 = _Setup("S1", [_Op("Face1")])
        cam = _install(monkeypatch, _CAM([s1], existing=["JOB1"]))
        self._stored(cam, "JOB1", tmp_path, s1)
        data = _payload(cp.handler(program_name="JOB1"))
        assert "membership_verified" not in data and "membership_note" not in data

    def test_the_as_is_note_carries_the_same_counts_sentence(self, monkeypatch, tmp_path):
        s1 = _Setup("S1", [_Op("Face1")])
        cam = _install(monkeypatch, _CAM([s1], existing=["JOB1"]))
        self._stored(cam, "JOB1", tmp_path, s1)
        data = _payload(cp.handler(program_name="JOB1"))
        assert cp._COUNTS_NOTE in data["note"]


class TestTheComposedNoteFitsTheWireBudget:
    """The note is assembled at run time from the base, the shared counts sentence and the
    membership clause, so test_prose_budget measures none of the compositions."""

    def _warned_readiness(self, monkeypatch):
        """The readiness sentence _cam_common actually builds for a warned job - the longest form a
        SUCCESSFUL post carries, and not one this test typed itself."""
        line = cc.ready_verdict("6 of 8 active ops valid", 2,
                                {"name": "Bore Deep Holes",
                                 "warning": "Tool is too short for this operation."}, None)
        monkeypatch.setattr(cp, "live_readiness", lambda: ({"valid": 6, "readiness": line}, None))
        return line

    def test_the_configured_note_with_a_membership_disagreement_fits(self, monkeypatch, tmp_path):
        s1, s2 = _Setup("Setup1", [_Op("Face1")]), _Setup("Setup2", [_Op("Drill1")])
        _install(monkeypatch, _CAM([s1, s2], program_class=_PartialProgram))
        line = self._warned_readiness(monkeypatch)
        data = _payload(cp.handler(setups=["Setup1", "Setup2"], post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        assert cp._COUNTS_NOTE in data["note"] and "Membership:" in data["note"]
        assert len(data["note"]) <= 400, len(data["note"])   # test_prose_budget.NOTE_BUDGET_CHARS
        # the unbounded sentence rides as its own key rather than inside the bounded note
        assert data["readiness"] == line and line not in data["note"]

    def test_the_as_is_note_fits_beside_the_same_readiness(self, monkeypatch, tmp_path):
        s1 = _Setup("S1", [_Op("Face1")])
        cam = _install(monkeypatch, _CAM([s1], existing=["JOB1"]))
        TestBothArmsPublishTheSameCounts()._stored(cam, "JOB1", tmp_path, s1)
        line = self._warned_readiness(monkeypatch)
        data = _payload(cp.handler(program_name="JOB1"))
        assert len(data["note"]) <= 400, len(data["note"])
        assert data["readiness"] == line and line not in data["note"]


# -- overwrite guard: reconfiguring an EXISTING program with a DIFFERENT scope --------------------

class TestOverwriteGuard:
    def test_refuses_when_stored_operations_differ_from_requested_scope(self, monkeypatch, tmp_path):
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1", operation_id=11)])],
                                         existing=["JOB1"]))
        prog = cam.ncPrograms.itemByName("JOB1")
        prog.operations = [_Op("SomeOtherOp", operation_id=22)]   # a DIFFERENT stored configuration
        prog.postConfiguration = object()
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                         program_name="JOB1")
        assert res["isError"] is True
        assert "JOB1" in res["message"] and "overwrite" in res["message"].lower()
        assert prog.operations[0].name == "SomeOtherOp"   # refused BEFORE any write - untouched

    def test_overwrite_true_proceeds_despite_differing_operations(self, monkeypatch, tmp_path):
        s1 = _Setup("S1", [_Op("Face1")])
        cam = _install(monkeypatch, _CAM([s1], existing=["JOB1"]))
        prog = cam.ncPrograms.itemByName("JOB1")
        prog.operations = [_Op("SomeOtherOp")]
        data = _payload(cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                                   program_name="JOB1", overwrite=True))
        assert data["posted"] is True
        assert prog.operations == [s1]                   # reconfigured to the requested scope

    def test_identical_operation_sets_proceed_without_overwrite(self, monkeypatch, tmp_path):
        # THE RE-POST BITE: the stored set and the requested scope are the SAME operation reached by
        # two different fetches - DISTINCT Python objects carrying one operationId, which is what the
        # live API hands back. An identity that falls back to id() sees two different sets here and
        # refuses every legitimate re-post.
        s1 = _Setup("S1", [_Op("Face1", operation_id=42)])
        cam = _install(monkeypatch, _CAM([s1], existing=["JOB1"]))
        prog = cam.ncPrograms.itemByName("JOB1")
        stored = _Op("Face1", operation_id=42)
        assert stored is not s1.operations.item(0)
        prog.operations = [stored]
        data = _payload(cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                                   program_name="JOB1"))
        assert data["posted"] is True and data.get("partial") is not True

    def test_an_unreadable_operation_id_refuses_instead_of_guessing(self, monkeypatch, tmp_path):
        # No identity means no comparison: the guard must say so and write nothing, never treat an
        # uncomparable pair as a match (or as a difference) on a stand-in identity.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1", operation_id=42)])],
                                         existing=["JOB1"]))
        prog = cam.ncPrograms.itemByName("JOB1")
        prog.operations = [_IdlessOp("Face1")]
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                         program_name="JOB1")
        assert res["isError"] is True
        assert "JOB1" in res["message"] and "operationId" in res["message"]
        assert isinstance(prog.operations[0], _IdlessOp)   # refused BEFORE any write
        assert cam.posted == []

    def test_an_unreadable_id_on_the_REQUESTED_side_refuses_too(self, monkeypatch, tmp_path):
        # the requested scope holds an operation with no readable id, so the comparable ids ({42})
        # match the stored set while a real difference hides behind the unreadable one - checking
        # only the stored side would reconfigure the program on a comparison that never happened.
        s1 = _Setup("S1", [_Op("Face1", operation_id=42), _IdlessOp("Ghost")])
        cam = _install(monkeypatch, _CAM([s1], existing=["JOB1"]))
        prog = cam.ncPrograms.itemByName("JOB1")
        prog.operations = [_Op("Face1", operation_id=42)]
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                         program_name="JOB1")
        assert res["isError"] is True
        assert "0 stored and 1 requested" in res["message"]
        assert cam.posted == []

    def test_overwrite_true_skips_the_unreadable_id_refusal(self, monkeypatch, tmp_path):
        s1 = _Setup("S1", [_Op("Face1", operation_id=42)])
        cam = _install(monkeypatch, _CAM([s1], existing=["JOB1"]))
        prog = cam.ncPrograms.itemByName("JOB1")
        prog.operations = [_IdlessOp("Face1")]
        data = _payload(cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                                   program_name="JOB1", overwrite=True))
        assert data["posted"] is True
        assert prog.operations == [s1]


# -- the honesty gate: a real file must land -----------------------------------

class TestPostWritesFile:
    def test_posts_document_and_reports_written_file(self, monkeypatch, tmp_path):
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        data = _payload(cp.handler(post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="1001"))
        assert data["posted"] is True and data["scope"] == "document"
        assert data["file_count"] == 1
        rec = data["files"][0]
        assert rec["file_path"].endswith("1001.nc") and rec["size_bytes"] > 0
        assert os.path.isfile(rec["file_path"])

    def test_declared_output_is_minted(self, monkeypatch, tmp_path):
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        data = _payload(cp.handler(post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="1"))
        for out in cp.RETURNS:
            assert out.assert_present(data) == "", out.assert_present(data)

    def test_no_file_written_is_error_even_when_api_returns_true(self, monkeypatch, tmp_path):
        # THE honesty gate: postProcess returns True but writes nothing -> must be isError, never a false ok.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], writes=False, returns=True))
        res = cp.handler(post=str(_write_cps(tmp_path)),
                         output_folder=str(tmp_path), program_name="1")
        assert res["isError"] is True and "usable nc file" in res["message"].lower()

    def test_created_program_is_rolled_back_on_failed_post(self, monkeypatch, tmp_path):
        # Orphan cleanup: a just-created program that produced no file is deleted, not left behind.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], writes=False))
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path), program_name="X")
        assert res["isError"] is True
        assert cam.ncPrograms.count == 0                    # the orphan was removed
        assert "removed" in res["message"].lower()

    def test_a_rollback_whose_delete_returns_false_says_the_program_remains(self, monkeypatch,
                                                                            tmp_path):
        # deleteMe()'s own bool is the only evidence the orphan went away. Asserting the removal
        # over a swallowed call leaves a program in the document under a sentence saying it is gone.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], writes=False,
                                         program_class=_UndeletableProgram))
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                         program_name="X")
        assert res["isError"] is True
        assert cam.ncPrograms.count == 1                     # it really is still there
        assert "'X' was NOT removed" in res["message"] and "returned false" in res["message"]
        assert "cam_delete" in res["message"]

    def test_a_rollback_whose_delete_raises_says_the_program_remains_too(self, monkeypatch,
                                                                         tmp_path):
        # The other way a delete fails: safe() would swallow the raise into the same false claim.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], writes=False,
                                         program_class=_DeleteRaisesProgram))
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                         program_name="X")
        assert res["isError"] is True
        assert cam.ncPrograms.count == 1
        assert "'X' was NOT removed" in res["message"] and "deleteMe raised" in res["message"]

    def test_a_true_delete_the_collection_contradicts_says_the_program_is_still_there(
            self, monkeypatch, tmp_path):
        # The rung cam_delete stands on: deleteMe()'s true bool is not the effect, so the name is
        # re-read off ncPrograms and a program that still resolves is reported as still there.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], writes=False,
                                         program_class=_SurvivesDeleteProgram))
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                         program_name="X")
        assert res["isError"] is True
        assert cam.ncPrograms.itemByName("X") is not None
        assert "still resolves in ncPrograms" in res["message"]
        assert "was removed" not in res["message"]

    def test_reused_program_is_not_deleted_on_failed_post(self, monkeypatch, tmp_path):
        # A pre-existing program is the caller's - a failed re-post must NOT delete it.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], writes=False, existing=["JOB1"]))
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path), program_name="JOB1")
        assert res["isError"] is True
        assert cam.ncPrograms.count == 1                    # still there
        assert cam.ncPrograms.itemByName("JOB1").deleted is False

    def test_missing_output_folder_param_is_error(self, monkeypatch, tmp_path):
        # Without nc_program_output_folder the file can't be aimed at out_dir - fail loudly, name it,
        # and roll back the just-created program.
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])],
                                          missing=("nc_program_output_folder",)))
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path), program_name="1")
        assert res["isError"] is True and "nc_program_output_folder" in res["message"]
        assert cam.ncPrograms.count == 0

    def test_program_error_is_partial_even_with_file(self, monkeypatch, tmp_path):
        # A file landed and postProcess returned true, but the NC Program faulted (hasError) -> partial,
        # surface the program error rather than claim clean success.
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], program_error=True))
        data = _payload(cp.handler(post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="1"))
        assert data["partial"] is True and data["program_error"] == "toolpath fault"
        assert data["file_count"] == 1

    def test_partial_when_api_false_but_file_appeared(self, monkeypatch, tmp_path):
        # A file landed but the API flagged failure - report both facts, do not claim clean success.
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], writes=True, returns=False))
        data = _payload(cp.handler(post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="1"))
        assert data["posted"] is False and data["partial"] is True and data["file_count"] == 1

    def test_post_raising_is_error_and_rolls_back(self, monkeypatch, tmp_path):
        cam = _CAM([_Setup("S1", [_Op("Face1")])])
        def _boom(program):
            raise RuntimeError("post kaboom")
        cam._do_post = _boom
        _install(monkeypatch, cam)
        res = cp.handler(post=str(_write_cps(tmp_path)),
                         output_folder=str(tmp_path), program_name="1")
        assert res["isError"] is True and "kaboom" in res["message"]
        assert cam.ncPrograms.count == 0                    # orphan removed after the raise


# ── nc_program_unit: a ChoiceParameterValue rejects a bare int index; set the choice STRING, and ──
# ── surface an explicit note (never post silently-wrong units) when the set still fails. ────────────

class TestUnitParam:
    def test_missing_unit_param_is_no_note(self):
        val, note = cp._set_unit_param(FakeCAMParameters(), "mm")
        assert val is cp._MISSING and note is None

    def test_readback_mismatch_is_an_error_note_not_a_postable_unit(self):
        class _KeepsDocument:
            def getChoices(self):
                return (True, ["Document unit", "Inches", "Millimeters"], ["$doc", "$in", "$mm"])
            @property
            def value(self):
                return "$doc"
            @value.setter
            def value(self, _):
                pass
        params = FakeCAMParameters([types.SimpleNamespace(name="nc_program_unit", value=_KeepsDocument())])
        val, note = cp._set_unit_param(params, "mm")
        assert isinstance(val, str) and "read back" in val
        assert note and "refused" in note

    def test_no_choices_exposed_is_error_note_not_wrong_typed_set(self):
        # a value with no getChoices() must NOT be set blind (the platform rejects a bare int with a
        # std::string type error) - surface the error + note instead.
        params = FakeCAMParameters([FakeCAMParameter("nc_program_unit", value=0)])
        val, note = cp._set_unit_param(params, "mm")
        assert isinstance(val, str) and "error" in val
        assert note and "units" in note.lower()

    def test_choice_value_picked_by_unit_word_in_its_name(self):
        # getChoices() returns (ok, names, values); the value whose NAME carries the unit word is set -
        # never an index guess into the names.
        cv = types.SimpleNamespace(value="$doc")
        cv.getChoices = lambda: (True, ["Document unit", "Inches", "Millimeters"],
                                 ["$doc", "$in", "$mm"])
        params = FakeCAMParameters(
            [types.SimpleNamespace(name="nc_program_unit", value=cv)])
        val, note = cp._set_unit_param(params, "mm")
        assert note is None and cv.value == "$mm"

    def test_quoted_native_choice_values_are_written_unquoted_for_mm_and_inch(self):
        class _Quoted:
            def __init__(self):
                self._value = "'DocumentUnit'"
            def getChoices(self):
                return (True, ["Millimeters", "Inches", "Document units"],
                        ["'Millimeters'", "'Inches'", "'DocumentUnit'"])
            @property
            def value(self):
                return self._value
            @value.setter
            def value(self, value):
                if value.startswith("'"):
                    raise RuntimeError("quoted choice rejected")
                self._value = value

        for units, expected in (("mm", "Millimeters"), ("inch", "Inches")):
            cv = _Quoted()
            params = FakeCAMParameters([types.SimpleNamespace(name="nc_program_unit", value=cv)])
            val, note = cp._set_unit_param(params, units)
            assert note is None and val == expected and cv.value == expected

    def test_set_failure_returns_error_value_and_surfaced_note(self):
        # the live defect: setting the value raises 'ChoiceParameterValue__set_value'. The failure must
        # come back as an error value + a human note, never a swallowed success.
        class _Reject:
            def getChoices(self):
                return (True, ["Document unit", "Inches", "Millimeters"], ["$doc", "$in", "$mm"])
            @property
            def value(self):
                return "$doc"
            @value.setter
            def value(self, v):
                raise RuntimeError("error in ChoiceParameterValue__set_value")

        params = FakeCAMParameters(
            [types.SimpleNamespace(name="nc_program_unit", value=_Reject())])
        val, note = cp._set_unit_param(params, "mm")
        assert isinstance(val, str) and "error" in val
        assert note and "units" in note.lower()

    def test_handler_refuses_missing_unit_param_before_posting(self, monkeypatch, tmp_path):
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])],
                                         missing=("nc_program_unit",)))
        result = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                            program_name="1", units="mm")
        assert result["isError"] is True
        assert "nc_program_unit" in result["message"]
        assert cam.posted == [] and cam.ncPrograms.count == 0

    def test_handler_refuses_unit_setter_failure_before_posting(self, monkeypatch, tmp_path):
        cam = _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])]))
        monkeypatch.setattr(cp, "_set_unit_param",
                            lambda params, units_key: ("<error: boom>",
                                                       "Output units could not be set to 'mm'."))
        result = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                            program_name="1", units="mm")
        assert result["isError"] is True
        assert "Output units could not be set" in result["message"]
        assert not list(tmp_path.glob("*.nc"))
        assert not list(tmp_path.glob("*.failed"))
        assert cam.posted == [] and cam.ncPrograms.count == 0


# ── post-log surfacing: a failed post's real error lives in the log, not the output folder ──────────

class TestPostLog:
    def _make_log(self, root, program, lines):
        d = os.path.join(str(root), "sess-1", "5")
        os.makedirs(d)
        with open(os.path.join(d, program + ".log"), "w") as f:
            f.write(lines)

    def test_is_failure_marker(self):
        assert cp._is_failure_marker("C:/x/1001.nc.failed") is True
        assert cp._is_failure_marker("C:/x/1001.nc") is False

    def test_reads_error_and_warning_lines_skipping_information(self, monkeypatch, tmp_path):
        root = tmp_path / "Fusion360CAM"
        self._make_log(root, "1001",
                       "Information: start\n"
                       "Error: Program number 'NaN' is out of range. Please enter 1-99999.\n"
                       "Warning: check units\nInformation: end\n")
        monkeypatch.setattr(cp, "_CAM_LOG_ROOT", str(root))
        errs = cp._post_log_errors("1001", 0.0)
        assert any("out of range" in e for e in errs)
        assert any(e.lower().startswith("warning") for e in errs)
        assert all(not e.lower().startswith("information") for e in errs)

    def test_no_log_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cp, "_CAM_LOG_ROOT", str(tmp_path / "nothing"))
        assert cp._post_log_errors("1001", 0.0) == []

    def test_the_log_root_climbs_out_of_a_per_session_temp_dir(self, tmp_path):
        # inside Fusion the process temp dir is <TEMP>/Fusion360CAM/<session>; the logs sit under
        # the Fusion360CAM tree above it, not under a second Fusion360CAM inside it.
        temp = tmp_path / "Fusion360CAM" / "20524-65"
        assert cp._cam_log_root(str(temp)) == str(tmp_path / "Fusion360CAM")
        assert cp._cam_log_root(str(tmp_path)) == str(tmp_path / "Fusion360CAM")

    def test_a_failed_post_reads_its_log_from_the_climbed_root(self, monkeypatch, tmp_path):
        root = tmp_path / "Fusion360CAM"
        self._make_log(root, "1002", "Error: This postprocessor requires a machine configuration "
                                     "for 5-axis simultaneous toolpath.\n")
        monkeypatch.setattr(cp, "_CAM_LOG_ROOT", cp._cam_log_root(str(root / "20524-65")))
        assert any("requires a machine configuration" in e
                   for e in cp._post_log_errors("1002", 0.0))

    def test_a_program_number_refusal_names_the_non_numeric_name_that_was_sent(self, monkeypatch,
                                                                                tmp_path):
        # the post's own line is the only thing that says a number was wanted here; the clause
        # relays it and names the value that was not one.
        root = tmp_path / "Fusion360CAM"
        self._make_log(root, "FinishPass",
                       "Error: Program number 'NaN' is out of range. Please enter 1-99999.\n")
        monkeypatch.setattr(cp, "_CAM_LOG_ROOT", str(root))
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], writes="failed"))
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                         program_name="FinishPass")
        assert res["isError"] is True
        assert "PROGRAM NUMBER" in res["message"] and "'FinishPass'" in res["message"]
        assert "numeric program_name" in res["message"]

    def test_a_numeric_name_earns_no_number_clause(self, monkeypatch, tmp_path):
        # the boundary: the same log line against a name that IS a number - telling that caller to
        # pass a number names a remedy they already took.
        root = tmp_path / "Fusion360CAM"
        self._make_log(root, "1001", "Error: Program number '1001' is out of range.\n")
        monkeypatch.setattr(cp, "_CAM_LOG_ROOT", str(root))
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], writes="failed"))
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path),
                         program_name="1001")
        assert res["isError"] is True and "out of range" in res["message"]
        assert "numeric program_name" not in res["message"]

    def test_failed_stub_is_error_with_the_log_reason_not_listed_as_a_deliverable(self, monkeypatch, tmp_path):
        # A '.failed' stub is the ONLY thing the post wrote -> no deliverable NC file -> error that
        # surfaces the LOG's actionable reason, and never lists the stub as if it were G-code.
        root = tmp_path / "Fusion360CAM"
        self._make_log(root, "1001", "Error: Program number 'NaN' is out of range.\n")
        monkeypatch.setattr(cp, "_CAM_LOG_ROOT", str(root))
        _install(monkeypatch, _CAM([_Setup("S1", [_Op("Face1")])], writes="failed"))
        res = cp.handler(post=str(_write_cps(tmp_path)), output_folder=str(tmp_path), program_name="1001")
        assert res["isError"] is True
        assert "out of range" in res["message"]            # the real reason, from the log
        assert ".failed" not in str(res.get("data", ""))   # the stub is not paraded as a deliverable


# -- an explicit SETUPS list: the multi-setup program 'scope' cannot express -------------------

class TestSetupsScope:
    """'scope' names ONE node; 'setups' names several. What is pinned is the resolve (exact,
    case-insensitive, refusing what it cannot identify) and that the whole list reaches the
    program's operations assignment."""

    def _two(self):
        return _Setup("Setup1", [_Op("Face1")]), _Setup("Setup2", [_Op("Drill1")])

    def test_a_setups_list_puts_several_setups_in_one_program(self, monkeypatch, tmp_path):
        s1, s2 = self._two()
        cam = _install(monkeypatch, _CAM([s1, s2]))
        data = _payload(cp.handler(setups=["Setup1", "Setup2"], post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        # the assignment is a plain LIST of exactly the named setups, in the order asked for
        assert cam.ncPrograms.item(0).operations == [s1, s2]
        assert data["scope"] == "setups" and data["scope_setups"] == ["Setup1", "Setup2"]
        assert data["program_operation_count"] == 2 and data["membership_verified"] is True

    def test_one_setup_of_two_is_not_the_whole_document(self, monkeypatch, tmp_path):
        # The discriminating case: a single-entry 'setups' must post ONE setup, not fall through to
        # the document scope that a None target means.
        s1, s2 = self._two()
        cam = _install(monkeypatch, _CAM([s1, s2]))
        _payload(cp.handler(setups=["Setup2"], post=str(_write_cps(tmp_path)),
                            output_folder=str(tmp_path), program_name="9"))
        assert cam.ncPrograms.item(0).operations == [s2]

    def test_a_comma_string_resolves_the_same_way(self, monkeypatch, tmp_path):
        s1, s2 = self._two()
        cam = _install(monkeypatch, _CAM([s1, s2]))
        _payload(cp.handler(setups="Setup1, Setup2,", post=str(_write_cps(tmp_path)),
                            output_folder=str(tmp_path), program_name="9"))
        assert cam.ncPrograms.item(0).operations == [s1, s2]   # the trailing comma is not a setup

    def test_the_match_is_case_insensitive(self, monkeypatch, tmp_path):
        s1, s2 = self._two()
        cam = _install(monkeypatch, _CAM([s1, s2]))
        _payload(cp.handler(setups=["setup2"], post=str(_write_cps(tmp_path)),
                            output_folder=str(tmp_path), program_name="9"))
        assert cam.ncPrograms.item(0).operations == [s2]

    def test_scope_and_setups_together_are_refused(self, monkeypatch, tmp_path):
        s1, s2 = self._two()
        cam = _install(monkeypatch, _CAM([s1, s2]))
        res = cp.handler(scope="Setup1", setups=["Setup2"], post=str(_write_cps(tmp_path)),
                         output_folder=str(tmp_path), program_name="9")
        assert res["isError"] is True
        assert "'scope' or 'setups', not both" in res["message"]
        assert cam.ncPrograms.count == 0 and cam.posted == []

    def test_an_unknown_setup_name_lists_the_available_ones(self, monkeypatch, tmp_path):
        s1, s2 = self._two()
        cam = _install(monkeypatch, _CAM([s1, s2]))
        res = cp.handler(setups=["Setup1", "Ghost"], post=str(_write_cps(tmp_path)),
                         output_folder=str(tmp_path), program_name="9")
        assert res["isError"] is True and "Ghost" in res["message"]
        assert "Setup1" in res["message"] and "Setup2" in res["message"]
        assert cam.ncPrograms.count == 0            # a bad name in the list posts nothing at all

    def test_a_setup_listed_twice_is_refused(self, monkeypatch, tmp_path):
        s1, s2 = self._two()
        cam = _install(monkeypatch, _CAM([s1, s2]))
        res = cp.handler(setups=["Setup1", "setup1"], post=str(_write_cps(tmp_path)),
                         output_folder=str(tmp_path), program_name="9")
        assert res["isError"] is True and "listed twice" in res["message"]
        assert cam.ncPrograms.count == 0

    def test_a_setups_value_that_is_neither_list_nor_string_is_refused(self, monkeypatch, tmp_path):
        s1, s2 = self._two()
        _install(monkeypatch, _CAM([s1, s2]))
        res = cp.handler(setups=42, post=str(_write_cps(tmp_path)),
                         output_folder=str(tmp_path), program_name="9")
        assert res["isError"] is True and "setups" in res["message"]

    def test_setups_against_an_existing_program_is_not_as_is(self, monkeypatch, tmp_path):
        # A 'setups' list IS a scope, so it must take the configure path (which then requires
        # output_folder) rather than silently posting the stored configuration as-is.
        s1, s2 = self._two()
        cam = _install(monkeypatch, _CAM([s1, s2], existing=["JOB1"]))
        prog = cam.ncPrograms.itemByName("JOB1")
        prog.parameters.itemByName("nc_program_output_folder").expression = str(tmp_path).replace("\\", "/")
        prog.postConfiguration = object()
        res = cp.handler(setups=["Setup1"], program_name="JOB1")
        assert res["isError"] is True and "output_folder" in res["message"]

    def test_the_overwrite_refusal_names_setups_among_what_to_omit(self, monkeypatch, tmp_path):
        # The refusal's way out is as-is mode, which 'setups' also blocks - a remedy naming only
        # scope/post/output_folder would land the caller back on this same path.
        s1, s2 = self._two()
        cam = _install(monkeypatch, _CAM([s1, s2], existing=["JOB1"]))
        cam.ncPrograms.itemByName("JOB1").operations = [s1]     # stored: Setup1 only
        res = cp.handler(setups=["Setup2"], post=str(_write_cps(tmp_path)),
                         output_folder=str(tmp_path), program_name="JOB1")
        assert res["isError"] is True and "machinist-curated" in res["message"]
        assert "'scope', 'setups', 'post', and 'output_folder'" in res["message"]

    def test_an_empty_setups_list_reads_as_not_given(self, monkeypatch, tmp_path):
        s1, s2 = self._two()
        cam = _install(monkeypatch, _CAM([s1, s2]))
        data = _payload(cp.handler(setups=[], post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        assert data["scope"] == "document"
        assert cam.ncPrograms.item(0).operations == [s1, s2]


# -- the membership read-back: what the program's OWN operations answer after the assignment ------

class TestProgramOperationCount:
    """MEASURED: NCProgram.operations holds the SETUPS/folders assigned, filteredOperations every
    Operation in that scope - posted or not. So 'program_operation_count' is the filtered read,
    'posted_operations' the rows of it reading hasToolpath True, and 'program_item_count' the
    stored containers beside them."""

    def test_the_operations_figure_is_the_filtered_read_not_the_stored_items(self, monkeypatch,
                                                                             tmp_path):
        s1 = _Setup("Setup1", [_Op("Face1"), _Op("Drill1")])
        cam = _install(monkeypatch, _CAM([s1]))
        data = _payload(cp.handler(setups=["Setup1"], post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        prog = cam.ncPrograms.item(0)
        assert prog.operations == [s1]                     # one stored item holding two operations
        assert data["program_item_count"] == len(prog.operations) == 1
        assert data["program_operation_count"] == 2        # what the program holds
        assert data["posted_operations"] == 2              # both carry a toolpath
        assert data["membership_verified"] is True         # the flatten still drives the id compare

    def test_the_held_operations_are_counted_apart_from_the_ones_carrying_a_toolpath(
            self, monkeypatch, tmp_path):
        # the shape the file measured: four operations in the program's scope, one operation block
        # in the posted NC file - the one op reading hasToolpath True.
        s1 = _Setup("Setup1", [_Op("FaceLeg"), _Op("Chamfer1", has_toolpath=False),
                               _Op("Drill1", has_toolpath=False),
                               _Op("Drill2", has_toolpath=False)])
        _install(monkeypatch, _CAM([s1]))
        data = _payload(cp.handler(setups=["Setup1"], post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        assert data["program_operation_count"] == 4
        assert data["posted_operations"] == 1
        assert "toolpath_unread" not in data               # every flag answered

    def test_a_row_whose_toolpath_flag_does_not_read_is_not_counted_as_posted(self, monkeypatch,
                                                                              tmp_path):
        # counting an unreadable flag either way invents a number: it is disclosed instead.
        s1 = _Setup("Setup1", [_Op("Face1"), _ToolpathlessOp("Mystery1")])
        _install(monkeypatch, _CAM([s1]))
        data = _payload(cp.handler(setups=["Setup1"], post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        assert data["program_operation_count"] == 2
        assert data["posted_operations"] == 1 and data["toolpath_unread"] == 1

    def test_the_note_words_the_operations_figure_as_what_the_program_holds(self, monkeypatch,
                                                                            tmp_path):
        # the reading this note exists to correct: 'the operations posted' off a count that also
        # holds the ops with no toolpath.
        s1 = _Setup("Setup1", [_Op("Face1"), _Op("Drill1", has_toolpath=False)])
        _install(monkeypatch, _CAM([s1]))
        data = _payload(cp.handler(setups=["Setup1"], post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        assert "program_operation_count: unsuppressed operations in scope" in data["note"]
        assert "posted_operations: hasToolpath True" in data["note"]
        assert "Suppressed operations are excluded from both counts and NC output" in data["note"]

    def test_the_note_says_a_suppressed_operation_is_neither_held_nor_posted(self, monkeypatch,
                                                                              tmp_path):
        # MEASURED: a program over a list holding a suppressed op OMITS it - filteredOperations
        # excludes it, postProcess answers True, and the file carries none of its moves. Without
        # this sentence a caller reads the two counts as a refusal it never got.
        s1 = _Setup("Setup1", [_Op("Face1")])
        _install(monkeypatch, _CAM([s1]))
        data = _payload(cp.handler(setups=["Setup1"], post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        assert "Suppressed operations are excluded from both counts and NC output." in data["note"]

    def test_a_filtered_read_that_raises_publishes_no_operations_figure(self, monkeypatch,
                                                                        tmp_path):
        s1 = _Setup("Setup1", [_Op("Face1")])
        _install(monkeypatch, _CAM([s1], program_class=_UnfilterableProgram))
        data = _payload(cp.handler(setups=["Setup1"], post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        assert "program_operation_count" not in data       # absent, never a fabricated count
        assert "posted_operations" not in data
        assert data["program_item_count"] == 1


class TestMembershipReadBack:
    def test_an_empty_read_back_is_an_error_not_a_false_ok(self, monkeypatch, tmp_path):
        s1, s2 = _Setup("Setup1", [_Op("Face1")]), _Setup("Setup2", [_Op("Drill1")])
        cam = _install(monkeypatch, _CAM([s1, s2], program_class=_AmnesicProgram))
        res = cp.handler(setups=["Setup1", "Setup2"], post=str(_write_cps(tmp_path)),
                         output_folder=str(tmp_path), program_name="9")
        assert res["isError"] is True
        assert "re-read ZERO operations" in res["message"]
        assert "resolves to 2" in res["message"]
        assert cam.posted == [] and cam.ncPrograms.count == 0   # nothing posted, no orphan left
        assert not [p for p in os.listdir(tmp_path) if p.endswith(".nc")]

    def test_a_membership_disagreement_is_disclosed_as_counts_not_judged(self, monkeypatch, tmp_path):
        # The program kept one of the two setups. Whether an NCProgram stores a scope verbatim is
        # not measured, so the disagreement is PUBLISHED - it is not turned into a refusal.
        s1, s2 = _Setup("Setup1", [_Op("Face1")]), _Setup("Setup2", [_Op("Drill1")])
        _install(monkeypatch, _CAM([s1, s2], program_class=_PartialProgram))
        data = _payload(cp.handler(setups=["Setup1", "Setup2"], post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        assert data["membership_verified"] is False
        assert data["program_operation_count"] == 1
        assert "the program holds 1, not the 2 the scope resolves to" in data["membership_note"]
        assert "cam_get(include=['nc_programs'])" in data["membership_note"]
        # the two sides of the disagreement ride as counts, since the clause no longer spells them
        assert data["membership_missing"] == 1 and data["membership_extra"] == 0
        assert "Membership:" in data["note"]                 # stated beside the file, not buried
        assert data["file_count"] == 1                       # the file still landed

    def test_an_unreadable_membership_is_neither_verified_nor_denied(self, monkeypatch, tmp_path):
        s1 = _Setup("Setup1", [_Op("Face1")])
        _install(monkeypatch, _CAM([s1], program_class=_UnreadableMembershipProgram))
        data = _payload(cp.handler(setups=["Setup1"], post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        # null, not false: the read did not answer, which is not the same as a mismatch
        assert data["membership_verified"] is None
        # both counts come off collections this program will not read, so neither key is published
        assert "program_operation_count" not in data and "program_item_count" not in data
        assert "did not read back" in data["membership_note"]

    def test_an_unreadable_operation_id_leaves_the_comparison_unmade(self, monkeypatch, tmp_path):
        s1 = _Setup("Setup1", [_IdlessOp("Face1")])
        _install(monkeypatch, _CAM([s1]))
        data = _payload(cp.handler(setups=["Setup1"], post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        assert data["membership_verified"] is None
        assert "no readable operationId" in data["membership_note"]

    def test_a_matching_membership_publishes_no_note(self, monkeypatch, tmp_path):
        s1 = _Setup("Setup1", [_Op("Face1")])
        _install(monkeypatch, _CAM([s1]))
        data = _payload(cp.handler(scope="Setup1", post=str(_write_cps(tmp_path)),
                                   output_folder=str(tmp_path), program_name="9"))
        assert data["membership_verified"] is True
        assert "membership_note" not in data and "Membership read-back:" not in data["note"]

