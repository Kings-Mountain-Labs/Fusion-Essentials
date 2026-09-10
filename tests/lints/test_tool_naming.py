"""Lint/contract for the TOOL NAMING SCHEMA (CLAUDE.md "Read vs Edit").

Every tool name is ``<domain>_<verb>[_<noun>]`` with ``<verb>`` from the closed set below, the
verb's KIND must agree with write=: a read-verb tool is read-only, an edit-verb tool is not - and
every tool lives alone in the module named after it."""

import os

from conftest import load_tool, register_all_tools, TOOLS_DIR

_ORIENT = {"orient"}
_READ = {"get"}
# Acquire: returns a handle/value/image to feed an Edit (no mutation). 'select' is NOT here:
# cam_select_geometry SETS an op's machining geometry (an Edit), and the user-pick acquisition uses
# 'request'/'get' (sys_request_selection / sys_get_selection).
_ACQUIRE = {"find", "measure", "probe", "inspect", "screenshot", "section", "compare",
            "list", "status", "capability", "interference", "request", "compute"}
# Edit: mutates state or runs an async op. The open-ended action set.
_EDIT = {"create", "edit", "delete", "set", "add", "remove", "move", "apply", "generate", "export",
         "convert", "recompute", "activate", "show", "hide", "constrain", "ground", "drive", "arrange",
         "extrude", "revolve", "loft", "chamfer", "fillet", "mirror", "combine", "stitch", "unstitch",
         "hole", "pattern", "base", "construction", "reorder", "save", "open", "close", "new", "copy",
         "insert", "update", "upload", "download", "configure", "reload", "execute", "capture",
         "rigid", "motion",
         "at", "extend", "offset", "patch", "thicken", "trim", "plane", "reduce", "remesh", "to",
         "dimension", "switch", "select", "shell", "sweep", "draft", "split", "project", "reverse",
         "untrim", "post", "restore", "scale", "thread", "repair", "fill", "smooth", "separate",
         "emboss", "pipe", "replace"}

_READ_KIND_VERBS = _ORIENT | _READ | _ACQUIRE       # these MUST be read-only
_EDIT_KIND_VERBS = _EDIT                             # these MUST NOT be read-only

# Tools intentionally exempt from the domain_verb shape (single-token discovery/meta tools).
_SHAPE_EXEMPT = {"workspace_orient"}

# Tools with a read-style verb whose action set still mutates or deletes persistent state, so a
# readOnlyHint client must not auto-approve them. Each reason is the observed behavior that makes
# read-only wrong for this tool - not every read-verb tool qualifies, only these three.
_WRITE_VERB_EXEMPT = {
    "view_section": "its clear action deletes user-created section analyses.",
    "sys_request_selection": "it clears the user's current Fusion selection.",
    "view_screenshot": "file_path writes and silently overwrites a caller-named PNG on local disk.",
}


def _name_and_readonly(item):
    d = item.to_dict()
    name = d.get("name")
    ann = d.get("annotations") or {}
    return name, bool(ann.get("readOnlyHint", False))


_ALL_VERBS = _READ_KIND_VERBS | _EDIT_KIND_VERBS


def _verb_of(name):
    """The verb token of a name. Normally <domain>_<verb>[_noun] (2nd token), but a few tools lead with
    the verb (find_geometry, workspace_orient) - so return the first token that IS a known verb, else
    the 2nd token (so an unknown name still reports its intended slot)."""
    parts = name.split("_")
    for p in parts:
        if p in _ALL_VERBS:
            return p
    return parts[1] if len(parts) >= 2 else parts[0]


class TestToolNaming:
    def test_every_name_is_domain_verb(self):
        bad = []
        for it in register_all_tools():
            name, _ = _name_and_readonly(it)
            if name in _SHAPE_EXEMPT:
                continue
            if len(name.split("_")) < 2:
                bad.append(name)
        assert not bad, (f"tool names must be <domain>_<verb>[_<noun>] (add to _SHAPE_EXEMPT only for a "
                         f"genuine single-token meta tool): {bad}")

    def test_verb_is_in_the_closed_set(self):
        unknown = {}
        for it in register_all_tools():
            name, _ = _name_and_readonly(it)
            if name in _SHAPE_EXEMPT:
                continue
            v = _verb_of(name)
            if v not in (_READ_KIND_VERBS | _EDIT_KIND_VERBS):
                unknown[name] = v
        assert not unknown, (f"verbs not in the closed vocabulary (extend the set in test_tool_naming.py "
                             f"if a new verb is genuinely needed): {unknown}")

    def test_exemption_tables_match_reality(self):
        # the house rule for every lint table: an entry that no longer names a registered tool, or
        # no longer needs its exemption, is deleted - never left to rot.
        status = {}
        for it in register_all_tools():
            name, readonly = _name_and_readonly(it)
            status[name] = readonly
        stale = []
        for name in _SHAPE_EXEMPT:
            if name not in status:
                stale.append(f"_SHAPE_EXEMPT: {name} is not a registered tool")
        for name, reason in _WRITE_VERB_EXEMPT.items():
            assert reason.strip(), f"_WRITE_VERB_EXEMPT: {name} needs a plain-English reason"
            if name not in status:
                stale.append(f"_WRITE_VERB_EXEMPT: {name} is not a registered tool")
            elif status[name]:
                stale.append(f"_WRITE_VERB_EXEMPT: {name} is read-only now - drop the exemption")
        assert not stale, "stale naming-exemption entries:\n  " + "\n  ".join(stale)

    def test_verb_kind_matches_write_status(self):
        mismatches = []
        for it in register_all_tools():
            name, readonly = _name_and_readonly(it)
            if name in _SHAPE_EXEMPT:
                continue
            v = _verb_of(name)
            if v in _READ_KIND_VERBS and not readonly and name not in _WRITE_VERB_EXEMPT:
                mismatches.append(f"{name}: read-verb '{v}' but write!=read")
            if v in _EDIT_KIND_VERBS and readonly:
                mismatches.append(f"{name}: edit-verb '{v}' but write=read (mislabeled read?)")
        assert not mismatches, "name/write= disagreements:\n  " + "\n  ".join(mismatches)

    def test_each_tool_lives_alone_in_the_module_named_after_it(self):
        # One tool per file, the file named after the tool: shared code goes to the family's
        # underscore helper, and a doc citing `<tool>.py` always lands on that tool.
        files = [fn for fn in sorted(os.listdir(TOOLS_DIR))
                 if fn.endswith(".py") and not fn.startswith("_")]
        load_tool(files[0][:-3])                       # bootstraps COMMANDS_DIR onto sys.path
        from mcpServer.mcp_primitives import registry  # importable only after the bootstrap
        bad = []
        for fn in files:
            reg = getattr(load_tool(fn[:-3]), "register_tool", None)
            if not callable(reg):
                bad.append(f"{fn}: registers no tool (a helper module is underscore-prefixed)")
                continue
            registry.reset_registry()
            reg()
            names = sorted(it.to_dict().get("name") for it in registry.get_tools())
            if names != [fn[:-3]]:
                bad.append(f"{fn}: registers {names}")
        assert not bad, ("a tool module registers exactly the one tool it is named after - split "
                         "the extras into their own files, or rename:\n  " + "\n  ".join(bad))
