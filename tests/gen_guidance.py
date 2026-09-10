# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Generate the Claude skill package from the CANONICAL guidance JSON.

``commands/mcpServer/guidance/parametric_cad_design.json`` is the one authored source of CAD
design practice. A rule carries an ``id``, its ``section``, the ``scenarios`` it applies to, and
``when`` / ``do`` / ``except`` / ``prove``; a recipe carries ordered ``steps`` and the ``bar`` that
says what done looks like. This script validates that data against the LIVE tool registry and
writes the package from it: ``SKILL.md`` is the MAP (the kernel whole, then where each playbook and
recipe is fetched), and ``playbooks/<section>.md`` is one section's rules and recipes.

Every file is rendered by the shipped package (``render``), the same functions the server serves
its resources through, so the committed skill and the served guidance are one render.

The structural limits are here, not in a lint: the kernel at five rules and 300 rendered words, one
recipe at 260, a section at six recipes, the map at 900 words excluding frontmatter. The per-section
rule cap is the exception - it is the shipped ``loader.MAX_SECTION_RULES``, imported rather than
restated, because it is also the number ``sys_get_guidance`` truncates one section read at.
Validation is authoring-time gating - it decides what may be committed, never what the server
answers. Whether a rule's wording is honest is a review judgment; this checks shape, routing, size.

Run from the repo root:

    py -3 tests/gen_guidance.py          # writes .claude/skills/parametric-cad-design/
    py -3 tests/gen_guidance.py --check  # exit 1 if the package is stale or the JSON is invalid
"""

import argparse
import json
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# gen_manifest owns the registry walk (collect()) and installs the mocked adsk at its module top;
# reuse it rather than rolling a second walk that could disagree about what is registered. It also
# puts commands/ on sys.path, which is what makes the shipped guidance package importable below.
import gen_manifest  # noqa: E402

# The shipped package renders the body and declares the section order - what holds everywhere, then
# the build in the order it happens. A document declaring anything else is rejected below rather
# than reordered, so the skill is a function of the file alone; importing the render (instead of
# holding a second copy) is what keeps the skill and the served resource one text.
from mcpServer.guidance.loader import KERNEL, MAX_SECTION_RULES, SECTION_IDS, recipes  # noqa: E402
from mcpServer.guidance.render import (SAFETY_INVARIANT, body, map_text,  # noqa: E402
                                       recipe_text, section_text)
# The pointer cam_get's strategies note publishes: validated below against the document, so the
# tool cannot name a recipe that was renamed away.
from mcpServer.guidance.loader import STRATEGY_RECIPE_ID  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDANCE_PATH = os.path.join(REPO_ROOT, "commands", "mcpServer", "guidance",
                             "parametric_cad_design.json")
SKILL_DIR = os.path.join(REPO_ROOT, ".claude", "skills", "parametric-cad-design")
SKILL_PATH = os.path.join(SKILL_DIR, "SKILL.md")
PLAYBOOK_DIR = os.path.join(SKILL_DIR, "playbooks")

RULE_FIELDS = ("id", "section", "scenarios", "when", "do", "except", "prove")
RECIPE_FIELDS = ("id", "section", "scenarios", "title", "use_when", "steps", "bar")
EXEMPLAR_FIELDS = ("document", "urn", "look_at", "access")

# A Fusion data-model LINEAGE urn - the address that survives a new version of the document. A
# version urn (dm.file) names one version and goes stale, so an exemplar must not carry one.
LINEAGE_PREFIX = "urn:adsk.wipprod:dm.lineage:"

KERNEL_MAX_RULES = 5
KERNEL_MAX_WORDS = 300
SKILL_MAP_MAX_WORDS = 900
MAX_RECIPES_PER_SECTION = 6
RECIPE_MAX_WORDS = 260
MIN_RECIPE_STEPS = 3
MAX_RECIPE_STEPS = 12

_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_WRAP_WIDTH = 96


# ── reading the canonical data ────────────────────────────────────────────────

def load(path=GUIDANCE_PATH):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def rules(doc):
    """Every rule, in the document's own section-then-authored order."""
    return [r for sec in (doc.get("sections") or []) for r in (sec.get("rules") or [])]


def scenario_ids(doc):
    """The closed scenario enum: the cases a rule may declare it applies to."""
    return list(doc.get("scenarios") or [])


def rules_for(doc, scenario):
    """The rules that DECLARE `scenario` - the routing a client filters by. A rule reaches a
    scenario only by naming it, so an inapplicable rule is one that named a case it does not fit,
    which is a data defect, not something to infer from the prose."""
    return [r for r in rules(doc) if scenario in (r.get("scenarios") or [])]


