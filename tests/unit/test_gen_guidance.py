# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Tests for the canonical guidance data and the generator that renders the Claude skill from it.

Two halves. The SHIPPED half asserts the committed JSON against the live tool registry and the
committed skill against the renderer, so the client skill cannot drift from the data and a proof
step cannot point a client at a tool the server does not register. The SYNTHETIC half drives each
structural check with data built to break exactly it - duplicate ids, a rule filed under the wrong
section, a scenario the enum does not know, and each size cap at its exact boundary - because a
cap that never fires reads identically to one that cannot.

Whether a rule's WORDING is honest (that the named tool really shows what 'observe' claims) is a
reading judgment and stays in review; these tests check shape, routing, and size.
"""

import copy
import json
import os
import re

import pytest

import gen_guidance
from conftest import load_tool, register_all_tools


@pytest.fixture(scope="module")
def tool_names():
    """Every registered tool name - the same inventory the wire-surface tests read."""
    return frozenset(item.get_name() for item in register_all_tools())


@pytest.fixture(scope="module")
def shipped():
    return gen_guidance.load()


# ── synthetic data: a minimal valid document, then one break per check ─────────

_SCENARIOS = ["alpha", "beta"]
_NAMES = frozenset({"doc_get", "design_get"})

# The synthetic document's recipe-carrying section: not the kernel, which may hold none.
_RECIPE_SECTION = "manufacture"


def _rule(rule_id, section_id, scenarios=None, **over):
    rule = {
        "id": rule_id,
        "section": section_id,
        "scenarios": sorted(scenarios if scenarios is not None else _SCENARIOS),
        "when": "a case arises",
        "do": "take the action",
        "except": "the other case",
        "prove": [{"tool": "doc_get", "observe": "what the document reports"}],
    }
    rule.update(over)
    return rule


def _recipe(recipe_id, section_id, scenarios=None, steps=None, **over):
    recipe = {
        "id": recipe_id,
        "section": section_id,
        "scenarios": sorted(scenarios if scenarios is not None else ["alpha"]),
        "title": "A worked sequence",
        "use_when": "the situation arises",
        "steps": steps if steps is not None else [
            {"tool": "doc_get", "do": "open the document", "read_back": "its save state"},
            {"tool": "design_get", "do": "read the timeline", "read_back": "the feature that landed"},
            {"tool": "doc_get", "do": "save", "read_back": "the new version"},
        ],
        "bar": {"measure": "the measured value holds", "eyes": "the part reads as the object"},
    }
    recipe.update(over)
    return recipe


# The one recipe id the shipped tool surface points at (cam_get's strategies note), so every
# document validate() sees must declare it - the synthetic baseline included.
_POINTED_AT = gen_guidance.STRATEGY_RECIPE_ID


def _doc(**over):
    """A minimal document that validates: one rule per section, the kernel universal, and the one
    recipe the tool surface points at."""
    doc = {
        "guidance_id": "probe",
        "name": "probe",
        "title": "Probe",
        "description": "A probe document.",
        "summary": "A probe.",
        "scenarios": list(_SCENARIOS),
        "sections": [
            {
                "id": section_id,
                "title": section_id.capitalize(),
                "use_when": "this part of the build is next",
                "rules": [_rule(
                    f"{section_id}-rule", section_id,
                    scenarios=_SCENARIOS if section_id == gen_guidance.KERNEL else ["alpha"])],
                "recipes": ([_recipe(_POINTED_AT, section_id)]
                            if section_id == _RECIPE_SECTION else []),
            }
            for section_id in gen_guidance.SECTION_IDS
        ],
    }
    doc.update(over)
    return doc


def _section(doc, section_id):
    return doc["sections"][gen_guidance.SECTION_IDS.index(section_id)]


def _pad(doc, holder, key, measure, target):
    """Grow `holder[key]` by single words until `measure(doc)` is exactly `target`.

    Each pad word adds exactly one rendered word, which is what lets a cap be probed AT its
    boundary and one word over it rather than somewhere past it. The holder is passed in because
    each cap measures a different render, and only some of them carry a given field.
    """
    while measure(doc) < target:
        holder[key] += " pad"
    assert measure(doc) == target
    return doc


def _kernel_words(doc):
    return gen_guidance.word_count(gen_guidance.section_text(doc, gen_guidance.KERNEL))


def _map_words(doc):
    return gen_guidance.word_count(gen_guidance.map_text(doc))


def _recipe_words(doc, recipe_id=_POINTED_AT):
    return gen_guidance.word_count(gen_guidance.recipe_text(doc, recipe_id))


def _problems(doc, names=_NAMES):
    return gen_guidance.validate(doc, names)


def assert_diagnostic(problems, needle):
    hits = [p for p in problems if needle in p]
    assert hits, f"no problem mentioning {needle!r} in {problems}"
    return hits


# ── the shipped package ───────────────────────────────────────────────────────

class TestShippedGuidance:
    def test_the_canonical_json_validates_against_the_live_registry(self, shipped, tool_names):
        assert gen_guidance.validate(shipped, tool_names) == []

    def test_the_synthetic_baseline_is_itself_valid(self):
        # every bite below is one edit away from this document; a baseline that already failed
        # would make each of them pass for the wrong reason.
        assert _problems(_doc()) == []

    def test_every_prove_step_names_a_registered_tool(self, shipped, tool_names):
        cited = sorted({step["tool"] for rule in gen_guidance.rules(shipped)
                        for step in rule["prove"]})
        assert cited, "the guidance names no tool at all"
        assert not [name for name in cited if name not in tool_names]

    def test_every_committed_package_file_is_exactly_what_the_generator_renders(self, shipped):
        # every file, not just SKILL.md: a playbook is a committed artifact an agent reads without
        # calling the server at all, so a stale one ships guidance the document no longer carries.
        for path, text in gen_guidance.package(shipped).items():
            with open(path, encoding="utf-8") as fh:
                assert fh.read() == text, path

    def test_the_package_is_the_map_plus_one_playbook_per_non_kernel_section(self, shipped):
        paths = gen_guidance.package(shipped)
        assert gen_guidance.SKILL_PATH in paths
        expected = [gen_guidance.playbook_path(s) for s in gen_guidance.SECTION_IDS
                    if s != gen_guidance.KERNEL]
        assert sorted(paths) == sorted([gen_guidance.SKILL_PATH] + expected)

    def test_the_skill_keeps_the_frontmatter_shape_its_readers_parse(self, shipped):
        # the eval harness appends a skill's BODY by splitting on this exact frontmatter block,
        # and the client loader keys the skill by the name inside it.
        rendered = gen_guidance.render(shipped)
        match = re.match(r"^---\n.*?\n---\n+", rendered, re.S)
        assert match, "the render lost the frontmatter block"
        assert f"name: {shipped['name']}\n" in match.group(0)
        assert rendered[match.end():] == gen_guidance.map_text(shipped)

    def test_the_committed_skill_carries_no_recipe_steps(self, shipped):
        # the whole experiment: an agent handed the map holds WHERE the recipes are, not the steps
        # themselves, and fetches the one it needs over the wire.
        body = gen_guidance.map_text(shipped)
        for recipe in gen_guidance.recipes(shipped):
            assert recipe["id"] in body, "the map must name every recipe"
        assert "Read back:" not in body, "a rendered step reached the map"

    def test_the_sections_are_the_canonical_ones_in_order(self, shipped):
        assert [s["id"] for s in shipped["sections"]] == list(gen_guidance.SECTION_IDS)

    def test_every_rule_is_a_member_of_exactly_one_section(self, shipped):
        ids = [rule["id"] for rule in gen_guidance.rules(shipped)]
        assert len(ids) == len(set(ids))
        for sec in shipped["sections"]:
            assert all(rule["section"] == sec["id"] for rule in sec["rules"])

    def test_the_kernel_is_within_its_rule_and_word_caps(self, shipped):
        kernel = _section(shipped, gen_guidance.KERNEL)
        assert len(kernel["rules"]) <= gen_guidance.KERNEL_MAX_RULES
        assert _kernel_words(shipped) <= gen_guidance.KERNEL_MAX_WORDS

    def test_the_skill_map_is_within_its_word_cap(self, shipped):
        assert _map_words(shipped) <= gen_guidance.SKILL_MAP_MAX_WORDS

    def test_every_recipe_is_within_its_word_cap(self, shipped):
        for recipe in gen_guidance.recipes(shipped):
            assert _recipe_words(shipped, recipe["id"]) <= gen_guidance.RECIPE_MAX_WORDS, recipe["id"]

    def test_the_kernel_carries_no_recipes_and_no_section_carries_too_many(self, shipped):
        for sec in shipped["sections"]:
            carried = sec.get("recipes") or []
            if sec["id"] == gen_guidance.KERNEL:
                assert carried == []
            assert len(carried) <= gen_guidance.MAX_RECIPES_PER_SECTION, sec["id"]

    def test_every_recipe_step_names_a_registered_tool(self, shipped, tool_names):
        cited = sorted({step["tool"] for recipe in gen_guidance.recipes(shipped)
                        for step in recipe["steps"]})
        assert cited, "the guidance carries no recipe step at all"
        assert not [name for name in cited if name not in tool_names]

    def test_the_recipe_cam_get_points_at_is_one_the_document_declares(self, shipped):
        # the dangling-pointer guard, asserted on the SHIPPED data rather than only through
        # validate(): cam_get publishes this id in a note, so it has to resolve.
        assert gen_guidance.STRATEGY_RECIPE_ID in {r["id"] for r in gen_guidance.recipes(shipped)}

    def test_a_deterministic_safety_rule_declares_its_kind(self, shipped):
        kinds = {rule["id"]: rule.get("kind") for rule in gen_guidance.rules(shipped)}
        assert kinds["verify-the-write"] == gen_guidance.SAFETY_INVARIANT
        assert all(k in (None, gen_guidance.SAFETY_INVARIANT) for k in kinds.values())


# ── rendering ─────────────────────────────────────────────────────────────────

class TestRender:
    def test_a_rule_renders_its_four_clauses_and_its_proof_steps(self):
        doc = _doc()
        _section(doc, "plan")["rules"][0]["prove"] = [
            {"tool": "doc_get", "observe": "the save state"},
            {"tool": "design_get", "observe": "the timeline"},
        ]
        text = gen_guidance.section_text(doc, "plan")
        # one line per rule: the instruction leads, scope and boundary ride in the same sentence
        assert "- Take the action when a case arises; not for the other case. " in text
        assert "Prove: `doc_get`: the save state; `design_get`: the timeline." in text
        assert "When a case arises:" not in text and "Except " not in text

    def test_an_example_renders_and_its_absence_leaves_no_line(self):
        with_example = _doc()
        _section(with_example, "plan")["rules"][0]["example"] = "a nut on a plain shaft"
        assert "Example: a nut on a plain shaft." in gen_guidance.section_text(with_example, "plan")
        assert "Example:" not in gen_guidance.section_text(_doc(), "plan")

    def test_a_safety_invariant_is_marked_in_the_render(self):
        doc = _doc()
        _section(doc, "finish")["rules"][0]["kind"] = gen_guidance.SAFETY_INVARIANT
        assert "**finish-rule** (safety invariant)" in gen_guidance.section_text(doc, "finish")
        assert "(safety invariant)" not in gen_guidance.section_text(_doc(), "finish")

    def test_the_body_carries_every_section_in_the_canonical_order(self):
        text = gen_guidance.body(_doc())
        positions = [text.index(f"## {s.capitalize()}") for s in gen_guidance.SECTION_IDS]
        assert positions == sorted(positions)

    def test_a_recipe_renders_its_use_when_its_numbered_steps_and_its_bar(self):
        text = gen_guidance.recipe_text(_doc(), _POINTED_AT)
        assert text.startswith("#### A worked sequence")
        assert "Use when the situation arises." in text
        assert "1. `doc_get` - open the document. Read back: its save state." in text
        assert "3. `doc_get` - save. Read back: the new version." in text
        assert "Bar - measure: the measured value holds. Eyes: the part reads as the object." in text

    def test_an_exemplar_renders_and_its_absence_leaves_no_line(self):
        with_exemplar = _doc()
        _section(with_exemplar, _RECIPE_SECTION)["recipes"][0]["exemplar"] = {
            "document": "Configured Dumbbell", "urn": gen_guidance.LINEAGE_PREFIX + "0Unl",
            "look_at": "Handle/Sketch1", "access": "Autodesk Design Samples, read only"}
        text = gen_guidance.recipe_text(with_exemplar, _POINTED_AT)
        assert ("Exemplar: Configured Dumbbell (" + gen_guidance.LINEAGE_PREFIX + "0Unl) - "
                "Handle/Sketch1. Access: Autodesk Design Samples, read only") in text
        assert "Exemplar:" not in gen_guidance.recipe_text(_doc(), _POINTED_AT)

    def test_a_section_renders_its_recipes_after_its_rules(self):
        text = gen_guidance.section_text(_doc(), _RECIPE_SECTION)
        assert text.index(f"**{_RECIPE_SECTION}-rule**") < text.index("### Recipes")
        assert text.index("### Recipes") < text.index("#### A worked sequence")
        assert "### Recipes" not in gen_guidance.section_text(_doc(), "plan")

    def test_the_map_points_at_each_playbook_and_recipe_without_carrying_them(self):
        text = gen_guidance.map_text(_doc())
        assert "**kernel-rule**" in text, "the kernel is IN the map, not pointed at"
        assert "- `kernel` -" not in text, "and so it is never a playbook to go fetch"
        assert "- `plan` - this part of the build is next" in text
        # the fetch rule is stated ONCE per list, never repeated on every row
        assert text.count("sys_get_guidance(section=") == 1
        assert text.count("sys_get_guidance(recipe=") == 1
        assert f"- `{_POINTED_AT}` -" in text
        assert "Read back:" not in text and "**plan-rule**" not in text


# ── each structural check, driven by data built to break it ───────────────────

class TestValidationBites:
    def test_an_unknown_prove_tool_fails_against_the_live_registry(self, shipped, tool_names):
        doc = copy.deepcopy(shipped)
        doc["sections"][0]["rules"][0]["prove"][0]["tool"] = "doc_get_everything"
        assert_diagnostic(gen_guidance.validate(doc, tool_names), "doc_get_everything")

    def test_a_prove_step_with_no_observation_is_reported(self):
        doc = _doc()
        _section(doc, "plan")["rules"][0]["prove"] = [{"tool": "doc_get", "observe": "  "}]
        assert_diagnostic(_problems(doc), "needs a non-empty 'observe'")

    def test_a_duplicate_rule_id_is_reported_naming_both_sections(self):
        doc = _doc()
        _section(doc, "model")["rules"][0]["id"] = "plan-rule"
        _section(doc, "model")["rules"][0]["section"] = "model"
        hits = assert_diagnostic(_problems(doc), "duplicate rule id 'plan-rule'")
        assert "'plan'" in hits[0] and "'model'" in hits[0]

    def test_a_rule_filed_under_the_wrong_section_is_reported(self):
        doc = _doc()
        _section(doc, "sketch")["rules"][0]["section"] = "model"
        assert_diagnostic(_problems(doc), "declares section 'model' but sits in 'sketch'")

    def test_sections_out_of_order_are_reported(self):
        doc = _doc()
        doc["sections"][1], doc["sections"][2] = doc["sections"][2], doc["sections"][1]
        assert_diagnostic(_problems(doc), "sections must be exactly")

    def test_a_missing_required_field_is_reported(self):
        for field in gen_guidance.RULE_FIELDS:
            doc = _doc()
            del _section(doc, "plan")["rules"][0][field]
            assert_diagnostic(_problems(doc), f"is missing '{field}'")

    def test_an_unknown_scenario_is_reported(self):
        doc = _doc()
        _section(doc, "plan")["rules"][0]["scenarios"] = ["alpha", "gamma"]
        assert_diagnostic(_problems(doc), "unknown scenario(s) ['gamma']")

    def test_unsorted_or_duplicated_rule_scenarios_are_reported(self):
        # the needle names the RULE: the document-level enum carries a message this one is a
        # prefix of, so a looser needle would pass on either branch firing.
        doc = _doc()
        _section(doc, "plan")["rules"][0]["scenarios"] = ["beta", "alpha"]
        assert_diagnostic(_problems(doc), "rule 'plan-rule' 'scenarios' must be sorted")
        doc = _doc()
        _section(doc, "plan")["rules"][0]["scenarios"] = ["alpha", "alpha"]
        assert_diagnostic(_problems(doc), "rule 'plan-rule' 'scenarios' must be sorted")

    def test_a_rule_with_an_empty_scenario_list_is_reported(self):
        doc = _doc()
        _section(doc, "plan")["rules"][0]["scenarios"] = []
        assert_diagnostic(_problems(doc), "rule 'plan-rule' needs a non-empty 'scenarios' list")

    def test_a_scenario_no_rule_claims_is_reported(self):
        doc = _doc(scenarios=["alpha", "beta", "gamma"])
        assert_diagnostic(_problems(doc), "scenario 'gamma' is declared but no rule or recipe applies to it")

    def test_a_kernel_rule_that_is_not_universal_is_reported(self):
        doc = _doc()
        _section(doc, gen_guidance.KERNEL)["rules"][0]["scenarios"] = ["alpha"]
        assert_diagnostic(_problems(doc), "must declare every scenario")

    def test_a_kernel_rule_carrying_an_example_is_reported(self):
        doc = _doc()
        _section(doc, gen_guidance.KERNEL)["rules"][0]["example"] = "a worked case"
        assert_diagnostic(_problems(doc), "carries an example")

    def test_an_unknown_kind_is_reported(self):
        doc = _doc()
        _section(doc, "plan")["rules"][0]["kind"] = "best_practice"
        assert_diagnostic(_problems(doc), "declares kind 'best_practice'")

    def test_a_non_ascii_string_is_reported_with_its_path(self):
        doc = _doc()
        _section(doc, "plan")["rules"][0]["do"] = "tilt the face 5°"
        assert_diagnostic(_problems(doc), "guidance.sections[1].rules[0].do carries non-ASCII")

    def test_a_bad_rule_id_is_reported(self):
        doc = _doc()
        _section(doc, "plan")["rules"][0]["id"] = "Plan_Rule"
        assert_diagnostic(_problems(doc), "id must be lowercase letters, digits and '-'")

    def test_an_empty_when_do_or_except_is_reported(self):
        # a present-but-blank clause renders as a sentence with a hole in it, which the
        # missing-key check never sees.
        for field in ("when", "do", "except"):
            doc = _doc()
            _section(doc, "plan")["rules"][0][field] = "   "
            assert_diagnostic(_problems(doc), f"rule 'plan-rule' '{field}' must be a non-empty string")

    def test_a_rule_with_no_prove_step_is_reported(self):
        doc = _doc()
        _section(doc, "plan")["rules"][0]["prove"] = []
        assert_diagnostic(_problems(doc), "rule 'plan-rule' needs at least one 'prove' step")

    def test_a_prove_step_that_is_not_an_object_is_reported(self):
        doc = _doc()
        _section(doc, "plan")["rules"][0]["prove"] = [["doc_get", "what changed"]]
        assert_diagnostic(_problems(doc), "prove step must carry 'tool' and 'observe'")

    def test_an_empty_example_is_reported(self):
        doc = _doc()
        _section(doc, "plan")["rules"][0]["example"] = "  "
        assert_diagnostic(_problems(doc), "'example' must be a non-empty string when present")


class TestRecipeValidationBites:
    """One break per recipe check. A recipe is what an agent is told to FOLLOW, so a step naming a
    tool that does not exist, or a bar with nothing to measure, is a dead instruction."""

    def _recipes(self, doc, *recipes):
        _section(doc, _RECIPE_SECTION)["recipes"] = list(recipes)
        return doc

    def _first(self, doc):
        return _section(doc, _RECIPE_SECTION)["recipes"][0]

    def test_an_unregistered_step_tool_is_reported(self):
        doc = _doc()
        self._first(doc)["steps"][1]["tool"] = "design_get_everything"
        assert_diagnostic(_problems(doc), "steps through 'design_get_everything'")

    def test_a_step_with_no_action_or_no_read_back_is_reported(self):
        for field in ("do", "read_back"):
            doc = _doc()
            self._first(doc)["steps"][0][field] = "   "
            assert_diagnostic(_problems(doc), f"step 'doc_get' needs a non-empty '{field}'")

    def test_a_step_that_is_not_an_object_is_reported(self):
        doc = _doc()
        self._first(doc)["steps"][0] = ["doc_get", "read it"]
        assert_diagnostic(_problems(doc), "step must carry 'tool', 'do' and 'read_back'")

    def test_a_missing_required_recipe_field_is_reported(self):
        for field in gen_guidance.RECIPE_FIELDS:
            doc = _doc()
            del self._first(doc)[field]
            assert_diagnostic(_problems(doc), f"is missing '{field}'")

    def test_a_blank_title_or_use_when_is_reported(self):
        for field in ("title", "use_when"):
            doc = _doc()
            self._first(doc)[field] = "  "
            assert_diagnostic(_problems(doc), f"'{field}' must be a non-empty string")

    def test_a_recipe_filed_under_the_wrong_section_is_reported(self):
        doc = _doc()
        self._first(doc)["section"] = "plan"
        assert_diagnostic(_problems(doc), f"declares section 'plan' but sits in '{_RECIPE_SECTION}'")

    def test_a_duplicate_recipe_id_is_reported_naming_both_sections(self):
        doc = _doc()
        _section(doc, "plan")["recipes"] = [_recipe(_POINTED_AT, "plan")]
        hits = assert_diagnostic(_problems(doc), f"duplicate recipe id '{_POINTED_AT}'")
        assert "'plan'" in hits[0] and f"'{_RECIPE_SECTION}'" in hits[0]

    def test_a_recipe_id_colliding_with_a_rule_id_is_reported(self):
        # one id names one record: recipe=<id> and a rule id are read out of the same name space by
        # an agent, so a collision makes the map ambiguous.
        doc = _doc()
        self._recipes(doc, _recipe(_POINTED_AT, _RECIPE_SECTION),
                      _recipe("plan-rule", _RECIPE_SECTION))
        assert_diagnostic(_problems(doc), "recipe id 'plan-rule' is already a rule id in 'plan'")

    def test_a_rule_id_colliding_with_an_earlier_recipe_id_is_reported(self):
        doc = _doc()
        _section(doc, "plan")["recipes"] = [_recipe("later-rule", "plan")]
        _section(doc, "validate")["rules"][0]["id"] = "later-rule"
        assert_diagnostic(_problems(doc), "rule id 'later-rule' is already a recipe id in 'plan'")

    def test_a_bad_recipe_id_is_reported(self):
        doc = _doc()
        self._recipes(doc, _recipe(_POINTED_AT, _RECIPE_SECTION),
                      _recipe("Choose_A_Strategy", _RECIPE_SECTION))
        assert_diagnostic(_problems(doc), "id must be lowercase letters, digits and '-'")

    def test_a_blank_bar_field_is_reported(self):
        for field in ("measure", "eyes"):
            doc = _doc()
            self._first(doc)["bar"][field] = "  "
            assert_diagnostic(_problems(doc), f"'bar.{field}' must be a non-empty string")

    def test_a_bar_that_is_not_an_object_is_reported(self):
        doc = _doc()
        self._first(doc)["bar"] = "done when it looks right"
        assert_diagnostic(_problems(doc), "'bar' must carry 'measure' and 'eyes'")

    def test_an_exemplar_missing_a_field_is_reported(self):
        for field in gen_guidance.EXEMPLAR_FIELDS:
            doc = _doc()
            exemplar = {"document": "D", "urn": gen_guidance.LINEAGE_PREFIX + "x",
                        "look_at": "L", "access": "A"}
            del exemplar[field]
            self._first(doc)["exemplar"] = exemplar
            assert_diagnostic(_problems(doc), f"'exemplar.{field}' must be a non-empty string")

    def test_an_exemplar_urn_that_is_not_a_lineage_urn_is_reported(self):
        # a version urn names ONE version and goes stale the next time the sample is saved.
        doc = _doc()
        self._first(doc)["exemplar"] = {"document": "D", "urn": "urn:adsk.wipprod:dm.file:abc",
                                        "look_at": "L", "access": "A"}
        assert_diagnostic(_problems(doc), "does not start with 'urn:adsk.wipprod:dm.lineage:'")

    def test_a_valid_exemplar_is_accepted(self):
        doc = _doc()
        self._first(doc)["exemplar"] = {"document": "D", "urn": gen_guidance.LINEAGE_PREFIX + "abc",
                                        "look_at": "L", "access": "A"}
        assert _problems(doc) == []

    def test_a_recipe_in_the_kernel_is_reported(self):
        doc = _doc()
        _section(doc, gen_guidance.KERNEL)["recipes"] = [_recipe("kernel-recipe",
                                                                 gen_guidance.KERNEL)]
        assert_diagnostic(_problems(doc), "the kernel carries no recipes")

    def test_unsorted_recipe_scenarios_are_reported_by_the_same_rule_as_a_rules(self):
        doc = _doc()
        self._first(doc)["scenarios"] = ["beta", "alpha"]
        assert_diagnostic(_problems(doc), f"recipe '{_POINTED_AT}' 'scenarios' must be sorted")

    def test_a_scenario_reached_only_by_a_recipe_is_routed(self):
        # the routing check reads BOTH record kinds: a scenario no rule names is still reached when
        # a recipe names it, and demanding a rule as well would refuse a valid document.
        doc = _doc(scenarios=["alpha", "beta", "gamma"])
        assert_diagnostic(_problems(doc), "scenario 'gamma' is declared but no rule or recipe applies to it")
        _section(doc, _RECIPE_SECTION)["recipes"][0]["scenarios"] = ["alpha", "gamma"]
        assert not [p for p in _problems(doc) if "no rule or recipe applies" in p]
        assert [r["id"] for r in gen_guidance.recipes_for(doc, "gamma")] == [_POINTED_AT]

    def test_a_document_that_does_not_declare_the_pointed_at_recipe_is_reported(self):
        # cam_get's strategies note publishes this id; a document without it leaves that note
        # pointing at a call the server refuses.
        doc = _doc()
        _section(doc, _RECIPE_SECTION)["recipes"] = []
        assert_diagnostic(_problems(doc), f"points at recipe '{_POINTED_AT}'")

    def test_a_section_without_a_use_when_line_is_reported(self):
        doc = _doc()
        _section(doc, "sketch")["use_when"] = "  "
        assert_diagnostic(_problems(doc), "section 'sketch' needs a 'use_when' line")

    @pytest.mark.parametrize("value", [7, {}, {"id": "orphan"}])
    def test_a_malformed_recipe_container_is_reported(self, value):
        doc = _doc()
        _section(doc, "plan")["recipes"] = value
        assert_diagnostic(_problems(doc), "section 'plan' 'recipes' must be a list")

    def test_a_nonobject_recipe_entry_is_reported(self):
        doc = _doc()
        _section(doc, "plan")["recipes"] = [["bad"]]
        assert_diagnostic(_problems(doc), "section 'plan' recipes must contain objects")

    @pytest.mark.parametrize("missing", [False, True])
    def test_missing_or_none_recipe_container_remains_optional(self, missing):
        doc = _doc()
        if missing:
            del _section(doc, "plan")["recipes"]
        else:
            _section(doc, "plan")["recipes"] = None
        assert _problems(doc) == []

    @pytest.mark.parametrize("recipe_id", [None, 7, []])
    def test_a_nonstring_recipe_id_is_reported(self, recipe_id):
        doc = _doc()
        recipe = _recipe("temporary", "plan")
        recipe["id"] = recipe_id
        _section(doc, "plan")["recipes"] = [recipe]
        assert_diagnostic(_problems(doc), "id must be lowercase letters, digits and '-'")


class TestDocumentLevelBites:
    """The checks above the rule loop: the header fields, the scenario enum itself, and each
    section's own shape."""

    def test_each_blank_top_level_field_is_reported(self):
        for key in ("guidance_id", "name", "title", "description", "summary"):
            assert_diagnostic(_problems(_doc(**{key: "   "})), f"top-level '{key}' must be a non-empty string")

    def test_a_document_declaring_no_scenario_is_reported(self):
        assert_diagnostic(_problems(_doc(scenarios=[])), "'scenarios' must declare at least one scenario")

    def test_a_blank_scenario_id_is_reported(self):
        assert_diagnostic(_problems(_doc(scenarios=["alpha", "  "])),
             "every entry in 'scenarios' must be a non-empty id string")

    def test_the_scenario_enum_must_be_sorted_and_free_of_duplicates(self):
        assert_diagnostic(_problems(_doc(scenarios=["beta", "alpha"])), "so routing is deterministic")
        assert_diagnostic(_problems(_doc(scenarios=["alpha", "alpha", "beta"])), "so routing is deterministic")

    def test_a_section_without_a_title_is_reported(self):
        doc = _doc()
        _section(doc, "sketch")["title"] = "  "
        assert_diagnostic(_problems(doc), "section 'sketch' needs a title")

    def test_a_section_with_no_rules_is_reported(self):
        doc = _doc()
        _section(doc, "model")["rules"] = []
        assert_diagnostic(_problems(doc), "section 'model' has no rules")


    @pytest.mark.parametrize("value", ["bad", 7])
    def test_malformed_sections_container_is_reported(self, value):
        doc = _doc(sections=value)
        assert_diagnostic(_problems(doc), "'sections' must be a list of section objects")

    def test_malformed_section_and_rule_entries_are_reported(self):
        doc = _doc(sections=["bad"])
        assert_diagnostic(_problems(doc), "each section must be an object")
        doc = _doc()
        _section(doc, "plan")["rules"] = ["bad"]
        assert_diagnostic(_problems(doc), "section 'plan' rules must contain objects")

    @pytest.mark.parametrize("section_id, value", [("plan", "bad"), ("kernel", 7)])
    def test_malformed_rules_container_is_reported(self, section_id, value):
        doc = _doc()
        _section(doc, section_id)["rules"] = value
        assert_diagnostic(_problems(doc), f"section '{section_id}' 'rules' must be a list")

    def test_malformed_scenario_and_rule_ids_are_reported(self):
        doc = _doc(scenarios=["alpha", ["bad"]])
        _section(doc, "plan")["rules"][0]["id"] = ["bad"]
        problems = _problems(doc)
        assert_diagnostic(problems, "every entry in 'scenarios' must be a non-empty id string")
        assert_diagnostic(problems, "id must be lowercase letters, digits and '-'")

    @pytest.mark.parametrize("section_id", ["plan", "kernel"])
    @pytest.mark.parametrize("value, message", [
        (7, "needs a non-empty 'scenarios' list"),
        (["alpha", {}], "'scenarios' entries must be non-empty id strings"),
    ])
    def test_malformed_rule_scenarios_are_reported(self, section_id, value, message):
        doc = _doc()
        _section(doc, section_id)["rules"][0]["scenarios"] = value
        assert_diagnostic(_problems(doc), f"rule '{section_id}-rule' {message}")


