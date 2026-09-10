"""Unit tests for ``_cam_templates.py`` - the template library walk and by-name search.

Targets ``_find_template_by_name``: the location validation (unknown location -> hint),
case-insensitive matching, and recursive descent into subfolders; plus ``_walk_library``'s
asset-URL pairing, depth limit and node cap. A fake library models the ``childTemplates`` /
``childFolderURLs`` / ``urlByLocation`` surface the walker uses - no live CAM needed.
"""

from types import SimpleNamespace

from conftest import load_tool

ct = load_tool("_cam_templates")

class FakeLib:
    """Minimal templateLibrary: a folder tree of named templates.

    ``tree`` maps a folder URL (any hashable) to (templates, subfolder_urls).
    ``urlByLocation`` returns the configured root url regardless of enum value.
    childAssetURLs answers ONE asset per DISTINCT template name in the folder - the shape measured
    across the shipped libraries (no folder holds two templates of one name), and the asset side
    the by-name resolve reads a repeated arrival's identity from."""
    def __init__(self, root_url, tree):
        self._root = root_url
        self._tree = tree

    def urlByLocation(self, loc):
        return self._root

    def childTemplates(self, folder_url):
        templates, _ = self._tree.get(folder_url, ([], []))
        return [SimpleNamespace(name=n) for n in templates]

    def childAssetURLs(self, folder_url):
        templates, _ = self._tree.get(folder_url, ([], []))
        return [_Url(f"lib://{folder_url}/{n}.f3dhsm-template")
                for n in dict.fromkeys(templates)]

    def childFolderURLs(self, folder_url):
        _, subs = self._tree.get(folder_url, ([], []))
        return subs


class TestFindTemplateByName:
    def test_unknown_location_is_rejected(self):
        lib = FakeLib("root", {"root": (["A"], [])})
        template, hint = ct._find_template_by_name(lib, "atlantis", "A")
        assert template is None
        assert "atlantis" in hint

    def test_finds_template_in_root(self):
        lib = FakeLib("root", {"root": (["2D Adaptive", "Face"], [])})
        template, hint = ct._find_template_by_name(lib, "cloud", "Face")
        assert template is not None
        assert template.name == "Face"
        assert hint is None

    def test_match_is_case_insensitive(self):
        lib = FakeLib("root", {"root": (["2D Adaptive"], [])})
        template, hint = ct._find_template_by_name(lib, "cloud", "2d adaptive")
        assert template is not None
        assert template.name == "2D Adaptive"

    def test_descends_into_subfolders(self):
        lib = FakeLib("root", {
            "root": ([], ["sub"]),
            "sub": (["Deep Template"], []),
        })
        template, hint = ct._find_template_by_name(lib, "cloud", "Deep Template")
        assert template is not None
        assert template.name == "Deep Template"

    def test_not_found_returns_none(self):
        lib = FakeLib("root", {"root": (["A", "B"], [])})
        template, hint = ct._find_template_by_name(lib, "cloud", "Missing")
        assert template is None

    def test_a_name_arriving_twice_in_one_folder_still_resolves(self):
        # the folder's own listing hands one template back twice; refusing that as an ambiguity
        # leaves the template unreachable by name and by url alike.
        lib = FakeLib("root", {"root": (["Drill", "Drill"], [])})
        template, hint = ct._find_template_by_name(lib, "cloud", "Drill")
        assert template is not None and template.name == "Drill"
        assert hint is None

    def test_a_name_two_assets_in_one_folder_answer_is_refused(self):
        # two arrivals the asset side cannot tell apart: nothing here identifies one, so the
        # resolve refuses instead of applying whichever the walk reached first.
        lib = FakeLib("root", {"root": (["Drill", "Drill"], [])})
        lib.childAssetURLs = lambda folder_url: [_Url("lib://root/Drill.f3dhsm-template"),
                                                 _Url("lib://root/DRILL.f3dhsm-template")]
        template, hint = ct._find_template_by_name(lib, "cloud", "Drill")
        assert template is None and "ambiguous" in hint

    def test_one_name_in_two_folders_is_still_refused(self):
        # the boundary the de-dup must not cross: two FOLDERS carrying the name are two assets, and
        # picking either would apply a template the caller did not name.
        lib = FakeLib("root", {"root": (["Drill"], ["sub"]), "sub": (["Drill"], [])})
        template, hint = ct._find_template_by_name(lib, "cloud", "Drill")
        assert template is None and "ambiguous" in hint


# ── _walk_library: asset-URL matching + depth limit + node cap ──────────────────────────────────────
# The string parse in _asset_url_for is a classic silent-wrong-string risk: it must pair a template
# NAME to its asset URL by the stem of "<folder>/<name>.f3dhsm-template". The depth limit must flag
# 'folders_truncated', and the global node cap must flag 'truncated'.


class _Url:
    """A library URL: toString() plus the leafName the shared asset_leaf_keys reading takes - the
    last path segment, as adsk.core.URL answers it."""
    def __init__(self, s): self._s = s
    def toString(self): return self._s
    @property
    def leafName(self): return self._s.rstrip("/").rsplit("/", 1)[-1]


class _WalkLib:
    """A library tree keyed by folder url-string: name -> (template_names, [subfolder url-strings],
    [asset_url_strings])."""
    def __init__(self, tree):
        self._tree = tree
    def displayName(self, url):
        return f"folder<{url.toString()}>"
    def childTemplates(self, url):
        names, _, _ = self._tree.get(url.toString(), ([], [], []))
        return [SimpleNamespace(name=n, description="", isValidTemplate=True,
                                isHoleTemplate=False) for n in names]
    def childFolderURLs(self, url):
        _, subs, _ = self._tree.get(url.toString(), ([], [], []))
        return [_Url(s) for s in subs]
    def childAssetURLs(self, url):
        _, _, assets = self._tree.get(url.toString(), ([], [], []))
        return [_Url(a) for a in assets]