def recipes_for(doc, scenario):
    """The recipes that DECLARE `scenario` - the same routing rule, over the other record kind."""
    return [r for r in recipes(doc) if scenario in (r.get("scenarios") or [])]


# ── rendering: the frontmatter this script adds to the shipped render ─────────

def word_count(text):
    """Rendered words - what the size limits are stated in."""
    return len(text.split())


def frontmatter(doc):
    """The client loader's own header: the name it is invoked by and the description telling an
    agent WHEN to reach for it. Excluded from the word limit - it is not guidance."""
    lines = ["---", f"name: {doc['name']}", "description: >-"]
    lines += textwrap.wrap(doc["description"], width=_WRAP_WIDTH,
                           initial_indent="  ", subsequent_indent="  ")
    lines.append("---")
    return "\n".join(lines) + "\n"


def render(doc):
    """SKILL.md: the loader's frontmatter over the MAP - never the whole document."""
    return frontmatter(doc) + "\n" + map_text(doc)


def playbook_path(section_id):
    """Where one section's playbook file lives in the committed package."""
    return os.path.join(PLAYBOOK_DIR, section_id + ".md")


def package(doc):
    """{path: text} for every file of the committed skill package - the map, then one playbook per
    non-kernel section (the kernel is IN the map, so it has no file of its own)."""
    files = {SKILL_PATH: render(doc)}
    for section_id in SECTION_IDS:
        if section_id != KERNEL:
            files[playbook_path(section_id)] = section_text(doc, section_id)
    return files


def stale_paths(doc):
    """Every package file whose committed bytes are not what the document renders, plus every file
    under playbooks/ the document does not declare - a section that was renamed away leaves one."""
    files = package(doc)
    stale = []
    for path, text in sorted(files.items()):
        existing = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read()
        if existing != text:
            stale.append(path)
    if os.path.isdir(PLAYBOOK_DIR):
        for name in sorted(os.listdir(PLAYBOOK_DIR)):
            path = os.path.join(PLAYBOOK_DIR, name)
            if os.path.isfile(path) and path not in files:
                stale.append(path)
    return stale


def write_package(doc):
    """Write every declared file, and return the undeclared playbook files left behind."""
    files = package(doc)
    os.makedirs(PLAYBOOK_DIR, exist_ok=True)
    for path, text in sorted(files.items()):
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return [p for p in stale_paths(doc) if p not in files]


# ── validation ────────────────────────────────────────────────────────────────

def registered_tool_names():
    """Every tool name the live registry carries - the inventory a prove step must name."""
    return frozenset(t["name"] for t in gen_manifest.collect()["tools"])


def _non_ascii_problems(doc):
    """Every string carrying a character that would cross the wire as a \\uXXXX escape."""
    problems = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        elif isinstance(node, str):
            bad = [hex(ord(c)) for c in node if ord(c) > 127]
            if bad:
                problems.append(f"{path} carries non-ASCII {bad} - use a plain-ASCII spelling "
                                "(' - ', '...', '->')")

    walk(doc, "guidance")
    return problems


def _scenario_problems(declared, label, known_scenarios):
    """The routing declaration's own problems - one rule for both record kinds, so a recipe cannot
    route by a laxer standard than a rule."""
    if not (isinstance(declared, list) and declared):
        return [f"{label} needs a non-empty 'scenarios' list"]
    problems = []
    valid = [s for s in declared if isinstance(s, str) and s.strip()]
    if len(valid) != len(declared):
        problems.append(f"{label} 'scenarios' entries must be non-empty id strings")
    unknown = sorted(s for s in valid if s not in known_scenarios)
    if unknown:
        problems.append(f"{label} declares unknown scenario(s) {unknown}")
    if valid != sorted(set(valid)):
        problems.append(f"{label} 'scenarios' must be sorted and carry no duplicate")
    return problems