class TestSizeCapsBiteAtTheirBoundary:
    """A cap that fires one word early or one word late is the same defect as one that never
    fires, so each is probed AT the limit and one past it."""

    def _kernel_of(self, count):
        return [_rule(f"kernel-rule-{i}", gen_guidance.KERNEL) for i in range(count)]

    def _plan_of(self, count):
        return [_rule(f"plan-rule-{i}", "plan", scenarios=["alpha"]) for i in range(count)]

    def test_the_kernel_rule_count_cap(self):
        at_cap = _doc()
        _section(at_cap, gen_guidance.KERNEL)["rules"] = self._kernel_of(
            gen_guidance.KERNEL_MAX_RULES)
        assert _problems(at_cap) == []
        over = _doc()
        _section(over, gen_guidance.KERNEL)["rules"] = self._kernel_of(
            gen_guidance.KERNEL_MAX_RULES + 1)
        assert_diagnostic(_problems(over), f"the kernel holds {gen_guidance.KERNEL_MAX_RULES + 1} rules")

    def _kernel_padded(self, target):
        doc = _doc()
        return _pad(doc, _section(doc, gen_guidance.KERNEL)["rules"][0], "do",
                    _kernel_words, target)

    def _map_padded(self, target):
        # the section's use_when is a line of the MAP and of nothing else the caps measure.
        doc = _doc()
        return _pad(doc, _section(doc, "plan"), "use_when", _map_words, target)

    def _recipe_padded(self, target):
        doc = _doc()
        return _pad(doc, _section(doc, _RECIPE_SECTION)["recipes"][0], "use_when",
                    _recipe_words, target)

    def test_the_kernel_word_cap(self):
        assert _problems(self._kernel_padded(gen_guidance.KERNEL_MAX_WORDS)) == []
        over = self._kernel_padded(gen_guidance.KERNEL_MAX_WORDS + 1)
        assert_diagnostic(_problems(over), f"the kernel renders {gen_guidance.KERNEL_MAX_WORDS + 1} words")

    def test_the_skill_map_word_cap(self):
        assert _problems(self._map_padded(gen_guidance.SKILL_MAP_MAX_WORDS)) == []
        over = self._map_padded(gen_guidance.SKILL_MAP_MAX_WORDS + 1)
        assert_diagnostic(_problems(over), f"the skill map renders {gen_guidance.SKILL_MAP_MAX_WORDS + 1} words")

    def test_the_recipe_word_cap(self):
        assert _problems(self._recipe_padded(gen_guidance.RECIPE_MAX_WORDS)) == []
        over = self._recipe_padded(gen_guidance.RECIPE_MAX_WORDS + 1)
        assert_diagnostic(_problems(over),
             f"recipe '{_POINTED_AT}' renders {gen_guidance.RECIPE_MAX_WORDS + 1} words")

    def test_the_recipes_per_section_cap(self):
        at_cap = _doc()
        _section(at_cap, _RECIPE_SECTION)["recipes"] = (
            [_recipe(_POINTED_AT, _RECIPE_SECTION)]
            + [_recipe(f"extra-{i}", _RECIPE_SECTION)
               for i in range(gen_guidance.MAX_RECIPES_PER_SECTION - 1)])
        assert _problems(at_cap) == []
        over = copy.deepcopy(at_cap)
        _section(over, _RECIPE_SECTION)["recipes"].append(_recipe("one-more", _RECIPE_SECTION))
        assert_diagnostic(_problems(over), f"section '{_RECIPE_SECTION}' holds "
                              f"{gen_guidance.MAX_RECIPES_PER_SECTION + 1} recipes")

    def test_the_recipe_step_count_bounds(self):
        step = {"tool": "doc_get", "do": "read it", "read_back": "what it said"}
        for count in (gen_guidance.MIN_RECIPE_STEPS, gen_guidance.MAX_RECIPE_STEPS):
            doc = _doc()
            _section(doc, _RECIPE_SECTION)["recipes"][0]["steps"] = [dict(step)] * count
            assert _problems(doc) == [], count
        for count in (gen_guidance.MIN_RECIPE_STEPS - 1, gen_guidance.MAX_RECIPE_STEPS + 1):
            doc = _doc()
            _section(doc, _RECIPE_SECTION)["recipes"][0]["steps"] = [dict(step)] * count
            assert_diagnostic(_problems(doc), f"holds {count} steps")

    def test_the_per_section_rule_cap(self):
        # the cap is on ANY section, not just the kernel: 'plan' carries the probe so the kernel's
        # own (lower) rule cap cannot be what fires.
        at_cap = _doc()
        _section(at_cap, "plan")["rules"] = self._plan_of(gen_guidance.MAX_SECTION_RULES)
        assert _problems(at_cap) == []
        over = _doc()
        _section(over, "plan")["rules"] = self._plan_of(gen_guidance.MAX_SECTION_RULES + 1)
        assert_diagnostic(_problems(over),
             f"section 'plan' holds {gen_guidance.MAX_SECTION_RULES + 1} rules")

    def test_examples_are_free(self):
        # examples carry no per-section cap: what a playbook is bounded by is its rule count and
        # its recipes, and a rule that earns an example is not competing with its neighbour.
        doc = _doc()
        for rule in _section(doc, "plan")["rules"]:
            rule["example"] = "a worked case"
        _section(doc, "plan")["rules"].append(
            _rule("plan-rule-two", "plan", scenarios=["alpha"], example="a second case"))
        assert _problems(doc) == []


