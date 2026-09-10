"""Unit tests for ``_materials.py`` - the ONE material/appearance catalog walk behind design_get's
'materials' and 'appearances' slices: the count-only library census, the EXACT-name library
resolver, the name-filtered capped page that still reports the true match count, and the two-level
``browse`` those compose into.
"""

from types import SimpleNamespace

import pytest

from conftest import load_tool, error_message, _NamedCollection

mat = load_tool("_materials")


class _CountingCollection(_NamedCollection):
    """A collection that records how often its contents were touched, so a test can prove a census
    read only the count."""

    def __init__(self, items=()):
        super().__init__(items)
        self.item_calls = 0

    def item(self, i):
        self.item_calls += 1
        return super().item(i)


def _entry(name, eid, is_used=False):
    return SimpleNamespace(name=name, id=eid, isUsed=is_used)


def _library(name, lib_id, materials=(), appearances=(), is_native=True, counting=False):
    coll = _CountingCollection if counting else _NamedCollection
    return SimpleNamespace(name=name, id=lib_id, isNative=is_native,
                           materials=coll(materials), appearances=coll(appearances))


def _design(materials=(), appearances=()):
    return SimpleNamespace(materials=_NamedCollection(materials),
                           appearances=_NamedCollection(appearances))


@pytest.fixture
def libs(monkeypatch):
    """Install a library set on the module's app seam; the test mutates the list it returns."""
    installed = []

    def _install(*libraries):
        installed[:] = libraries
        monkeypatch.setattr(mat, "app",
                            SimpleNamespace(materialLibraries=_NamedCollection(installed)))
        return installed

    _install()
    return _install


# ── the census: counts only, both kinds, never a content walk ──────────────────────────────────────

class TestCensus:
    def test_reports_both_counts_per_library(self, libs):
        libs(_library("Fusion Material Library", "lib-1",
                      materials=[_entry("Steel", "m1")], appearances=[_entry("Paint", "a1"),
                                                                      _entry("Glass", "a2")]),
             _library("Private Visual Library", "lib-2"))
        rows, readable = mat.catalog_census()
        assert readable is True
        assert [r["name"] for r in rows] == ["Fusion Material Library", "Private Visual Library"]
        assert rows[0]["material_count"] == 1 and rows[0]["appearance_count"] == 2
        assert rows[0]["id"] == "lib-1" and rows[0]["is_native"] is True
        assert rows[1]["material_count"] == 0 and rows[1]["appearance_count"] == 0

    def test_never_walks_a_library_contents(self, libs):
        # The census is the level-0 projection and must stay O(1) per library: reading .count only,
        # never item(i). Walking here is what would flood a blind design_get(include=['materials']).
        big = _library("Fusion Material Library", "lib-1",
                       materials=[_entry(f"M{i}", f"m{i}") for i in range(324)],
                       appearances=[_entry(f"A{i}", f"a{i}") for i in range(326)], counting=True)
        libs(big)
        rows, _readable = mat.catalog_census()
        assert rows[0]["material_count"] == 324 and rows[0]["appearance_count"] == 326
        assert big.materials.item_calls == 0 and big.appearances.item_calls == 0

    def test_no_libraries_loaded(self, libs):
        libs()
        assert mat.catalog_census() == ([], True)

    def test_a_count_that_will_not_read_is_null_not_zero(self, libs):
        # this census IS the caller's evidence of what a library holds, so a 0 there says "this
        # library is empty" - the one claim an unread count cannot support.
        blind = _library("Half-read", "lib-1", materials=[_entry("Steel", "m1")])
        del blind.appearances
        libs(blind)
        rows, readable = mat.catalog_census()
        assert readable is True
        assert rows[0]["material_count"] == 1 and rows[0]["appearance_count"] is None

    def test_a_genuinely_empty_library_still_reads_zero(self, libs):
        # the boundary the null must not swallow: 0 is an answer when the collection DID read.
        libs(_library("Empty", "lib-1"))
        rows, _readable = mat.catalog_census()
        assert rows[0]["material_count"] == 0 and rows[0]["appearance_count"] == 0

    def test_an_unreadable_library_collection_is_not_no_libraries(self, libs, monkeypatch):
        libs()
        monkeypatch.setattr(mat, "app", SimpleNamespace())   # no materialLibraries at all
        rows, readable = mat.catalog_census()
        assert rows == [] and readable is False


