# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Read the packaged guidance document, and version it by the hash of what was read.

The JSON sits beside this module and is resolved RELATIVE TO IT, so the read answers the same in
any process working directory. A document that does not load raises ``GuidanceUnavailable`` naming
the file: a packaged asset that is not there is reported, never substituted.
"""

import hashlib
import json
import os

# The section order the document declares, named here so a caller can build a closed input over it
# without opening the file (and so a load failure still leaves a tool with a schema). It is a fact
# OF the JSON: test_sys_get_guidance.py holds this tuple against the shipped document's own ids.
SECTION_IDS = ("kernel", "plan", "sketch", "model", "surface", "assemble", "validate", "finish",
               "manufacture")

# The recipe ids the document declares, the same kind of fact as SECTION_IDS and held the same way:
# a closed input over them without opening the file, pinned to the shipped document by its test.
RECIPE_IDS = ("sketch-anchored-profile", "sketch-link-between-bores", "sketch-organic-outline",
              "model-moulded-part", "model-frozen-body-with-interfaces", "model-parametric-family",
              "surface-swept-bottle", "surface-skin-into-parts",
              "assemble-part-modelled-in-place", "assemble-screw-motion",
              "manufacture-choose-a-strategy", "manufacture-prove-a-toolpath",
              "manufacture-radial-hole-across-the-axis")

# The recipe cam_get's strategies note points a caller at - a fact of the document like the ids
# above, so the pointer and the served id are one string and gen_guidance can refuse a document
# that does not declare it.
STRATEGY_RECIPE_ID = "manufacture-choose-a-strategy"

# The one section that holds for every scenario. It is rendered whole into the skill map, while
# every other section is pointed at, so both the render and the authoring gate name it from here.
KERNEL = "kernel"

# The most rules ONE section may carry, held here so the serving side and the authoring gate read
# the same number: sys_get_guidance truncates a section read at it, and gen_guidance.validate
# refuses a document whose section holds more - so no shipped section can carry rules that one call
# would silently drop. The resource channel is not bound by it: render.body renders every rule of
# every section.
MAX_SECTION_RULES = 8

GUIDANCE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "parametric_cad_design.json")


class GuidanceUnavailable(Exception):
    """The packaged document did not load: it is missing, unreadable, or not JSON."""


def load(path=None):
    """(document, sha256) for the packaged guidance, or GuidanceUnavailable naming the file.

    The hash is taken over the BYTES that were read, so it versions exactly what is served rather
    than a re-serialization of the parsed object."""
    path = GUIDANCE_PATH if path is None else path
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise GuidanceUnavailable(
            f"The packaged guidance document did not read: {path} - {exc}. It ships beside the "
            "server code; until that file is restored there is no guidance to serve.")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GuidanceUnavailable(
            f"The packaged guidance document is not readable JSON: {path} - {exc}.")
    return doc, hashlib.sha256(raw).hexdigest()


def section_ids(doc):
    """The section ids the DOCUMENT carries, in its own order - the reading order it declares."""
    return [sec.get("id") for sec in (doc.get("sections") or []) if isinstance(sec, dict)]


def find_section(doc, section_id):
    """The section carrying this id, or None. EXACT match: the ids are a closed set the caller
    already chose from, not a name space to search."""
    for sec in (doc.get("sections") or []):
        if isinstance(sec, dict) and sec.get("id") == section_id:
            return sec
    return None


def section_index(doc):
    """[{id, title, use_when, rule_count, recipe_count}] per section - the compact rows a
    no-argument read answers with, where use_when says when to ask and the counts say how much."""
    return [{"id": sec.get("id"), "title": sec.get("title"), "use_when": sec.get("use_when"),
             "rule_count": len(sec.get("rules") or []),
             "recipe_count": len(sec.get("recipes") or [])}
            for sec in (doc.get("sections") or []) if isinstance(sec, dict)]


def recipes(doc):
    """Every recipe the document carries, in its own section-then-authored order."""
    return [rec for sec in (doc.get("sections") or []) if isinstance(sec, dict)
            for rec in (sec.get("recipes") or []) if isinstance(rec, dict)]


def find_recipe(doc, recipe_id):
    """The recipe carrying this id, or None. EXACT match, like find_section: the ids are a closed
    set the caller already chose from."""
    for rec in recipes(doc):
        if rec.get("id") == recipe_id:
            return rec
    return None


def recipe_map(doc):
    """[{id, section, title, use_when}] for EVERY recipe - the map an agent picks one from."""
    return [{"id": rec.get("id"), "section": rec.get("section"), "title": rec.get("title"),
             "use_when": rec.get("use_when")} for rec in recipes(doc)]


def recipe_index(sec):
    """[{id, title, use_when}] for ONE section's recipes - what a section read names them by,
    without carrying a body."""
    return [{"id": rec.get("id"), "title": rec.get("title"), "use_when": rec.get("use_when")}
            for rec in (sec.get("recipes") or []) if isinstance(rec, dict)]
