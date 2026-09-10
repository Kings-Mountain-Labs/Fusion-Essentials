# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Render the packaged guidance document as Markdown - one authored render, every consumer.

``resources/read`` serves this text to a client that reads MCP resources, and ``gen_guidance.py``
writes the same functions' output into the checked-in Claude skill package: ``map_text`` as the
SKILL.md map, ``section_text`` as one playbook file per section. The render lives here, beside the
document it renders, so no two channels can say different things. Whether the DATA is fit to render
at all - the size caps, the registry check behind every step - stays with the generator.
"""

from . import loader

# The one declared rule kind: a deterministic safety/API rule says so and is marked as one in the
# render; every other rule is strategy.
SAFETY_INVARIANT = "safety_invariant"


def _rule_lines(rule):
    """One rule as one line: the instruction first, its scope and its boundary in the same
    sentence, then how it is proved; the example on its own line when there is one."""
    head = f"**{rule['id']}**"
    if rule.get("kind") == SAFETY_INVARIANT:
        head += " (safety invariant)"
    prove = "; ".join(f"`{s['tool']}`: {s['observe']}" for s in rule["prove"])
    do = rule["do"][0].upper() + rule["do"][1:]
    lines = [f"{head} - {do} when {rule['when']}; not for {rule['except']}. Prove: {prove}."]
    if rule.get("example"):
        lines.append(f"Example: {rule['example']}.")
    return lines


def recipe_text(doc, recipe_id):
    """One recipe's rendered Markdown - the unit the recipe size limit measures.

    KeyError when the document carries no recipe with that id, for the same reason section_text
    raises: a miss is a defect in the data, not an empty recipe to render."""
    rec = loader.find_recipe(doc, recipe_id)
    if rec is None:
        raise KeyError(recipe_id)
    lines = [f"#### {rec['title']}", "", f"Use when {rec['use_when']}.", ""]
    for number, step in enumerate(rec.get("steps") or [], 1):
        lines.append(f"{number}. `{step['tool']}` - {step['do']}. Read back: {step['read_back']}.")
    bar = rec.get("bar") or {}
    lines += ["", f"Bar - measure: {bar.get('measure')}. Eyes: {bar.get('eyes')}."]
    exemplar = rec.get("exemplar")
    if exemplar:
        lines.append(f"Exemplar: {exemplar['document']} ({exemplar['urn']}) - "
                     f"{exemplar['look_at']}. Access: {exemplar['access']}")
    return "\n".join(lines).rstrip() + "\n"


def section_text(doc, section_id):
    """One section's rendered Markdown - its rules, then its recipes - heading included.

    KeyError when the document carries no section with that id: the ids are a closed set the caller
    already chose from, so a miss is a defect in the data rather than an empty section to render."""
    sec = loader.find_section(doc, section_id)
    if sec is None:
        raise KeyError(section_id)
    lines = [f"## {sec['title']}", ""]
    for rule in (sec.get("rules") or []):
        lines += _rule_lines(rule)
        lines.append("")
    recipes = sec.get("recipes") or []
    if recipes:
        lines += ["### Recipes", ""]
        for rec in recipes:
            lines.append(recipe_text(doc, rec["id"]).rstrip())
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def body(doc):
    """The whole document as Markdown, in the reading order the package declares.

    This is what ``resources/read`` returns at the document's own address - every rule and every
    recipe, unbounded, for a client that would rather hold the document than call per section."""
    parts = [f"# {doc['title']}", "", doc["summary"], ""]
    for section_id in loader.SECTION_IDS:
        parts.append(section_text(doc, section_id).rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def map_text(doc):
    """The skill MAP: the kernel whole, then WHERE each playbook and each recipe is fetched.

    An agent carrying this text holds no recipe steps - it holds the one call that returns them,
    so the wire is paid for the recipe actually needed rather than for the whole document."""
    kernel = loader.find_section(doc, loader.KERNEL)
    parts = [f"# {doc['title']}", "", doc["summary"], "", "## Kernel", ""]
    for rule in (kernel.get("rules") or []) if kernel else []:
        parts += _rule_lines(rule)
        parts.append("")
    parts += ["## Playbooks", "",
              "A playbook `<id>` is the file `playbooks/<id>.md`, or `sys_get_guidance(section=\"<id>\")`.",
              ""]
    for sec in (doc.get("sections") or []):
        if sec.get("id") == loader.KERNEL:
            continue
        parts.append(f"- `{sec['id']}` - {sec['use_when']}")
    parts += ["", "## Recipes", "",
              "A recipe `<id>` is `sys_get_guidance(recipe=\"<id>\")` - ordered steps, a read-back "
              "per step, a bar for done. Its prefix names the playbook it belongs to.", ""]
    for rec in loader.recipes(doc):
        parts.append(f"- `{rec['id']}` - {rec['use_when']}")
    return "\n".join(parts).rstrip() + "\n"