class TestTheSectionCapIsOneNumberOnBothSides:
    """The authoring gate and the serving cap are the same number, asserted by driving BOTH: the
    largest section validate accepts comes back whole from sys_get_guidance, and the first section
    it refuses is the first one that truncates. A gate looser than the cap would ship rules no
    section read answers with; a gate tighter than it would refuse a document the tool serves fine."""

    def _plan_rules(self, count):
        return [_rule(f"plan-rule-{i}", "plan", scenarios=["alpha"]) for i in range(count)]

    def _served(self, monkeypatch, doc):
        tool = load_tool("sys_get_guidance")
        monkeypatch.setattr(tool.loader, "load", lambda path=None: (doc, "0" * 64))
        result = tool.handler(section="plan")
        assert result["isError"] is False, result
        return json.loads(result["content"][0]["text"])

    def test_the_largest_section_the_gate_accepts_is_served_whole(self, monkeypatch):
        doc = _doc()
        _section(doc, "plan")["rules"] = self._plan_rules(gen_guidance.MAX_SECTION_RULES)
        assert _problems(doc) == []
        out = self._served(monkeypatch, doc)
        assert out["rule_count"] == gen_guidance.MAX_SECTION_RULES
        assert "truncated" not in out

    def test_the_first_section_the_gate_refuses_is_the_first_one_that_truncates(self, monkeypatch):
        doc = _doc()
        _section(doc, "plan")["rules"] = self._plan_rules(gen_guidance.MAX_SECTION_RULES + 1)
        assert_diagnostic(_problems(doc), "section 'plan' holds")
        out = self._served(monkeypatch, doc)
        assert out["truncated"] is True
        assert out["rule_total"] == gen_guidance.MAX_SECTION_RULES + 1


