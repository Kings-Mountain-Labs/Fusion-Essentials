# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Unit tests for ``sys_get_guidance`` - the packaged design guidance, one section per call.

No adsk.*: the tool reads a JSON file that ships beside the server. The tests read that same file
independently (never through the loader) so "the payload equals the canonical record" is a real
comparison, drive the wire contract through the REAL SimpleMCPServer, and pin the two pointers - the
cold-start instructions and the capability map - to a tool that actually registers.
"""

import asyncio
import hashlib
import json
import os
import re

import pytest

from conftest import load_mcp_server, load_tool, register_all_tools

gd = load_tool("sys_get_guidance")

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_JSON_PATH = os.path.join(_REPO, "commands", "mcpServer", "guidance", "parametric_cad_design.json")
_SERVER_SRC = os.path.join(_REPO, "commands", "mcpServer", "server", "mcp_server.py")
_TOOL_SRC = os.path.join(_REPO, "commands", "mcpServer", "tools", "sys_get_guidance.py")
_GUIDANCE_SRC = tuple(os.path.join(_REPO, "commands", "mcpServer", "guidance", name)
                      for name in ("__init__.py", "loader.py", "render.py", "resources.py"))

_IDS = list(gd.loader.SECTION_IDS)
_RECIPE_IDS = list(gd.loader.RECIPE_IDS)

# The most rules one section read answers with - the packaged number, which gen_guidance.validate
# also refuses a section past (test_gen_guidance.py holds the two sides against each other).
_CAP = gd.loader.MAX_SECTION_RULES


def _canonical():
    """The shipped document, read straight off disk - the tests' own copy of the truth."""
    with open(_JSON_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _payload(result):
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _rule(section_id, rule_id):
    return {"id": rule_id, "section": section_id, "scenarios": ["simple_part"], "when": "w",
            "do": "d", "except": "e", "prove": [{"tool": "design_get", "observe": "o"}]}


def _recipe_record(recipe_id, section_id="manufacture"):
    return {"id": recipe_id, "section": section_id, "title": "T", "scenarios": ["simple_part"],
            "use_when": "u", "steps": [{"tool": "cam_get", "do": "d", "read_back": "r"}],
            "bar": {"measure": "m", "eyes": "e"}}


def _doctored(rule_count=1, section_id="kernel", recipes=()):
    """A synthetic document with one section holding `rule_count` rules - the in-process stand-in
    for data the shipped file does not carry (an oversized section, a missing one)."""
    return {"guidance_id": "doctored", "title": "T", "scenarios": ["simple_part"],
            "sections": [{"id": section_id, "title": "S", "use_when": "u",
                          "rules": [_rule(section_id, f"r{i}") for i in range(rule_count)],
                          "recipes": list(recipes)}]}


@pytest.fixture
def serve(monkeypatch):
    """Serve a doctored document instead of the packaged one, for the branches the shipped data
    cannot reach. Returns a callable taking the document."""
    def _install(doc, sha="0" * 64):
        monkeypatch.setattr(gd.loader, "load", lambda path=None: (doc, sha))
    return _install


def _strings(node):
    """Every string in a decoded payload - keys and values, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)
    elif isinstance(node, str):
        yield node


# ── the index: no arguments ─────────────────────────────────────────────────

class TestSectionIndex:
    def test_no_arguments_returns_one_row_per_section_in_document_order(self):
        out = _payload(gd.handler())
        assert [row["id"] for row in out["sections"]] == _IDS
        assert out["section"] is None
        assert out["next_sections"] == _IDS
        assert out["guidance_id"] == _canonical()["guidance_id"]

    def test_each_index_row_carries_its_title_counts_and_use_when(self):
        canonical = {sec["id"]: sec for sec in _canonical()["sections"]}
        rows = {row["id"]: row for row in _payload(gd.handler())["sections"]}
        for section_id in _IDS:
            assert rows[section_id]["rule_count"] == len(canonical[section_id]["rules"])
            assert rows[section_id]["recipe_count"] == len(canonical[section_id]["recipes"])
            assert rows[section_id]["title"]
            assert rows[section_id]["use_when"] == canonical[section_id]["use_when"]

    def test_the_index_maps_every_recipe_by_id_section_title_and_use_when(self):
        # the map an agent picks ONE recipe from: without it the recipe read is a call nobody can
        # aim, since the ids live only in the schema enum.
        canonical = [r for sec in _canonical()["sections"] for r in sec["recipes"]]
        rows = _payload(gd.handler())["recipes"]
        assert [r["id"] for r in rows] == [r["id"] for r in canonical]
        for row, rec in zip(rows, canonical):
            assert row == {"id": rec["id"], "section": rec["section"], "title": rec["title"],
                           "use_when": rec["use_when"]}

    def test_the_index_carries_no_rules_and_no_recipe_steps_at_all(self):
        # the whole point of an index: a client that asked for nothing is not sent the document.
        out = _payload(gd.handler())
        assert "rules" not in out
        assert all("rules" not in row for row in out["sections"])
        assert all("steps" not in row and "bar" not in row for row in out["recipes"])

    def test_the_index_publishes_the_scenario_vocabulary_the_rules_declare_from(self):
        assert _payload(gd.handler())["scenarios"] == _canonical()["scenarios"]

    def test_sha256_is_the_hash_of_the_packaged_file_itself(self):
        with open(_JSON_PATH, "rb") as fh:
            expected = hashlib.sha256(fh.read()).hexdigest()
        assert _payload(gd.handler())["sha256"] == expected

    def test_an_empty_section_string_reads_as_no_section(self):
        assert _payload(gd.handler(section=""))["section"] is None


# ── one section ─────────────────────────────────────────────────────────────

class TestOneSection:
    @pytest.mark.parametrize("section_id", _IDS)
    def test_each_section_returns_its_own_rules_and_nothing_else(self, section_id):
        canonical = {sec["id"]: sec for sec in _canonical()["sections"]}[section_id]
        out = _payload(gd.handler(section=section_id))
        assert out["section"] == section_id
        assert out["section_title"] == canonical["title"]
        assert [r["id"] for r in out["rules"]] == [r["id"] for r in canonical["rules"]]

    @pytest.mark.parametrize("section_id", _IDS)
    def test_every_returned_rule_equals_the_canonical_record(self, section_id):
        # Structure for structure, not a paraphrase: a renamed field, a dropped 'except' or a
        # reworded 'prove' step in the payload builder fails here.
        canonical = {sec["id"]: sec for sec in _canonical()["sections"]}[section_id]
        assert _payload(gd.handler(section=section_id))["rules"] == canonical["rules"]

    def test_next_sections_names_the_others_and_never_the_one_returned(self):
        out = _payload(gd.handler(section="assemble"))
        assert out["next_sections"] == [i for i in _IDS if i != "assemble"]

    def test_a_section_read_carries_the_same_id_and_hash_as_the_index(self):
        index, section = _payload(gd.handler()), _payload(gd.handler(section="kernel"))
        assert section["guidance_id"] == index["guidance_id"]
        assert section["sha256"] == index["sha256"]

    @pytest.mark.parametrize("section_id", _IDS)
    def test_a_section_names_its_recipes_without_carrying_one(self, section_id):
        # the section read is the RULES plus an index of what else this section offers: a recipe
        # body here would flood the read an agent makes to orient within a section.
        canonical = {sec["id"]: sec for sec in _canonical()["sections"]}[section_id]
        out = _payload(gd.handler(section=section_id))
        assert out["use_when"] == canonical["use_when"]
        assert out["recipe_index"] == [{"id": r["id"], "title": r["title"],
                                        "use_when": r["use_when"]}
                                       for r in canonical["recipes"]]
        assert all("steps" not in row for row in out["recipe_index"])


# ── one recipe ──────────────────────────────────────────────────────────────

class TestOneRecipe:
    @pytest.mark.parametrize("recipe_id", _RECIPE_IDS)
    def test_a_recipe_comes_back_whole_and_equals_the_canonical_record(self, recipe_id):
        canonical = {r["id"]: r for sec in _canonical()["sections"] for r in sec["recipes"]}
        out = _payload(gd.handler(recipe=recipe_id))
        assert out["recipe"] == canonical[recipe_id]

    def test_a_recipe_read_carries_the_same_id_and_hash_and_address_as_the_index(self):
        index = _payload(gd.handler())
        out = _payload(gd.handler(recipe=_RECIPE_IDS[0]))
        assert out["guidance_id"] == index["guidance_id"]
        assert out["sha256"] == index["sha256"]
        assert out["resource_uri"] == index["resource_uri"]

    def test_the_note_says_the_steps_are_ordered_and_what_the_bar_is(self):
        # a recipe is a SEQUENCE; a client that reads its steps as a menu picks one and skips the
        # read-backs, so the ordering and the read-back are what the note has to say.
        note = _payload(gd.handler(recipe=_RECIPE_IDS[0]))["note"]
        assert "ORDERED" in note and "read_back" in note and "'bar'" in note

    def test_a_recipe_read_carries_no_rules(self):
        out = _payload(gd.handler(recipe=_RECIPE_IDS[0]))
        assert "rules" not in out and "sections" not in out

    def test_asking_for_a_section_and_a_recipe_at_once_is_refused_naming_both(self):
        # they are two different reads, and answering one of them silently would hand back a
        # payload that does not match what was asked for.
        result = gd.handler(section="finish", recipe=_RECIPE_IDS[0])
        assert result["isError"] is True
        assert "finish" in result["message"] and _RECIPE_IDS[0] in result["message"]

    def test_a_recipe_id_the_document_does_not_carry_is_refused_naming_what_it_does(self, serve):
        serve(_doctored(recipes=[_recipe_record("other-recipe")]))
        result = gd.handler(recipe=_RECIPE_IDS[0])
        assert result["isError"] is True
        assert _RECIPE_IDS[0] in result["message"] and "other-recipe" in result["message"]

    def test_a_document_carrying_no_recipe_at_all_says_so(self, serve):
        # 'none' rather than nothing: a refusal ending in "It carries: ." reads as a bug.
        serve(_doctored())
        result = gd.handler(recipe=_RECIPE_IDS[0])
        assert result["isError"] is True and "none" in result["message"]

    def test_an_unknown_recipe_is_refused_naming_every_legal_id(self):
        result = gd.handler(recipe="choose-a-strategy")
        assert result["isError"] is True
        for recipe_id in _RECIPE_IDS:
            assert recipe_id in result["message"]

    def test_a_bad_recipe_is_refused_before_the_document_is_even_read(self, monkeypatch):
        reads = []
        monkeypatch.setattr(gd.loader, "load",
                            lambda path=None: (reads.append(1), ({}, ""))[1])
        assert gd.handler(recipe="nope")["isError"] is True
        assert reads == []

    def test_an_empty_recipe_string_reads_as_no_recipe(self):
        assert _payload(gd.handler(recipe=""))["section"] is None


# ── the address the resource channel serves ─────────────────────────────────

class TestTheResourceAddress:
    def test_the_index_publishes_the_address_of_the_same_document(self):
        assert (_payload(gd.handler())["resource_uri"]
                == "fusion-essentials://guidance/parametric-cad-design")

    def test_a_section_read_carries_the_same_address(self):
        assert (_payload(gd.handler(section="kernel"))["resource_uri"]
                == _payload(gd.handler())["resource_uri"])

    def test_the_address_is_the_one_the_resource_catalog_actually_serves(self):
        # a published address that resources/read does not answer at is worse than none: the tool
        # and the catalog build it through the same function, and this is that seam.
        assert _payload(gd.handler())["resource_uri"] == gd.resources.catalog()[0]["uri"]

    def test_the_address_follows_the_document_that_answered(self, serve):
        serve(_doctored())
        assert _payload(gd.handler())["resource_uri"].endswith("/doctored")

    def test_the_note_says_what_the_address_is_for(self):
        # a bare URI in a payload teaches nothing; the note is where a client learns it can read
        # the whole document there instead of calling this tool per section.
        assert "resource_uri" in _payload(gd.handler())["note"]


# ── the bound ───────────────────────────────────────────────────────────────

class TestRuleBound:
    def test_a_section_holding_exactly_the_cap_is_returned_whole(self, serve):
        serve(_doctored(rule_count=_CAP))
        out = _payload(gd.handler(section="kernel"))
        assert out["rule_count"] == _CAP
        assert len(out["rules"]) == _CAP
        assert "truncated" not in out and "rule_total" not in out

    def test_one_rule_past_the_cap_truncates_and_counts_what_was_dropped(self, serve):
        serve(_doctored(rule_count=_CAP + 1))
        out = _payload(gd.handler(section="kernel"))
        assert out["truncated"] is True
        assert out["rule_total"] == _CAP + 1
        assert out["rule_count"] == _CAP
        assert len(out["rules"]) == _CAP

    def test_the_shipped_document_is_served_untruncated(self):
        # the cap exists for a document that grows; today every section fits under it, so no
        # section read reports a truncation the caller would have to page around.
        for section_id in _IDS:
            assert "truncated" not in _payload(gd.handler(section=section_id))

    def test_the_cap_the_tool_serves_is_the_packaged_one_read_at_call_time(self, serve,
                                                                          monkeypatch):
        # the number lives in the guidance package so the authoring gate can refuse a section past
        # it; a copy taken in this module would keep serving 8 while the gate moved.
        monkeypatch.setattr(gd.loader, "MAX_SECTION_RULES", 2)
        serve(_doctored(rule_count=3))
        out = _payload(gd.handler(section="kernel"))
        assert out["rule_count"] == 2 and out["rule_total"] == 3


class TestTheTruncationNamesTheUncappedChannel:
    def test_a_truncated_section_points_at_the_resource_that_serves_them_all(self, serve):
        serve(_doctored(rule_count=_CAP + 1))
        out = _payload(gd.handler(section="kernel"))
        assert gd.TRUNCATED_NOTE.strip() in out["note"]
        assert "resource_uri" in out["note"] and out["resource_uri"].endswith("/doctored")

    def test_an_untruncated_section_carries_no_escape_sentence(self, serve):
        # the pointer ships where it is needed and nowhere else: a section that came back whole has
        # nothing to page around, and a standing "read it elsewhere" sentence would say it does.
        serve(_doctored(rule_count=_CAP))
        assert _payload(gd.handler(section="kernel"))["note"] == gd.SECTION_NOTE

    def test_the_shipped_section_reads_carry_only_the_record_note(self):
        for section_id in _IDS:
            assert _payload(gd.handler(section=section_id))["note"] == gd.SECTION_NOTE

    def test_the_note_glosses_the_kind_field_the_rules_carry(self):
        # 'kind' is the one field a rule carries that names nothing in the record shape, and the
        # rendered document marks it - a client reading records alone has only this sentence.
        note = _payload(gd.handler(section="finish"))["note"]
        assert "'kind'" in note and "safety invariant" in note
        kinds = [r.get("kind") for r in _payload(gd.handler(section="finish"))["rules"]]
        assert "safety_invariant" in kinds


# ── refusals ────────────────────────────────────────────────────────────────

class TestRefusals:
    def test_an_unknown_section_is_refused_naming_every_legal_id(self):
        result = gd.handler(section="assembly")
        assert result["isError"] is True
        for section_id in _IDS:
            assert section_id in result["message"]
        assert "assembly" in result["message"]

    def test_a_section_the_document_does_not_carry_is_refused_naming_what_it_does(self, serve):
        # the declared list and the packaged data disagreeing is a data defect, reported as the
        # sections the document actually holds - never as an empty rule list.
        serve(_doctored(section_id="kernel"))
        result = gd.handler(section="assemble")
        assert result["isError"] is True
        assert "assemble" in result["message"] and "kernel" in result["message"]

    def test_a_packaged_document_that_did_not_load_is_reported_not_substituted(self, monkeypatch):
        def _raise(path=None):
            raise gd.loader.GuidanceUnavailable("the file is not there: guidance.json")
        monkeypatch.setattr(gd.loader, "load", _raise)
        result = gd.handler()
        assert result["isError"] is True
        assert "guidance.json" in result["message"]

    def test_a_bad_section_is_refused_before_the_document_is_even_read(self, monkeypatch):
        reads = []
        monkeypatch.setattr(gd.loader, "load",
                            lambda path=None: (reads.append(1), ({}, ""))[1])
        assert gd.handler(section="nope")["isError"] is True
        assert reads == []


# ── ASCII ───────────────────────────────────────────────────────────────────

class TestAscii:
    def test_every_string_the_payload_carries_is_pure_ascii(self):
        # the payload crosses the wire JSON-encoded with ensure_ascii, so a non-ASCII character
        # ships as a 6-character escape. Checked on the DECODED values, where it is still visible.
        payloads = ([_payload(gd.handler())] + [_payload(gd.handler(section=i)) for i in _IDS]
                    + [_payload(gd.handler(recipe=r)) for r in _RECIPE_IDS])
        bad = [(s, hex(ord(c))) for p in payloads for s in _strings(p)
               for c in s if ord(c) > 127]
        assert not bad, f"non-ASCII in the guidance payload: {bad}"

    def test_the_ascii_walk_reaches_the_rule_records(self):
        # the walk is only worth its lines while it descends into the nested rule dicts.
        assert "connected-reference-path" in set(_strings(_payload(gd.handler(section="assemble"))))

    def test_the_ascii_walk_reaches_the_recipe_steps(self):
        # a recipe nests one level deeper than a rule: its steps are dicts inside a list inside the
        # record, and a walk that stopped short would clear a payload it never read.
        strings = set(_strings(_payload(gd.handler(recipe=_RECIPE_IDS[0]))))
        assert "read_back" in strings


# ── the wire contract, through the real server ──────────────────────────────

@pytest.fixture(scope="module")
def server():
    """The REAL SimpleMCPServer with every tool registered - the same object tools/list is served
    from, so presence and dispatch are proven against the transport, not a stand-in."""
    mcp_server = load_mcp_server()
    srv = mcp_server.SimpleMCPServer()
    for item in register_all_tools():
        srv.register(item)
    return srv


def _entry(server):
    tools = server._handle_tools_list(1)["result"]["tools"]
    return {t["name"]: t for t in tools}["sys_get_guidance"]


def _call(server, arguments):
    return asyncio.run(server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "sys_get_guidance", "arguments": arguments},
    }))["result"]


class TestWireContract:
    def test_the_tool_is_in_tools_list_read_only_and_strict(self, server):
        entry = _entry(server)
        assert entry["annotations"]["readOnlyHint"] is True
        assert entry["annotations"].get("destructiveHint", False) is False
        assert entry["inputSchema"]["additionalProperties"] is False

    def test_the_section_input_carries_the_closed_id_set_as_a_schema_enum(self, server):
        assert _entry(server)["inputSchema"]["properties"]["section"]["enum"] == _IDS

    def test_the_recipe_input_carries_the_shipped_recipe_ids_as_a_schema_enum(self, server):
        assert _entry(server)["inputSchema"]["properties"]["recipe"]["enum"] == _RECIPE_IDS

    def test_one_recipe_comes_back_through_a_real_tools_call(self, server):
        canonical = {r["id"]: r for sec in _canonical()["sections"] for r in sec["recipes"]}
        out = _payload(_call(server, {"recipe": _RECIPE_IDS[0]}))
        assert out["recipe"] == canonical[_RECIPE_IDS[0]]

    def test_the_server_refuses_an_out_of_enum_recipe_before_dispatch(self, server):
        result = _call(server, {"recipe": "no-such-recipe"})
        assert result["isError"] is True
        assert "Valid values:" in result["message"] and "'recipe'" in result["message"]

    def test_it_runs_off_the_main_thread(self):
        assert gd.item.run_on_main_thread is False

    def test_the_module_never_touches_adsk(self):
        # the whole chain this off-main-thread handler pulls in: the tool AND the guidance package
        # behind it. An adsk call anywhere in it would run off Fusion's main thread.
        for path in (_TOOL_SRC,) + _GUIDANCE_SRC:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            assert "import adsk" not in src, path
            assert "adsk." not in src, path

    def test_the_index_comes_back_through_a_real_tools_call(self, server):
        out = _payload(_call(server, {}))
        assert [row["id"] for row in out["sections"]] == _IDS

    def test_one_section_comes_back_through_a_real_tools_call(self, server):
        canonical = {sec["id"]: sec for sec in _canonical()["sections"]}["assemble"]
        assert _payload(_call(server, {"section": "assemble"}))["rules"] == canonical["rules"]

    def test_the_server_refuses_an_out_of_enum_section_before_dispatch(self, server):
        # pinned on the SERVER's own wording: the handler refuses a bad section too, so a needle
        # both messages carry would pass with the schema gate switched off entirely.
        result = _call(server, {"section": "assembly"})
        assert result["isError"] is True
        assert "Valid values:" in result["message"] and "'section'" in result["message"]

    def test_the_server_refuses_an_unknown_argument(self, server):
        # same rule: a bare 'guidance_id' needle also matches the TypeError a lenient schema would
        # produce, so the strict gate's own sentence is what this pins.
        result = _call(server, {"guidance_id": "parametric-cad-design"})
        assert result["isError"] is True
        assert "Unknown argument for tool 'sys_get_guidance'" in result["message"]


# ── the two pointers ────────────────────────────────────────────────────────

class TestPointersNameARegisteredTool:
    def test_the_cold_start_instructions_point_at_this_tool(self, server):
        # read off the INSTRUCTIONS literal the way the cold-start test does (mcp_server imports
        # Fusion utils this harness cannot load), so the pointer is checked in the text that ships.
        with open(_SERVER_SRC, encoding="utf-8") as fh:
            src = fh.read()
        literal = re.search(r"INSTRUCTIONS\s*=\s*\((.*?)\)\n", src, re.DOTALL)
        assert literal, "INSTRUCTIONS literal not found in mcp_server.py"
        assert "sys_get_guidance" in "".join(re.findall(r'"([^"]*)"', literal.group(1)))
        assert "sys_get_guidance" in server.tools

    def test_the_capability_map_points_at_this_tool(self, server):
        cm = load_tool("sys_capability_map")
        note = _payload(cm.handler())["note"]
        assert "sys_get_guidance" in note
        assert "sys_get_guidance" in server.tools


# ── the loader ──────────────────────────────────────────────────────────────

class TestLoader:
    def test_the_declared_section_ids_are_the_shipped_documents_own(self):
        doc, _sha = gd.loader.load()
        assert gd.loader.section_ids(doc) == _IDS

    def test_the_declared_recipe_ids_are_the_shipped_documents_own(self):
        # the tuple is what the schema enum is built from, so a recipe added to the JSON without
        # it is a recipe no client can ask for.
        doc, _sha = gd.loader.load()
        assert [r["id"] for r in gd.loader.recipes(doc)] == _RECIPE_IDS

    def test_find_recipe_matches_exactly_and_answers_none_otherwise(self):
        doc, _sha = gd.loader.load()
        assert gd.loader.find_recipe(doc, _RECIPE_IDS[0])["id"] == _RECIPE_IDS[0]
        assert gd.loader.find_recipe(doc, _RECIPE_IDS[0][:-1]) is None
        assert gd.loader.find_recipe(doc, _RECIPE_IDS[0].upper()) is None

    def test_the_document_loads_from_any_working_directory(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        doc, sha = gd.loader.load()
        assert gd.loader.section_ids(doc) == _IDS
        assert len(sha) == 64

    def test_a_working_directory_relative_path_does_not_survive_the_same_move(self, monkeypatch,
                                                                             tmp_path):
        # what makes the test above non-vacuous: the same file named RELATIVELY is unreadable from
        # there, so the module-relative resolution is what carried it.
        relative = os.path.join("commands", "mcpServer", "guidance", "parametric_cad_design.json")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(gd.loader.GuidanceUnavailable):
            gd.loader.load(relative)

    def test_a_missing_packaged_file_is_reported_by_name(self, tmp_path):
        missing = str(tmp_path / "gone.json")
        with pytest.raises(gd.loader.GuidanceUnavailable) as exc:
            gd.loader.load(missing)
        assert "gone.json" in str(exc.value)

    def test_a_file_that_is_not_json_is_reported_as_such(self, tmp_path):
        broken = tmp_path / "broken.json"
        broken.write_text('{"sections": [', encoding="utf-8")
        with pytest.raises(gd.loader.GuidanceUnavailable) as exc:
            gd.loader.load(str(broken))
        assert "JSON" in str(exc.value) and "broken.json" in str(exc.value)

    def test_the_hash_follows_the_bytes(self, tmp_path):
        one, two = tmp_path / "a.json", tmp_path / "b.json"
        one.write_text('{"sections": []}', encoding="utf-8")
        two.write_text('{"sections": [] }', encoding="utf-8")
        assert gd.loader.load(str(one))[1] != gd.loader.load(str(two))[1]

    def test_find_section_matches_exactly_and_answers_none_otherwise(self):
        doc, _sha = gd.loader.load()
        assert gd.loader.find_section(doc, "assemble")["id"] == "assemble"
        assert gd.loader.find_section(doc, "assem") is None
        assert gd.loader.find_section(doc, "Assemble") is None