def _rule_problems(rule, section_id, known_scenarios, tool_names):
    problems = []
    rule_id = rule.get("id")
    label = f"rule '{rule_id}'" if isinstance(rule_id, str) else f"a rule in section '{section_id}'"

    for field in RULE_FIELDS:
        if field not in rule:
            problems.append(f"{label} is missing '{field}'")

    if "id" in rule and (not isinstance(rule_id, str) or not rule_id or
                         set(rule_id) - _ID_CHARS):
        problems.append(f"{label} id must be lowercase letters, digits and '-'")

    for field in ("when", "do", "except"):
        if field in rule and not (isinstance(rule[field], str) and rule[field].strip()):
            problems.append(f"{label} '{field}' must be a non-empty string")

    if "section" in rule and rule["section"] != section_id:
        problems.append(f"{label} declares section '{rule['section']}' but sits in '{section_id}'")

    if "scenarios" in rule:
        problems += _scenario_problems(rule["scenarios"], label, known_scenarios)

    if "prove" in rule:
        prove = rule["prove"]
        if not (isinstance(prove, list) and prove):
            problems.append(f"{label} needs at least one 'prove' step")
        else:
            for step in prove:
                if not isinstance(step, dict):
                    problems.append(f"{label} prove step must carry 'tool' and 'observe'")
                    continue
                tool, observe = step.get("tool"), step.get("observe")
                if not (isinstance(observe, str) and observe.strip()):
                    problems.append(f"{label} prove step '{tool}' needs a non-empty 'observe'")
                if tool not in tool_names:
                    problems.append(f"{label} proves through '{tool}', which is not a registered "
                                    "tool - a client told to call it gets nothing")

    kind = rule.get("kind")
    if kind is not None and kind != SAFETY_INVARIANT:
        problems.append(f"{label} declares kind '{kind}' - the only kind is '{SAFETY_INVARIANT}'")

    example = rule.get("example")
    if example is not None and not (isinstance(example, str) and example.strip()):
        problems.append(f"{label} 'example' must be a non-empty string when present")

    return problems


def _step_problems(steps, label, tool_names):
    problems = []
    if not isinstance(steps, list):
        return [f"{label} 'steps' must be an ordered list of {MIN_RECIPE_STEPS} to "
                f"{MAX_RECIPE_STEPS} steps"]
    if not MIN_RECIPE_STEPS <= len(steps) <= MAX_RECIPE_STEPS:
        problems.append(f"{label} holds {len(steps)} steps - a recipe carries {MIN_RECIPE_STEPS} "
                        f"to {MAX_RECIPE_STEPS}")
    for step in steps:
        if not isinstance(step, dict):
            problems.append(f"{label} step must carry 'tool', 'do' and 'read_back'")
            continue
        tool = step.get("tool")
        for field in ("do", "read_back"):
            if not (isinstance(step.get(field), str) and step[field].strip()):
                problems.append(f"{label} step '{tool}' needs a non-empty '{field}'")
        if tool not in tool_names:
            problems.append(f"{label} steps through '{tool}', which is not a registered tool - a "
                            "client told to call it gets nothing")
    return problems


def _exemplar_problems(exemplar, label):
    if not isinstance(exemplar, dict):
        return [f"{label} 'exemplar' must carry {list(EXEMPLAR_FIELDS)}"]
    problems = []
    for field in EXEMPLAR_FIELDS:
        if not (isinstance(exemplar.get(field), str) and exemplar[field].strip()):
            problems.append(f"{label} 'exemplar.{field}' must be a non-empty string")
    urn = exemplar.get("urn")
    if isinstance(urn, str) and urn.strip() and not urn.startswith(LINEAGE_PREFIX):
        problems.append(f"{label} exemplar urn '{urn}' does not start with '{LINEAGE_PREFIX}' - a "
                        "lineage urn is the address that survives a new version")
    return problems


def _recipe_problems(recipe, section_id, known_scenarios, tool_names):
    problems = []
    recipe_id = recipe.get("id")
    label = (f"recipe '{recipe_id}'" if isinstance(recipe_id, str)
             else f"a recipe in section '{section_id}'")

    for field in RECIPE_FIELDS:
        if field not in recipe:
            problems.append(f"{label} is missing '{field}'")

    if "id" in recipe and (not isinstance(recipe_id, str) or not recipe_id or
                           set(recipe_id) - _ID_CHARS):
        problems.append(f"{label} id must be lowercase letters, digits and '-'")

    for field in ("title", "use_when"):
        if field in recipe and not (isinstance(recipe[field], str) and recipe[field].strip()):
            problems.append(f"{label} '{field}' must be a non-empty string")

    if "section" in recipe and recipe["section"] != section_id:
        problems.append(f"{label} declares section '{recipe['section']}' but sits in "
                        f"'{section_id}'")

    if "scenarios" in recipe:
        problems += _scenario_problems(recipe["scenarios"], label, known_scenarios)

    if "steps" in recipe:
        problems += _step_problems(recipe["steps"], label, tool_names)

    if "bar" in recipe:
        bar = recipe["bar"]
        if not isinstance(bar, dict):
            problems.append(f"{label} 'bar' must carry 'measure' and 'eyes'")
        else:
            for field in ("measure", "eyes"):
                if not (isinstance(bar.get(field), str) and bar[field].strip()):
                    problems.append(f"{label} 'bar.{field}' must be a non-empty string")

    if recipe.get("exemplar") is not None:
        problems += _exemplar_problems(recipe["exemplar"], label)

    return problems