# ── find_library: EXACT name, miss lists what is loaded, duplicate refused ──────────────────────────

class TestFindLibrary:
    def test_exact_name_case_insensitive(self, libs):
        libs(_library("Fusion Material Library", "lib-1"), _library("Other", "lib-2"))
        lib, err = mat.find_library("fusion material library")
        assert err is None and lib.id == "lib-1"

    def test_census_id_resolves_duplicate_library_names(self, libs):
        libs(_library("Shared", "lib-1"), _library("Shared", "lib-2"))
        rows, _ = mat.catalog_census()
        lib, err = mat.find_library(rows[1]["id"])
        assert err is None and lib.id == "lib-2"

    def test_partial_name_is_not_a_match(self, libs):
        # a substring must NOT resolve - 'Fusion' would otherwise silently pick one of several
        # 'Fusion ...' libraries.
        libs(_library("Fusion Material Library", "lib-1"),
             _library("Fusion Appearance Library", "lib-2"))
        lib, err = mat.find_library("Fusion")
        assert lib is None and "No loaded material library named 'Fusion'" in err

    def test_miss_lists_the_loaded_names(self, libs):
        libs(_library("Fusion Material Library", "lib-1"), _library("Other", "lib-2"))
        _lib, err = mat.find_library("Nope")
        assert "'Fusion Material Library'" in err and "'Other'" in err

    def test_duplicate_name_is_refused_not_first_matched(self, libs):
        libs(_library("Shared", "lib-1"), _library("Shared", "lib-2"))
        lib, err = mat.find_library("Shared")
        assert lib is None and "refusing to pick one" in err

    def test_no_libraries_loaded_says_none(self, libs):
        libs()
        _lib, err = mat.find_library("Anything")
        assert "none" in err

    def test_an_unreadable_catalog_is_not_reported_as_no_libraries_loaded(self, libs, monkeypatch):
        # the same hole catalog_census publishes as readable=false. "Loaded libraries: none" asserts
        # the catalog is EMPTY - which sends a caller off to install a library already installed.
        libs()
        monkeypatch.setattr(mat, "app", SimpleNamespace())   # no materialLibraries at all
        lib, err = mat.find_library("Fusion Material Library")
        assert lib is None
        assert "UNKNOWN" in err and "could not be read" in err
        assert "Loaded libraries: none" not in err


# ── entries: filter, cap, true total, and id on every row ──────────────────────────────────────────

class TestEntries:
    def test_every_row_carries_id_beside_name(self):
        rows, matched, truncated = mat.entries(
            _NamedCollection([_entry("Steel", "PrismMaterial-405")]), "Lib")
        assert rows == [{"name": "Steel", "id": "PrismMaterial-405", "scope": "Lib"}]
        assert matched == 1 and truncated is False

    def test_repeated_names_stay_distinct_rows(self):
        # names repeat within one library (326 appearances, 92 distinct names), so two same-named
        # entries must both appear, told apart by id - collapsing them would hide a real choice.
        rows, matched, _t = mat.entries(
            _NamedCollection([_entry("Paint", "a1"), _entry("Paint", "a2")]), "Lib")
        assert [r["id"] for r in rows] == ["a1", "a2"] and matched == 2

    def test_name_filter_is_a_case_insensitive_substring(self):
        coll = _NamedCollection([_entry("Steel", "m1"), _entry("Stainless Steel", "m2"),
                                 _entry("Aluminum", "m3")])
        rows, matched, _t = mat.entries(coll, "Lib", name_filter="STEE")
        assert [r["name"] for r in rows] == ["Steel", "Stainless Steel"] and matched == 2

    def test_cap_truncates_rows_but_matched_stays_true(self):
        # the honesty rule: the page is capped, the TOTAL is not - an agent must be able to tell
        # "these are all of them" from "these are the first few".
        coll = _NamedCollection([_entry(f"M{i}", f"m{i}") for i in range(10)])
        rows, matched, truncated = mat.entries(coll, "Lib", cap=3)
        assert len(rows) == 3 and matched == 10 and truncated is True

    def test_filtered_total_counts_every_match_not_the_page(self):
        coll = _NamedCollection([_entry(f"Steel {i}", f"m{i}") for i in range(8)]
                                + [_entry("Brass", "b1")])
        rows, matched, truncated = mat.entries(coll, "Lib", name_filter="steel", cap=2)
        assert len(rows) == 2 and matched == 8 and truncated is True

    def test_usage_flag_only_when_asked(self):
        # document rows carry is_used; library rows omit it (a per-entry read this page skips).
        doc, _m, _t = mat.entries(_NamedCollection([_entry("Steel", "m1", is_used=True)]),
                                  "document", with_usage=True)
        lib, _m2, _t2 = mat.entries(_NamedCollection([_entry("Steel", "m1", is_used=True)]), "Lib")
        assert doc[0]["is_used"] is True and "is_used" not in lib[0]

    def test_empty_collection(self):
        assert mat.entries(_NamedCollection([]), "Lib") == ([], 0, False)

    def test_absent_collection_is_not_a_crash(self):
        assert mat.entries(None, "Lib") == ([], 0, False)