# ── scenario routing ──────────────────────────────────────────────────────────

# Rules that CANNOT apply to a scenario, one entry per declared scenario: a mechanism practice has
# nothing to say about a single part, an audit of an imported body authors no sketch, pattern or
# configuration, a fixed mechanism is one built assembly rather than a variant table, and a
# configurable template is authored as configurations rather than as a mechanism to drive. A rule
# that declared one of these would route guidance into a case it does not fit, which is a data
# defect no prose check could see. Each entry is a judgment about applicability, so it names the
# rules that must never route here - not every rule that happens not to today.
_INAPPLICABLE = {
    "configurable_template": ("connected-reference-path", "exercise-the-mechanism",
                              "as-built-for-parts-modelled-in-place"),
    "fixed_mechanism": ("variants-are-configurations",),
    "simple_part": ("connected-reference-path", "exercise-the-mechanism",
                    "variants-are-configurations"),
    "parametric_family": ("connected-reference-path", "exercise-the-mechanism"),
    "floating_mechanism": ("connected-reference-path",),
    "imported_audit": ("construction-carries-symmetry-and-spacing", "pattern-only-identical-intent",
                       "let-the-process-shape-the-part", "name-parts-for-what-they-are",
                       "variants-are-configurations"),
    # a machining job runs on a part that already exists: it authors no sketch and no variant table
    "machining_job": ("construction-carries-symmetry-and-spacing", "anchor-to-the-origin-by-relation",
                      "variants-are-configurations"),
    # a surfaced product is one body's shape, not a mechanism to drive or a variant table
    "surfaced_product": ("connected-reference-path", "exercise-the-mechanism",
                         "variants-are-configurations"),
}