def validate(doc, tool_names):
    """Every problem with `doc`, one line each. An empty list means the data is renderable and
    every claim it makes about the tool surface resolves."""
    problems = []

    for key in ("guidance_id", "name", "title", "description", "summary"):
        if not (isinstance(doc.get(key), str) and doc[key].strip()):
            problems.append(f"top-level '{key}' must be a non-empty string")

    raw_scenarios = doc.get("scenarios")
    declared = scenario_ids(doc) if isinstance(raw_scenarios, list) else []
    if raw_scenarios is not None and not isinstance(raw_scenarios, list):
        problems.append("'scenarios' must be a list of id strings")
    if not declared:
        problems.append("'scenarios' must declare at least one scenario")
    if any(not (isinstance(s, str) and s.strip()) for s in declared):
        problems.append("every entry in 'scenarios' must be a non-empty id string")
    elif declared != sorted(set(declared)):
        problems.append("'scenarios' must be sorted and carry no duplicate, so routing is "
                        "deterministic")
    known = {s for s in declared if isinstance(s, str)}

    raw_sections = doc.get("sections")
    sections = raw_sections if isinstance(raw_sections, list) else []
    if raw_sections is not None and not isinstance(raw_sections, list):
        problems.append("'sections' must be a list of section objects")
    present = [s.get("id") if isinstance(s, dict) else None for s in sections]
    if present != list(SECTION_IDS):
        problems.append(f"sections must be exactly {list(SECTION_IDS)} in that order, got {present}")

    owner = {}
    recipe_owner = {}
    routed = set()
    for sec in sections:
        if not isinstance(sec, dict):
            problems.append("each section must be an object")
            continue
        section_id = sec.get("id")
        if not (isinstance(sec.get("title"), str) and sec["title"].strip()):
            problems.append(f"section '{section_id}' needs a title")
        if not (isinstance(sec.get("use_when"), str) and sec["use_when"].strip()):
            problems.append(f"section '{section_id}' needs a 'use_when' line saying when an agent "
                            "should read it")
        raw_rules = sec.get("rules")
        section_rules = raw_rules if isinstance(raw_rules, list) else []
        if raw_rules is not None and not isinstance(raw_rules, list):
            problems.append(f"section '{section_id}' 'rules' must be a list of rule objects")
        if not section_rules:
            problems.append(f"section '{section_id}' has no rules")
        if len(section_rules) > MAX_SECTION_RULES:
            # The serving cap, asked of the DATA: one sys_get_guidance call returns at most this
            # many rules, so a section authored past it would ship rules no section read answers.
            problems.append(f"section '{section_id}' holds {len(section_rules)} rules - at most "
                            f"{MAX_SECTION_RULES}, which is all one sys_get_guidance call serves")
        for rule in section_rules:
            if not isinstance(rule, dict):
                problems.append(f"section '{section_id}' rules must contain objects")
                continue
            problems += _rule_problems(rule, section_id, known, tool_names)
            rule_id = rule.get("id")
            if isinstance(rule_id, str):
                if rule_id in owner:
                    problems.append(f"duplicate rule id '{rule_id}' - it is a member of both "
                                    f"'{owner[rule_id]}' and '{section_id}'")
                elif rule_id in recipe_owner:
                    problems.append(f"rule id '{rule_id}' is already a recipe id in "
                                    f"'{recipe_owner[rule_id]}' - one id names one record")
                else:
                    owner[rule_id] = section_id
            rule_scenarios = rule.get("scenarios")
            if isinstance(rule_scenarios, list):
                routed.update(s for s in rule_scenarios if isinstance(s, str))

        raw_recipes = sec.get("recipes")
        if raw_recipes is None:
            section_recipes = []
        elif not isinstance(raw_recipes, list):
            problems.append(f"section '{section_id}' 'recipes' must be a list of recipe objects")
            section_recipes = []
        else:
            section_recipes = raw_recipes
        if section_id == KERNEL and section_recipes:
            problems.append("the kernel carries no recipes - it is what holds in every case, and "
                            "a recipe is a sequence for one situation")
        if len(section_recipes) > MAX_RECIPES_PER_SECTION:
            problems.append(f"section '{section_id}' holds {len(section_recipes)} recipes - at "
                            f"most {MAX_RECIPES_PER_SECTION}")
        for recipe in section_recipes:
            if not isinstance(recipe, dict):
                problems.append(f"section '{section_id}' recipes must contain objects")
                continue
            problems += _recipe_problems(recipe, section_id, known, tool_names)
            recipe_id = recipe.get("id")
            if isinstance(recipe_id, str):
                if recipe_id in recipe_owner:
                    problems.append(f"duplicate recipe id '{recipe_id}' - it is a member of both "
                                    f"'{recipe_owner[recipe_id]}' and '{section_id}'")
                elif recipe_id in owner:
                    problems.append(f"recipe id '{recipe_id}' is already a rule id in "
                                    f"'{owner[recipe_id]}' - one id names one record")
                else:
                    recipe_owner[recipe_id] = section_id
            recipe_scenarios = recipe.get("scenarios")
            if isinstance(recipe_scenarios, list):
                routed.update(s for s in recipe_scenarios if isinstance(s, str))

    for sec in sections:
        if not isinstance(sec, dict) or sec.get("id") != KERNEL:
            continue
        kernel_rules = sec.get("rules")
        if not isinstance(kernel_rules, list):
            continue
        if len(kernel_rules) > KERNEL_MAX_RULES:
            problems.append(f"the kernel holds {len(kernel_rules)} rules - at most "
                            f"{KERNEL_MAX_RULES}")
        for rule in kernel_rules:
            if not isinstance(rule, dict):
                continue
            rule_scenarios = rule.get("scenarios")
            if (isinstance(rule_scenarios, list)
                    and {s for s in rule_scenarios if isinstance(s, str)} != known):
                problems.append(f"kernel rule '{rule.get('id')}' must declare every scenario - "
                                "a rule that does not hold everywhere is a playbook rule")
            if rule.get("example"):
                problems.append(f"kernel rule '{rule.get('id')}' carries an example - examples "
                                "belong to the conditional playbooks")

    for scenario in sorted(known - routed):
        problems.append(f"scenario '{scenario}' is declared but no rule or recipe applies to it")

    if STRATEGY_RECIPE_ID not in recipe_owner:
        problems.append(f"cam_get's strategies note points at recipe '{STRATEGY_RECIPE_ID}' "
                        "(loader.STRATEGY_RECIPE_ID), which this document does not "
                        "declare - the pointer would dangle")

    problems += _non_ascii_problems(doc)

    # The size limits are measured on the RENDER, so they are asked only once the data renders.
    if not problems:
        kernel_words = word_count(section_text(doc, KERNEL))
        if kernel_words > KERNEL_MAX_WORDS:
            problems.append(f"the kernel renders {kernel_words} words - at most {KERNEL_MAX_WORDS}")
        map_words = word_count(map_text(doc))
        if map_words > SKILL_MAP_MAX_WORDS:
            problems.append(f"the skill map renders {map_words} words excluding frontmatter - at "
                            f"most {SKILL_MAP_MAX_WORDS}")
        for recipe in recipes(doc):
            recipe_words = word_count(recipe_text(doc, recipe["id"]))
            if recipe_words > RECIPE_MAX_WORDS:
                problems.append(f"recipe '{recipe['id']}' renders {recipe_words} words - at most "
                                f"{RECIPE_MAX_WORDS}")

    return problems


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 if the skill package is stale or the JSON is invalid (no write).")
    args = parser.parse_args()

    doc = load()
    problems = validate(doc, registered_tool_names())
    if problems:
        print("commands/mcpServer/guidance/parametric_cad_design.json is invalid:\n  "
              + "\n  ".join(problems), file=sys.stderr)
        return 1

    if args.check:
        stale = stale_paths(doc)
        if stale:
            print("Stale - run `py -3 tests/gen_guidance.py` and commit (delete any file it does "
                  "not write):\n  " + "\n  ".join(os.path.relpath(p, REPO_ROOT) for p in stale),
                  file=sys.stderr)
            return 1
        print("The parametric-cad-design skill package is up to date.")
        return 0

    undeclared = write_package(doc)
    print(f"Wrote {SKILL_PATH} ({word_count(map_text(doc))} map words, kernel "
          f"{word_count(section_text(doc, KERNEL))}) and "
          f"{len(package(doc)) - 1} playbook(s).")
    if undeclared:
        print("Undeclared playbook file(s) left in place - delete them:\n  "
              + "\n  ".join(os.path.relpath(p, REPO_ROOT) for p in undeclared), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