class TestClampCap:
    @pytest.mark.parametrize("given", [0, -5, None, "", "junk"])
    def test_unusable_request_falls_back_to_default(self, given):
        assert mat.clamp_cap(given) == mat.DEFAULT_CAP

    def test_request_is_clamped_to_max(self):
        assert mat.clamp_cap(100000) == mat.MAX_CAP

    def test_reasonable_request_honored(self):
        assert mat.clamp_cap(5) == 5


# ── browse: the two levels ─────────────────────────────────────────────────────────────────────────

class TestBrowseLevelZero:
    def test_document_set_plus_library_census_without_contents(self, libs):
        libs(_library("Fusion Material Library", "lib-1",
                      materials=[_entry("Steel", "m1"), _entry("Brass", "m2")]))
        design = _design(materials=[_entry("Steel (2)", "d1", is_used=True)])
        out, err = mat.browse(design, "materials")
        assert err is None
        assert out["document"]["count"] == 1
        assert out["document"]["entries"][0] == {"name": "Steel (2)", "id": "d1",
                                                 "scope": "document", "is_used": True}
        assert out["libraries"] == [{"name": "Fusion Material Library", "id": "lib-1",
                                     "is_native": True, "material_count": 2,
                                     "appearance_count": 0}]
        assert "entries" not in out              # no library CONTENTS at level 0
        # the completeness markers: everything read, so nothing is being reported as unknown
        assert out["document"]["readable"] is True
        assert out["libraries_readable"] is True and out["unread_libraries"] == 0

    def test_note_advertises_the_scope_flags(self, libs):
        libs(_library("Fusion Material Library", "lib-1"))
        out, _err = mat.browse(_design(), "materials")
        note = out["note"]
        assert "library=" in note and "name_filter=" in note and "max_results=" in note
        assert "model_set_material" in note      # what the names feed

    def test_appearances_note_points_at_its_own_consumer(self, libs):
        libs()
        out, _err = mat.browse(_design(), "appearances")
        assert "set_appearance" in out["note"]

    def test_appearances_reads_the_appearance_collection(self, libs):
        # the two slices differ only in which collection they project - reading .materials for
        # both would return the wrong catalog under the right label.
        libs()
        design = _design(materials=[_entry("Steel", "m1")], appearances=[_entry("Paint", "a1")])
        out, _err = mat.browse(design, "appearances")
        assert [e["name"] for e in out["document"]["entries"]] == ["Paint"]
        assert out["kind"] == "appearances"

    def test_name_filter_narrows_the_document_set(self, libs):
        libs()
        design = _design(materials=[_entry("Steel", "m1"), _entry("Brass", "m2")])
        out, _err = mat.browse(design, "materials", name_filter="bra")
        assert [e["name"] for e in out["document"]["entries"]] == ["Brass"]
        assert out["document"]["count"] == 1

    def test_document_page_is_capped_and_flagged(self, libs):
        libs()
        design = _design(materials=[_entry(f"M{i}", f"m{i}") for i in range(4)])
        out, _err = mat.browse(design, "materials", max_results=2)
        assert out["document"]["returned"] == 2 and out["document"]["count"] == 4
        assert out["document"]["truncated"] is True