class TestWalkLibrary:
    def test_asset_url_paired_to_template_by_stem(self):
        tree = {"root": (["Face", "2D Adaptive"], [],
                         ["lib://root/Face.f3dhsm-template",
                          "lib://root/2D Adaptive.f3dhsm-template"])}
        node = ct._walk_library(_WalkLib(tree), _Url("root"), 0, 4, {"n": 0, "truncated": False})
        urls = {t["name"]: t["url"] for t in node["templates"]}
        assert urls["Face"] == "lib://root/Face.f3dhsm-template"
        assert urls["2D Adaptive"] == "lib://root/2D Adaptive.f3dhsm-template"

    def test_template_without_matching_asset_gets_none_url(self):
        # two assets beside one template: no name matches and the lists are different lengths, so
        # there is no position that addresses this template either.
        tree = {"root": (["Lonely"], [], ["lib://root/Other.f3dhsm-template",
                                          "lib://root/Third.f3dhsm-template"])}
        node = ct._walk_library(_WalkLib(tree), _Url("root"), 0, 4, {"n": 0, "truncated": False})
        assert node["templates"][0]["url"] is None
        assert "url_basis" not in node["templates"][0]

    def test_a_folder_whose_leaf_names_match_no_template_pairs_by_position(self):
        # the shipped hole templates' shape: real asset urls the walk publishes as null because no
        # leafName spells the template's name, which leaves template_url unreachable for them.
        tree = {"root": (["Drill", "Bore"], [],
                         ["lib://root/hole_a.f3dhsm-template", "lib://root/hole_b.f3dhsm-template"])}
        node = ct._walk_library(_WalkLib(tree), _Url("root"), 0, 4, {"n": 0, "truncated": False})
        rows = {t["name"]: t for t in node["templates"]}
        assert rows["Drill"]["url"] == "lib://root/hole_a.f3dhsm-template"
        assert rows["Bore"]["url"] == "lib://root/hole_b.f3dhsm-template"
        assert rows["Drill"]["url_basis"] == "folder_position"

    def test_one_name_matching_holds_the_rest_back_from_position_pairing(self):
        # a mixed folder: the name match proves the leafName spelling is in use here, so the odd
        # template out is NOT addressed by an index that would name another template's asset.
        tree = {"root": (["Face", "Bore"], [],
                         ["lib://root/Face.f3dhsm-template", "lib://root/hole_b.f3dhsm-template"])}
        node = ct._walk_library(_WalkLib(tree), _Url("root"), 0, 4, {"n": 0, "truncated": False})
        rows = {t["name"]: t for t in node["templates"]}
        assert rows["Face"]["url"] == "lib://root/Face.f3dhsm-template"
        assert rows["Bore"]["url"] is None

    def test_a_name_listed_twice_in_one_folder_is_one_row(self):
        # the flattened listing hands the same template back twice; two rows would double the
        # library's own census and refuse the name as ambiguous everywhere it is resolved.
        tree = {"root": (["Drill", "Drill"], [], ["lib://root/Drill.f3dhsm-template"])}
        counter = {"n": 0, "truncated": False}
        node = ct._walk_library(_WalkLib(tree), _Url("root"), 0, 4, counter)
        assert [t["name"] for t in node["templates"]] == ["Drill"]
        assert node["templates"][0]["url"] == "lib://root/Drill.f3dhsm-template"
        assert node["templates_collided"] == 1     # dropped, and counted rather than silent
        assert counter["n"] == 1

    def test_two_assets_answering_one_name_stay_two_rows(self):
        # the boundary the identity keying exists for: two DISTINCT assets whose leafNames both
        # read 'drill' are two templates, and collapsing them would drop one from the library's
        # own census. Neither name identifies an asset, so both rows fall to the index pairing.
        tree = {"root": (["Drill", "Drill"], [],
                         ["lib://root/Drill.f3dhsm-template", "lib://root/DRILL.f3dhsm-template"])}
        node = ct._walk_library(_WalkLib(tree), _Url("root"), 0, 4, {"n": 0, "truncated": False})
        assert [t["name"] for t in node["templates"]] == ["Drill", "Drill"]
        assert [t["url"] for t in node["templates"]] == ["lib://root/Drill.f3dhsm-template",
                                                         "lib://root/DRILL.f3dhsm-template"]
        assert all(t["url_basis"] == "folder_position" for t in node["templates"])
        assert "templates_collided" not in node

    def test_descends_and_reports_nested_templates(self):
        tree = {"root": ([], ["sub"], []), "sub": (["Deep"], [], [])}
        node = ct._walk_library(_WalkLib(tree), _Url("root"), 0, 4, {"n": 0, "truncated": False})
        assert node["folders"][0]["templates"][0]["name"] == "Deep"

    def test_depth_limit_flags_folders_truncated(self):
        tree = {"root": ([], ["sub"], []), "sub": (["Deep"], [], [])}
        # max_depth=1 -> we are at depth 0, depth+1 (1) is NOT < 1, so we don't descend.
        node = ct._walk_library(_WalkLib(tree), _Url("root"), 0, 1, {"n": 0, "truncated": False})
        assert node["folders"] == []
        assert node.get("folders_truncated") is True
