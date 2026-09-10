"""Lint: the generated permission presets NEVER auto-allow a hard-to-reverse tool.

A destructive-kind tool or ``sys_execute_script`` in any ``gen_posture`` preset's allow list fails,
as does the hatch missing from a preset's deny list. The committed ``.claude/settings.json`` allow
list must be exactly the registry's read bucket plus the ``_OWNER_ALLOWED_WRITES`` exceptions."""

import json
import os

import gen_manifest
import gen_posture

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              os.pardir, os.pardir, ".claude", "settings.json")


def _must_never_auto_allow(tools):
    """Tool names that must never appear in ANY allow list: every destructive-kind tool, plus the
    script hatch by name, so the ban survives even if its kind were ever mislabeled."""
    banned = {t["name"] for t in tools if t["write"] == "destructive"}
    banned.add(gen_posture.SCRIPT_HATCH)
    return banned


def _auto_allow_violations(presets, banned):
    """Every (preset, tool) pair where a banned tool leaked into that preset's allow list; wire
    names are stripped back to bare tool names for the membership test."""
    prefix = gen_posture.WIRE_PREFIX
    violations = []
    for preset_name, preset in presets.items():
        for wire in preset.get("allow", []):
            name = wire[len(prefix):] if wire.startswith(prefix) else wire
            if name in banned:
                violations.append((preset_name, name))
    return violations


class TestPermissionPostureNeverAutoAllowsDestructive:
    def test_no_preset_auto_allows_a_destructive_tool_or_the_script_hatch(self):
        tools = gen_manifest.collect()["tools"]
        presets = gen_posture.build_presets(tools)
        banned = _must_never_auto_allow(tools)
        violations = _auto_allow_violations(presets, banned)
        assert not violations, (
            "gen_posture put a destructive-kind tool (or sys_execute_script) into an allow list - an "
            "agent would run it unattended. Fix the bucket routing in gen_posture.build_presets:\n  "
            + "\n  ".join(f"{p}: {n}" for p, n in violations))

    def test_the_script_hatch_is_denied_in_every_preset(self):
        tools = gen_manifest.collect()["tools"]
        presets = gen_posture.build_presets(tools)
        hatch = gen_posture.WIRE_PREFIX + gen_posture.SCRIPT_HATCH
        for name, preset in presets.items():
            assert hatch in preset.get("deny", []), (
                f"{name}: {gen_posture.SCRIPT_HATCH} must be explicitly denied, not left to fall "
                "through to a prompt")


# Write-kind tools the local permission config auto-approves anyway, each with the reason it is
# exempt from prompting. The tool's write= stays honest on the wire; this table governs only the
# local prompt. Grows only on an owner call.
_OWNER_ALLOWED_WRITES = {
    "view_screenshot": "screenshots are agent-to-agent workflow - a prompt on every capture "
                       "blocks unattended runs, and the file write overwrites only a "
                       "caller-named PNG",
}


def _fusion_allow_diff(allow_entries, read_names, owner_writes=None):
    """(missing, extra) between the committed allow list's fusion-wire entries and the registry's
    read bucket plus the owner's write exceptions; non-fusion entries are out of scope."""
    prefix = gen_posture.WIRE_PREFIX
    committed = {e[len(prefix):] for e in allow_entries if e.startswith(prefix)}
    writes = _OWNER_ALLOWED_WRITES if owner_writes is None else owner_writes
    reads = set(read_names) | set(writes)
    return sorted(reads - committed), sorted(committed - reads)


class TestCommittedSettingsMatchTheReadBucket:
    def test_committed_allow_list_is_exactly_the_read_bucket(self):
        with open(_SETTINGS_PATH, encoding="utf-8") as fh:
            allow = json.load(fh)["permissions"]["allow"]
        reads = gen_posture.buckets(gen_manifest.collect()["tools"])["read"]
        missing, extra = _fusion_allow_diff(allow, reads)
        assert not missing, (
            "read-kind tools missing from .claude/settings.json's allow list - add them (the "
            "committed posture auto-approves exactly the read bucket): " + ", ".join(missing))
        assert not extra, (
            "non-read tools auto-approved in .claude/settings.json - an agent would run them "
            "unattended; remove them (or fix the tool's write= kind if it truly reads): "
            + ", ".join(extra))

