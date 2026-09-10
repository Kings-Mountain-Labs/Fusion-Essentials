"""Lint: a tool that publishes a coordinate FRAME is classified, and mints the keys it claims.

A module in _PUBLISHES_A_FRAME_BLOCK that no longer mints a key its entry names fails; a module
minting a frame payload key while sitting in neither table (or in both) fails; a table entry whose
module publishes no frame is stale and fails."""

import ast
import functools
import os
import re

import _corpus
from conftest import TOOLS_DIR

# A frame payload key being MINTED: a dict-literal entry or an item assignment, under either name
# the wire uses - 'frame' (a block) or 'frame_axes' (the basis published beside its own origin key).
# A module that merely MENTIONS a frame in prose (sketch_set_text points its caller back at
# sketch_create's frame) is a consumer, not a producer, and has nothing to classify.
_FRAME_KEYS = ("frame", "frame_axes")
_MINTS_FRAME = re.compile(
    "|".join(rf"""\[["']{k}["']\]\s*=|["']{k}["']\s*:""" for k in _FRAME_KEYS))

# module -> (the keys inside its frame block, why the caller cannot guess this frame). The keys are
# what the module's own note and description teach, so a rename that leaves either behind fails.
_PUBLISHES_A_FRAME_BLOCK = {
    "sketch_create": (
        ("origin_mm", "normal", "space", "x_world", "y_world", "x_local", "y_local"),
        "sketch_create establishes the frame every later coordinate on that sketch is authored in, "
        "and sketch_get reads the same block back - on an xz/yz origin plane or on a face the local "
        "axes are not world and nothing else in the call says so. origin_mm is always mm, whatever "
        "'units' the call used. Three placement cases, and 'space' is the key that separates them: "
        "a root-owned sketch is world as it stands, a component placed EXACTLY once is lifted "
        "through that occurrence and is world, and a component instanced several times has NO "
        "single world frame (each instance puts the sketch somewhere different), so its "
        "component-local frame is published instead. The axis keys follow space - x_world/y_world "
        "or x_local/y_local - so a consumer keyed on the world name reads a missing key rather "
        "than local numbers under a world label"),
    "find_geometry": (
        ("origin", "x_world", "y_world", "normal"),
        "a planar face's own plane is what lets a caller compute a point ON the face; its "
        "parametric origin is NOT the centroid the same record reports as 'position', so the two "
        "points differ and both are published. This origin rides the call's 'units', unlike the "
        "sketch frame's origin_mm"),
}

# module -> the audited reason its frame is NOT a block of an origin plus three axes.
_PUBLISHES_OTHERWISE = {
    "assembly_get": "four row-local shapes: an occurrence's placement basis is merged FLAT into "
                    "the row (origin/x_axis/y_axis/z_axis, no wrapper), a joint row carries its own "
                    "frame, that same joint row carries the motion's headings as BARE directions "
                    "beside it (rotation_axis / slide_direction - single vectors, not a basis, "
                    "published as the JointMotion member answers, with no placement lift: "
                    "rotation_axis read world on a top-level joint (measured), while "
                    "slide_direction's space and either heading through a nested instance are "
                    "unmeasured), and a joint-origin row's frame holds axes only with the origin "
                    "published beside it as world_position",
    "model_inspect": "its 'frame' is a prose LABEL naming which frame the extents were measured in "
                     "('world axes (axis-aligned)', or the joint origin's part space) and sits "
                     "beside a separate 'frame_axes' block - the label is the disclosure",
    "joint_create_origin": "publishes 'frame_axes' as primary_axis_Z / secondary_axis_X / "
                           "third_axis_Y, with the origin beside it as 'location' - those names "
                           "carry WHICH axis the anchor mode drove, which a fixed x/y/z block "
                           "cannot express",
}


def _tool_modules():
    """Every registered-tool module name (helpers are excluded - they publish nothing themselves)."""
    return sorted(fn[:-3] for fn in os.listdir(TOOLS_DIR)
                  if fn.endswith(".py") and not fn.startswith("_"))


def _source(name):
    return _corpus.text(os.path.join(TOOLS_DIR, f"{name}.py"))


def _keys_in(tree):
    """Every string standing in KEY position in one parsed module - a dict-literal key, or a
    subscript like frame["x_world"]. The same word in a comment, a docstring or a wire sentence is
    a MENTION and is not collected."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            found.update(k.value for k in node.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str))
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if isinstance(node.slice.value, str):
                found.add(node.slice.value)
    return found


@functools.lru_cache(maxsize=1)
def _minted_keys():
    """Every string used as a payload KEY anywhere in tools/, helpers included - a module may
    publish its frame through a shared helper (sketch_create's block is built in _sketch_detail), so
    the keys live one file over."""
    found = set()
    for fn in sorted(os.listdir(TOOLS_DIR)):
        if fn.endswith(".py"):
            found |= _keys_in(_corpus.tree(os.path.join(TOOLS_DIR, fn)))
    return frozenset(found)


def _minted_as_key(key):
    return key in _minted_keys()


def _minting_modules(names=None):
    """The modules whose source MINTS a frame payload key."""
    return [n for n in (names or _tool_modules()) if _MINTS_FRAME.search(_source(n))]


def _classification_gaps(minting, block, otherwise):
    """[complaint] for every minting module in neither table, and every table entry in both."""
    out = []
    for name in sorted(minting):
        if name not in block and name not in otherwise:
            out.append(f"{name}: publishes a frame but is in neither table - add it to "
                       "_PUBLISHES_A_FRAME_BLOCK naming the keys inside the block, or to "
                       "_PUBLISHES_OTHERWISE saying what shape it publishes instead")
    for name in sorted(set(block) & set(otherwise)):
        out.append(f"{name}: is in BOTH tables - a frame is either a block or it is not")
    return out


class TestFrameBlocksMintTheKeysTheyClaim:
    def test_every_declared_key_is_minted_somewhere_in_the_package(self):
        offenders = []
        for name, (keys, _why) in sorted(_PUBLISHES_A_FRAME_BLOCK.items()):
            for key in keys:
                if not _minted_as_key(key):
                    offenders.append(f"{name}: frame key '{key}' is minted nowhere in tools/")
        assert not offenders, (
            "a frame key this table names is not built anywhere - either the payload was renamed "
            "(update the note, the description and this entry together) or the entry is wrong:\n  "
            + "\n  ".join(offenders))


class TestEveryPublishedFrameIsClassified:
    def test_at_least_the_known_publishers_are_found(self):
        # A floor, so the scan cannot pass vacuously if the regex or the directory walk breaks.
        assert {"sketch_create", "find_geometry", "assembly_get", "model_inspect",
                "joint_create_origin"} <= set(_minting_modules())

    def test_a_consumer_that_only_mentions_a_frame_is_not_treated_as_a_publisher(self):
        # sketch_set_text points its caller back at sketch_create's frame; it publishes none.
        assert "sketch_set_text" not in _minting_modules()

    def test_every_module_publishing_a_frame_sits_in_exactly_one_table(self):
        gaps = _classification_gaps(_minting_modules(), _PUBLISHES_A_FRAME_BLOCK,
                                    _PUBLISHES_OTHERWISE)
        assert not gaps, "\n  ".join([""] + gaps)

    def test_no_table_entry_is_stale(self):
        minting = set(_minting_modules())
        stale = [f"{n}: listed here but publishes no frame - drop the entry"
                 for n in sorted(set(_PUBLISHES_A_FRAME_BLOCK) | set(_PUBLISHES_OTHERWISE))
                 if n not in minting]
        assert not stale, "\n  ".join([""] + stale)