class TestScenarioRouting:
    def test_every_declared_scenario_routes_to_the_whole_kernel(self, shipped):
        kernel_ids = {r["id"] for r in _section(shipped, gen_guidance.KERNEL)["rules"]}
        for scenario in gen_guidance.scenario_ids(shipped):
            reached = {r["id"] for r in gen_guidance.rules_for(shipped, scenario)}
            assert kernel_ids <= reached, scenario

    def test_no_rule_reaches_a_scenario_it_cannot_apply_to(self, shipped):
        for scenario, excluded in _INAPPLICABLE.items():
            reached = {r["id"] for r in gen_guidance.rules_for(shipped, scenario)}
            assert reached, f"{scenario} routes to nothing"
            assert not reached & set(excluded), scenario

    def test_the_exclusions_name_rules_that_exist(self, shipped):
        known = {r["id"] for r in gen_guidance.rules(shipped)}
        named = {rid for ids in _INAPPLICABLE.values() for rid in ids}
        assert named <= known, sorted(named - known)

    def test_every_declared_scenario_carries_a_negative_oracle(self, shipped):
        # EQUALITY, not containment: a scenario with no entry has no negative oracle at all, so
        # handing it a rule it cannot apply to leaves validate clean, the render byte-identical and
        # the suite green - the routing would be silently mutable.
        assert set(_INAPPLICABLE) == set(gen_guidance.scenario_ids(shipped))

    def test_routing_follows_the_declaration_and_nothing_else(self):
        doc = _doc()
        assert [r["id"] for r in gen_guidance.rules_for(doc, "beta")] == ["kernel-rule"]
        _section(doc, "model")["rules"][0]["scenarios"] = ["alpha", "beta"]
        assert [r["id"] for r in gen_guidance.rules_for(doc, "beta")] == ["kernel-rule", "model-rule"]

    def test_an_undeclared_scenario_routes_to_nothing(self):
        assert gen_guidance.rules_for(_doc(), "gamma") == []


