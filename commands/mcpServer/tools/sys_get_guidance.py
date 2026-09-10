# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""sys_get_guidance - the server's packaged CAD design guidance: the index, one section, or one
recipe per call. Static content read through ``..guidance.loader``; nothing here touches Fusion."""

from ._common import ok, error
from . import _inputs
from ..guidance import loader
from ..guidance import resources
from ..mcp_primitives.tool import Tool
from ..mcp_primitives.item import Item
from ..mcp_primitives.registry import register

_SECTION = _inputs.Choice(
    "section", loader.SECTION_IDS,
    description="Omit for the index.")

_RECIPE = _inputs.Choice("recipe", loader.RECIPE_IDS)

INDEX_NOTE = (
    "The one design-guidance document this server packages. Call again with section=<id> for a "
    "section's rules, or recipe=<id> for one recipe - one per call. 'recipes' maps every recipe it "
    "carries; 'sha256' is the content hash of what was served, and each rule and recipe declares "
    "which of 'scenarios' it applies to. 'resource_uri' is where the same guidance is served whole "
    "over MCP's resource channel.")

SECTION_NOTE = (
    "Each rule is a record: 'when' the condition, 'do' the practice, 'except' where it does not "
    "apply, 'prove' the tool to read the result back through and what to observe, 'scenarios' the "
    "cases it declares for. 'kind' marks a safety invariant; a rule without it is strategy. "
    "'recipe_index' names this section's recipes - recipe=<id> returns one whole. 'next_sections' "
    "names what is left to ask for.")

RECIPE_NOTE = (
    "The steps are ORDERED, and each 'read_back' is the observation to make before the next step - "
    "a step whose read_back does not answer is where to stop, not to push past. 'bar' is what done "
    "looks like: the measure that must hold and what a screenshot must show. 'exemplar', when "
    "present, is a document to open and X-ray.")

TRUNCATED_NOTE = (
    " This section holds more rules than one call returns: 'rule_count' of 'rule_total' are in "
    "'rules' and the rest are not here. Read the document at 'resource_uri' over the resource "
    "channel for all of them - it is rendered whole, with no per-section cap.")


def handler(section=None, recipe=None) -> dict:
    """See TOOL_DESCRIPTION."""
    wanted, refusal = _SECTION.resolve(section)
    if refusal:
        return error(refusal)
    wanted_recipe, refusal = _RECIPE.resolve(recipe)
    if refusal:
        return error(refusal)
    if wanted and wanted_recipe:
        return error(f"Ask for one or the other: section='{wanted}' returns that section's rules, "
                     f"recipe='{wanted_recipe}' returns that one recipe. Call twice.")

    try:
        doc, sha256 = loader.load()
    except loader.GuidanceUnavailable as exc:
        return error(str(exc))

    ids = loader.section_ids(doc)
    result = {"guidance_id": doc.get("guidance_id"), "title": doc.get("title"), "sha256": sha256,
              "resource_uri": resources.uri_for(doc.get("guidance_id"))}

    if wanted_recipe:
        rec = loader.find_recipe(doc, wanted_recipe)
        if rec is None:
            carried = [r.get("id") for r in loader.recipes(doc)]
            return error(f"The packaged guidance document carries no recipe '{wanted_recipe}'. It "
                         "carries: " + (", ".join(str(i) for i in carried) or "none") + ".")
        result.update({"recipe": rec, "note": RECIPE_NOTE})
        return ok(result)

    if not wanted:
        result.update({"section": None,
                       "sections": loader.section_index(doc),
                       "recipes": loader.recipe_map(doc),
                       "scenarios": list(doc.get("scenarios") or []),
                       "next_sections": ids,
                       "note": INDEX_NOTE})
        return ok(result)

    sec = loader.find_section(doc, wanted)
    if sec is None:
        return error(f"The packaged guidance document carries no section '{wanted}'. It carries: "
                     + ", ".join(str(i) for i in ids) + ".")

    rules = list(sec.get("rules") or [])
    shown = rules[:loader.MAX_SECTION_RULES]
    result.update({"section": wanted,
                   "section_title": sec.get("title"),
                   "use_when": sec.get("use_when"),
                   "rule_count": len(shown),
                   "rules": shown,
                   "recipe_index": loader.recipe_index(sec),
                   "next_sections": [i for i in ids if i != wanted],
                   "note": SECTION_NOTE})
    if len(rules) > loader.MAX_SECTION_RULES:
        result["truncated"] = True
        result["rule_total"] = len(rules)
        result["note"] = SECTION_NOTE + TRUNCATED_NOTE
    return ok(result)


TOOL_DESCRIPTION = (
    "Read this server's packaged CAD DESIGN GUIDANCE: no argument gives the index, 'section' its "
    "rules, 'recipe' one recipe whole. One per call."
)

tool = (
    Tool.create_simple(name="sys_get_guidance", description=TOOL_DESCRIPTION)
    .add_input_property(*_SECTION.as_property())
    .add_input_property(*_RECIPE.as_property())
    .strict_schema()
)
item = Item.create_tool_item(tool=tool, write="read", handler=handler, run_on_main_thread=False)


def register_tool():
    register(item)