class TestBrowseLevelOne:
    def test_named_library_lists_its_entries(self, libs):
        libs(_library("Fusion Material Library", "lib-1",
                      materials=[_entry("Steel", "m1"), _entry("Brass", "m2")]))
        out, err = mat.browse(_design(), "materials", library="fusion material library")
        assert err is None
        assert out["library"] == "Fusion Material Library" and out["library_id"] == "lib-1"
        assert [e["name"] for e in out["entries"]] == ["Steel", "Brass"]
        assert out["count"] == 2 and out["returned"] == 2 and "truncated" not in out

    def test_cap_sets_truncated_and_keeps_the_true_count(self, libs):
        libs(_library("Big", "lib-1", materials=[_entry(f"M{i}", f"m{i}") for i in range(324)]))
        out, _err = mat.browse(_design(), "materials", library="Big", max_results=5)
        assert out["returned"] == 5 and out["count"] == 324 and out["truncated"] is True
        assert "name_filter=" in out["note"]

    def test_filter_applies_within_the_library(self, libs):
        libs(_library("Big", "lib-1", materials=[_entry("Steel", "m1"), _entry("Brass", "m2")]))
        out, _err = mat.browse(_design(), "materials", library="Big", name_filter="steel")
        assert [e["name"] for e in out["entries"]] == ["Steel"] and out["count"] == 1

    def test_unknown_library_errors_naming_the_loaded_ones(self, libs):
        libs(_library("Fusion Material Library", "lib-1"))
        out, err = mat.browse(_design(), "materials", library="Ghost")
        assert out is None and "Fusion Material Library" in error_message(err)

    def test_library_rows_omit_the_usage_flag(self, libs):
        libs(_library("Big", "lib-1", materials=[_entry("Steel", "m1", is_used=True)]))
        out, _err = mat.browse(_design(), "materials", library="Big")
        assert "is_used" not in out["entries"][0]


class TestBrowseGuards:
    def test_unknown_kind_errors(self, libs):
        out, err = mat.browse(_design(), "colours")
        assert out is None and "colours" in error_message(err)


class TestUnreadableIsMarkedNotEmpty:
    """A catalog that could not be read must never publish as an EMPTY catalog: an agent picks a
    name from this list, and "no such material exists" is a different fact from "the list did not
    read"."""

    def test_unreadable_document_collection_is_marked(self, libs):
        libs(_library("Fusion Material Library", "lib-1"))
        out, err = mat.browse(SimpleNamespace(), "materials")     # no design.materials at all
        assert err is None
        assert out["document"]["readable"] is False and out["document"]["count"] == 0
        assert "UNKNOWN, not empty" in out["note"]

    def test_a_readable_but_empty_document_set_is_not_marked_unreadable(self, libs):
        # the boundary: a document that genuinely holds no local materials still reads count 0 with
        # readable=true, so the marker never cries wolf on an empty catalog.
        libs()
        out, _err = mat.browse(_design(), "materials")
        assert out["document"]["readable"] is True and out["document"]["count"] == 0
        assert "UNKNOWN, not empty" not in out["note"]

    def test_unreadable_library_collection_is_marked(self, libs, monkeypatch):
        libs()
        monkeypatch.setattr(mat, "app", SimpleNamespace())
        out, _err = mat.browse(_design(), "materials")
        assert out["libraries"] == [] and out["libraries_readable"] is False
        assert "libraries_readable=false" in out["note"]

    def test_a_library_row_with_a_null_count_is_tallied(self, libs):
        blind = _library("Half-read", "lib-1")
        del blind.materials
        libs(blind, _library("Whole", "lib-2"))
        out, _err = mat.browse(_design(), "materials")
        assert out["unread_libraries"] == 1
        assert "unread_libraries" in out["note"]

    def test_unreadable_named_library_entries_are_marked(self, libs):
        lib = _library("Fusion Material Library", "lib-1")
        del lib.materials
        libs(lib)
        out, err = mat.browse(_design(), "materials", library="Fusion Material Library")
        assert err is None
        assert out["readable"] is False and out["count"] == 0 and out["entries"] == []
        assert "UNKNOWN, not empty" in out["note"]

    def test_a_readable_named_library_is_flagged_readable(self, libs):
        libs(_library("Big", "lib-1", materials=[_entry("Steel", "m1")]))
        out, _err = mat.browse(_design(), "materials", library="Big")
        assert out["readable"] is True and out["count"] == 1

    def test_an_unreadable_is_used_flag_is_null_not_false(self, libs):
        # is_used false is what a caller overwrites or deletes an entry on - it may not be inferred
        # from a flag that did not read.
        libs()
        entry = _entry("Steel", "m1")
        del entry.isUsed
        out, _err = mat.browse(_design(materials=[entry]), "materials")
        assert out["document"]["entries"][0]["is_used"] is None