# ── the generator's own --check contract ──────────────────────────────────────

class TestCheckDetectsAStalePackage:
    def _run(self, monkeypatch, argv):
        monkeypatch.setattr(gen_guidance.sys, "argv", ["gen_guidance.py"] + argv)
        return gen_guidance.main()

    def _elsewhere(self, monkeypatch, tmp_path):
        """Point the whole package at an empty directory, so nothing committed is touched."""
        monkeypatch.setattr(gen_guidance, "SKILL_PATH", str(tmp_path / "SKILL.md"))
        monkeypatch.setattr(gen_guidance, "PLAYBOOK_DIR", str(tmp_path / "playbooks"))

    def test_check_passes_on_the_committed_package(self, monkeypatch, capsys):
        assert self._run(monkeypatch, ["--check"]) == 0
        assert "up to date" in capsys.readouterr().out

    def test_check_reports_a_stale_skill_and_writes_nothing(self, monkeypatch, tmp_path, capsys):
        stale = tmp_path / "SKILL.md"
        stale.write_text("---\nname: parametric-cad-design\n---\n\n# Stale\n", encoding="utf-8")
        monkeypatch.setattr(gen_guidance, "SKILL_PATH", str(stale))
        assert self._run(monkeypatch, ["--check"]) == 1
        assert "Stale" in capsys.readouterr().err
        assert stale.read_text(encoding="utf-8").endswith("# Stale\n")

    def test_check_reports_a_stale_playbook(self, monkeypatch, tmp_path, capsys):
        # the playbook files are committed artifacts too: an agent reads one off disk without
        # calling the server, so a stale one is guidance the document no longer carries.
        self._elsewhere(monkeypatch, tmp_path)
        assert self._run(monkeypatch, []) == 0
        target = gen_guidance.playbook_path("plan")
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("## Stale plan\n")
        assert self._run(monkeypatch, ["--check"]) == 1
        assert "plan.md" in capsys.readouterr().err

    def test_check_reports_a_playbook_the_document_does_not_declare(self, monkeypatch, tmp_path,
                                                                    capsys):
        self._elsewhere(monkeypatch, tmp_path)
        assert self._run(monkeypatch, []) == 0
        stray = os.path.join(gen_guidance.PLAYBOOK_DIR, "retired.md")
        with open(stray, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("## Retired\n")
        assert self._run(monkeypatch, ["--check"]) == 1
        assert "retired.md" in capsys.readouterr().err

    def test_write_reports_a_playbook_the_document_does_not_declare(self, monkeypatch, tmp_path,
                                                                     capsys):
        self._elsewhere(monkeypatch, tmp_path)
        assert self._run(monkeypatch, []) == 0
        stray = os.path.join(gen_guidance.PLAYBOOK_DIR, "retired.md")
        with open(stray, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("## Retired\n")
        assert self._run(monkeypatch, []) == 1
        assert "retired.md" in capsys.readouterr().err
        with open(stray, encoding="utf-8") as fh:
            assert fh.read() == "## Retired\n"

    def test_check_reports_a_missing_skill(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gen_guidance, "SKILL_PATH", str(tmp_path / "absent.md"))
        assert self._run(monkeypatch, ["--check"]) == 1

    def test_invalid_data_fails_before_anything_is_written(self, monkeypatch, tmp_path, capsys):
        self._elsewhere(monkeypatch, tmp_path)
        broken = _doc()
        _section(broken, "plan")["rules"][0]["prove"][0]["tool"] = "no_such_tool"
        monkeypatch.setattr(gen_guidance, "load", lambda *a, **k: broken)
        assert self._run(monkeypatch, []) == 1
        assert "no_such_tool" in capsys.readouterr().err
        assert not os.path.exists(gen_guidance.SKILL_PATH)
        assert not os.path.isdir(gen_guidance.PLAYBOOK_DIR)

    def test_generating_writes_every_file_and_then_checks_clean(self, monkeypatch, tmp_path):
        self._elsewhere(monkeypatch, tmp_path)
        assert self._run(monkeypatch, []) == 0
        doc = gen_guidance.load()
        for path, text in gen_guidance.package(doc).items():
            with open(path, encoding="utf-8") as fh:
                assert fh.read() == text, path
        assert self._run(monkeypatch, ["--check"]) == 0
