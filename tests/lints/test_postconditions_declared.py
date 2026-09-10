"""Lint: every WRITE/DESTRUCTIVE tool declares HOW its effect is proven - postconditions=[...] for a
detachable effect, else verification=Verification(kind=...) from the closed set (item.py). Every
reference it carries RESOLVES: an evidence_test node id pytest would collect and no other tool
claims, a registered read poller, an observing receipt row, an OPEN ledger row. Gaps only shrink."""

import json
import os
import re
import subprocess
import sys
import tempfile

import pytest

from conftest import is_write_tool, load_tool, register_all_tools

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _postconditions_of(item):
    """Walk the handler wrapper chain (write-guard -> assert) to the declared postconditions."""
    h = item.handler
    seen = set()
    while h is not None and id(h) not in seen:
        seen.add(id(h))
        posts = getattr(h, "__assert_postconditions__", None)
        if posts is not None:
            return posts
        h = getattr(h, "__wrapped__", None)
    return None


# The measured count of kind="gap" declarations - a tool whose success no read-back can confirm.
# Shrink-only: closing a gap (an inline gate or a kernel kind lands) lowers it; raising it means a
# NEW tool shipped with no effective read-back, which is a deliberate decision this number makes
# visible instead of a quiet reclassification. The ceiling is an alarm that UN-RINGS itself:
# the shrink-only half of the test below forces the number back down the moment a gap closes, so
# a tool parked here while its evidence is unrecorded cannot quietly stay parked.
_GAP_CEILING = 2


def _verification_of(item):
    """The registration's verification classification, or None (mcp_primitives/item.py)."""
    return getattr(item, "verification", None)


def _gap_tools(items):
    """Every tool whose declaration says its effect cannot be verified."""
    return sorted(it.get_name() for it in items
                  if _verification_of(it) is not None and _verification_of(it).kind == "gap")


class TestPostconditionsDeclared:
    def test_the_gap_count_only_shrinks(self):
        gaps = _gap_tools(register_all_tools())
        assert len(gaps) <= _GAP_CEILING, (
            f"{len(gaps)} gap entries exceed the ceiling of {_GAP_CEILING}. A gap is a tool whose "
            "success cannot be verified at all - adding one is a deliberate decision: raise "
            "_GAP_CEILING in the same diff with the new tool's named defect, or give the tool a "
            "real read-back.\n  " + "\n  ".join(gaps))
        if len(gaps) < _GAP_CEILING:
            raise AssertionError(
                f"only {len(gaps)} gap entries remain - lower _GAP_CEILING to {len(gaps)} to "
                "lock the win in:\n  " + "\n  ".join(gaps))

    def test_every_write_tool_declares_how_its_effect_is_verified(self):
        missing = []
        for it in register_all_tools():
            if not is_write_tool(it):
                continue                      # read tools mutate nothing to verify
            if _postconditions_of(it):
                continue
            if _verification_of(it) is not None:
                continue
            missing.append(it.get_name())
        assert not missing, (
            "a write tool declares how its effect is proven: postconditions=[...] for a detachable "
            "effect, else verification=Verification(kind=...) - one of inline / effect / deferred / "
            "external / dynamic / gap, each carrying the reference this lint resolves (see the "
            "Verification kinds in item.py). These declare neither:\n  " + "\n  ".join(sorted(missing)))

    def test_declared_postconditions_are_postcondition_kinds(self):
        kernel = load_tool("_assert")
        for it in register_all_tools():
            posts = _postconditions_of(it)
            for p in posts or []:
                assert isinstance(p, kernel.Postcondition), (
                    f"{it.get_name()}: postconditions must be _assert.Postcondition kinds, got {type(p)}")
                assert p.rung in kernel.RUNGS, f"{it.get_name()}: {p.name} declares rung {p.rung!r}"


# The minimum a write VERB's read-back has to prove, on the _assert.RUNGS ladder. A verb that adds or
# removes material or moves geometry is held to GEOMETRY (the right shape changed); one that sets,
# names, creates or deletes a thing is held to VALUE (the right thing read back); the rest to EXISTS.
# A name matches on its longest listed prefix; a write verb listed nowhere is held to EXISTS.
_MINIMUM_RUNG = {
    "geometry": ("model_", "surface_", "mesh_", "sketch_add", "sketch_move", "sketch_copy",
                 "sketch_project", "sketch_edit", "sketch_insert", "sketch_dimension",
                 "joint_drive", "assembly_move", "design_move_occurrence"),
    "value": ("cam_", "data_", "doc_", "design_", "drawing_", "joint_", "param_", "pmi_",
              "appearance_", "assembly_", "save_", "sketch_create", "sketch_add_3d_line",
              "sketch_constrain", "sketch_set_text", "sketch_delete", "model_set_material",
              "model_create_component", "model_construction", "mesh_delete", "mesh_insert",
              "mesh_export"),
    "exists": ("view_", "sys_", "model_base_feature", "cam_show_toolpath", "cam_generate",
               "cam_activate_setup", "design_activate_component"),
}
# The kinds whose handler reads an effect back in the call; a deferred kind's rung is the POLLER's
# read, made later, so it is recorded but never counted toward the verb's minimum.
_IN_CALL_KINDS = ("inline", "effect")

# Tools whose read-back stops short of their verb's minimum, each with the reason - a platform
# that offers no stronger read, a request that carries no value to compare, or a kind not yet
# built (the ledger id names the row). Shrink-only: an entry leaves when the read-back lands.
_RUNG_SHORT = {
    "mesh_generate_face_groups": "a landed generation can leave the face-group count and the "
                                 "group ids unchanged, so no read can convict",
    "cam_apply_template": "CAMTemplate exposes no operation list to compare; the setup's own "
                          "census (count grew, each added op read) is the strongest read",
    "data_delete_file": "post-delete resolvability of a URN is unmeasured (DATADELETE-RESOLVE-1)",
    "data_delete_folder": "post-delete resolvability of a folder id is unmeasured "
                          "(DATADELETE-RESOLVE-1)",
    "doc_new": "the request carries no value: a new document exists and is active",
    "design_recompute": "the request carries no value: the timeline health after the rebuild is "
                        "the only observation",
}


def _minimum_rung_for(name):
    """The rung a tool's verb is held to: its longest matching prefix in _MINIMUM_RUNG."""
    best, best_len = "exists", -1
    for rung, prefixes in _MINIMUM_RUNG.items():
        for prefix in prefixes:
            if name.startswith(prefix) and len(prefix) > best_len:
                best, best_len = rung, len(prefix)
    return best


def _declared_rung(item, kernel):
    """The strongest rung a tool's IN-CALL declarations reach - the kernel kinds and, beside them,
    an inline/effect verification's own rung - or None when nothing is read back in the call."""
    rungs = [p.rung for p in (_postconditions_of(item) or [])]
    v = _verification_of(item)
    if v is not None and v.kind in _IN_CALL_KINDS and v.rung:
        rungs.append(v.rung)
    if not rungs:
        return None
    return max(rungs, key=kernel.RUNGS.index)


class TestRungMeetsTheVerb:
    def test_every_read_back_declares_its_rung(self):
        silent = []
        for it in register_all_tools():
            v = _verification_of(it)
            if v is not None and v.kind in _IN_CALL_KINDS + ("deferred",) and not v.rung:
                silent.append(f"{it.get_name()}: {v.kind} with no rung")
        assert not silent, (
            "a verification that reads an effect back says how much the read proves - declare "
            "rung= (count | exists | value | geometry) on each of these:\n  " + "\n  ".join(silent))

    def test_every_write_reads_back_at_least_what_its_verb_demands(self):
        kernel = load_tool("_assert")
        short = []
        for it in register_all_tools():
            if not is_write_tool(it):
                continue
            declared = _declared_rung(it, kernel)
            if declared is None:
                continue                      # deferred / external / dynamic / gap: nothing read in-call
            need = _minimum_rung_for(it.get_name())
            if kernel.RUNGS.index(declared) < kernel.RUNGS.index(need):
                if it.get_name() in _RUNG_SHORT:
                    continue
                short.append(f"{it.get_name()}: declares {declared}, its verb needs {need}")
        assert not short, (
            "a write's read-back proves less than its verb demands (count < exists < value < "
            "geometry). Raise the read-back - a kernel kind of the needed rung, or the handler "
            "reading the right value/geometry back with rung= declared on its verification - or "
            "add the tool to _RUNG_SHORT with the reason it cannot:\n  " + "\n  ".join(sorted(short)))

    def test_every_short_entry_is_still_short(self):
        kernel = load_tool("_assert")
        items = {it.get_name(): it for it in register_all_tools()}
        stale = []
        for name in _RUNG_SHORT:
            it = items.get(name)
            if it is None:
                stale.append(f"{name}: no such tool")
                continue
            declared = _declared_rung(it, kernel)
            need = _minimum_rung_for(name)
            if declared is None or kernel.RUNGS.index(declared) >= kernel.RUNGS.index(need):
                stale.append(f"{name}: now reaches {declared}, its verb needs {need}")
        assert not stale, ("_RUNG_SHORT only shrinks - these entries no longer describe a shortfall, "
                           "remove them:\n  " + "\n  ".join(stale))


# An evidence node id is spendable only when normal project discovery collects it. A bounded child
# collection records pytest's structured session.items without executing tests.

_DYNAMIC_TOOL = "sys_execute_script"          # the one caller-authored effect (the script hatch)
_NODE_ID = re.compile(r"^tests/[\w/]+\.py(?:::\w+){1,2}$")
_RECEIPT_REF = re.compile(r"^tests/live/[\w.]+\.md#\w+$")
_DEFECT_ID = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
# A receipt bucket that records the ABSENCE of an observation. A reference to one of these names a
# row that exists but proves nothing, which is what a gap is for (the receipt's own header classes a
# skipped row "Not verified - excused").
_EMPTY_BUCKETS = ("skipped", "pending")
# An OPEN row of the defect ledger: an unticked checkbox opening the line, then the id.
_OPEN_ROW = r"^- \[ \] {id}\b"
# The defect ledger's filename under plans/. The file is UNTRACKED (the plans tree is gitignored),
# so it is present on a working machine and absent from a clean checkout - which is why the gap-id
# check skips rather than passes when it cannot find it.
_LEDGER_NAME = "backlog.md"

_COLLECTOR_PLUGIN = '''import json
import os


def pytest_collection_finish(session):
    with open(os.environ["FUSION_EVIDENCE_NODE_IDS"], "w", encoding="utf-8") as stream:
        json.dump([item.nodeid for item in session.items], stream)
'''


def _collected_node_ids():
    """The node ids normal project discovery collects, or a bounded collection failure."""
    with tempfile.TemporaryDirectory(prefix="fusion-evidence-collection-") as temporary:
        plugin_path = os.path.join(temporary, "_fusion_evidence_collector.py")
        result_path = os.path.join(temporary, "node_ids.json")
        with open(plugin_path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(_COLLECTOR_PLUGIN)
        env = os.environ.copy()
        env.pop("PYTEST_ADDOPTS", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["FUSION_EVIDENCE_NODE_IDS"] = result_path
        env["PYTHONPATH"] = temporary + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "--color=no",
                   "-p", "no:cacheprovider", "-p", "_fusion_evidence_collector", "tests"]
        try:
            result = subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True,
                                    encoding="utf-8", errors="replace", timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return frozenset(), f"pytest collection could not finish: {exc}"
        detail = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode not in (0, 5):
            return frozenset(), (f"pytest collection exited {result.returncode}: "
                                 + (detail[-1200:] or "no diagnostic output"))
        try:
            with open(result_path, encoding="utf-8") as stream:
                node_ids = json.load(stream)
        except (OSError, ValueError) as exc:
            return frozenset(), f"pytest collection produced no readable node-id record: {exc}"
        if not isinstance(node_ids, list) or not all(isinstance(node, str) for node in node_ids):
            return frozenset(), "pytest collection produced a malformed node-id record"
        return frozenset(node.replace("\\", "/") for node in node_ids), ""


def _resolve_node_id(node_id, collected=None, collection_problem=""):
    """'' when normal pytest discovery collects the evidence node id, else why it does not."""
    if not _NODE_ID.match(node_id):
        return "not a 'tests/<file>.py::[Class::]test_name' node id"
    path = os.path.join(REPO_ROOT, *node_id.split("::", 1)[0].split("/"))
    if not os.path.isfile(path):
        return f"no such test file: {node_id.split('::', 1)[0]}"
    if collected is None:
        collected, collection_problem = _collected_node_ids()
    if collection_problem:
        return collection_problem
    if node_id in collected or any(node.startswith(node_id + "[") for node in collected):
        return ""
    return "pytest did not collect this node id under the project's normal tests/ discovery"


def _resolve_receipt(ref):
    """'' when the reference names a receipt row that records an OBSERVATION, else why it does not.

    The row must be a real TABLE row - the tool named in the first cell, not merely mentioned in
    the file's prose - and its bucket cell must not be one of _EMPTY_BUCKETS: a skipped or pending
    row says the tool was not driven, so pointing a verification at it would cite the absence of
    evidence as evidence."""
    if not _RECEIPT_REF.match(ref):
        return "not a 'tests/live/<receipt>.md#<tool>' reference"
    rel, _, anchor = ref.partition("#")
    path = os.path.join(REPO_ROOT, *rel.split("/"))
    if not os.path.isfile(path):
        return f"no such receipt: {rel}"
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    row = re.search(r"^\|\s*" + re.escape(anchor) + r"\s*\|([^|]*)\|", text, re.M)
    if row is None:
        return f"{rel} carries no row for '{anchor}'"
    bucket = row.group(1).strip().lower()
    if bucket.startswith(_EMPTY_BUCKETS):
        return (f"{rel}'s row for '{anchor}' reads '{row.group(1).strip()}' - it records no "
                "observation, so it is not evidence of anything")
    return ""


def _resolve_defect(defect_id):
    """'' when the defect id is well shaped AND opens a row of the defect ledger, else why it does not.

    Returns None - not a verdict - when the ledger file is absent, which the caller turns into a
    visible skip. A well-shaped id that no ledger carries is exactly the shape this exists to
    catch, so answering '' on a missing file would pass the mutant it was written for."""
    if not _DEFECT_ID.match(defect_id or ""):
        return f"{defect_id!r} is not a ledger id like 'DRAW-1'"
    path = os.path.join(REPO_ROOT, "plans", _LEDGER_NAME)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if not re.search(_OPEN_ROW.format(id=re.escape(defect_id)), text, re.M):
        return f"the defect ledger carries no OPEN row for {defect_id}"
    return ""


def _poller_problem(items, poller_name):
    """Why a deferred declaration's named poller cannot confirm the effect, or ''."""
    poller = items.get(poller_name)
    if poller is None:
        return f"poller '{poller_name}' is not a registered tool"
    if is_write_tool(poller):
        return f"poller '{poller_name}' is a write, not a read that confirms"
    return ""


def _duplicate_evidence_claims(items):
    """node id -> the tools claiming it, for every node id claimed more than once."""
    claimed = {}
    for it in items:
        v = _verification_of(it)
        if v is None or not v.evidence_test:
            continue
        claimed.setdefault(v.evidence_test, []).append(it.get_name())
    return {node: sorted(tools) for node, tools in claimed.items() if len(tools) > 1}


class TestVerificationDeclarations:
    """The declaration side: a closed kind whose every reference resolves."""

    def test_every_declaration_is_a_verification_kind(self):
        items = register_all_tools()        # also bootstraps the mcpServer package path
        from mcpServer.mcp_primitives.item import Verification
        wrong = []
        for it in items:
            v = _verification_of(it)
            if v is None:
                continue
            if not isinstance(v, Verification):
                wrong.append(f"{it.get_name()}: {type(v)}")
            elif v.kind not in Verification.KINDS:
                wrong.append(f"{it.get_name()}: kind {v.kind!r}")
        assert not wrong, ("verification= must be a Verification kind from the closed set "
                           f"{list(Verification.KINDS)}:\n  " + "\n  ".join(wrong))

    def test_every_declared_evidence_test_resolves(self):
        declared = [(it.get_name(), _verification_of(it).evidence_test)
                    for it in register_all_tools()
                    if (_verification_of(it) is not None
                        and _verification_of(it).evidence_test)]
        collected, collection_problem = _collected_node_ids()
        if collection_problem:
            pytest.fail("evidence_test node ids could not be collected: " + collection_problem)
        broken = []
        for tool_name, node_id in declared:
            why = _resolve_node_id(node_id, collected, collection_problem)
            if why:
                broken.append(f"{tool_name} -> {node_id}: {why}")
        assert not broken, (
            "these tools name an evidence_test that does not resolve - the test was renamed, moved "
            "or deleted, so the declaration claims a proof nobody can run. Point the declaration at "
            "the test that now carries the obligation, or write one:\n  " + "\n  ".join(broken))

    def test_no_evidence_test_is_claimed_by_two_tools(self):
        shared = _duplicate_evidence_claims(register_all_tools())
        assert not shared, (
            "one test cannot carry two tools' obligations - each needs its own biting proof:\n  "
            + "\n  ".join(f"{node}: {', '.join(t)}" for node, t in sorted(shared.items())))

    def test_a_deferred_declaration_names_a_registered_read_tool_as_its_poller(self):
        items = {it.get_name(): it for it in register_all_tools()}
        bad = []
        for name, it in items.items():
            v = _verification_of(it)
            if v is None or v.kind != "deferred":
                continue
            why = _poller_problem(items, v.poller)
            if why:
                bad.append(f"{name} -> {why}")
        assert not bad, ("a deferred effect is confirmed by a NAMED read tool the payload sends "
                         "the caller to:\n  " + "\n  ".join(sorted(bad)))

    def test_an_external_receipt_names_a_row_of_a_live_receipt(self):
        broken = []
        for it in register_all_tools():
            v = _verification_of(it)
            if v is None or not v.evidence_receipt:
                continue
            why = _resolve_receipt(v.evidence_receipt)
            if why:
                broken.append(f"{it.get_name()} -> {v.evidence_receipt}: {why}")
        assert not broken, ("an evidence_receipt points at the receipt row that carries the tool's "
                            "live evidence:\n  " + "\n  ".join(broken))

    def test_dynamic_reaches_only_the_script_hatch(self):
        others = sorted(it.get_name() for it in register_all_tools()
                        if (_verification_of(it) is not None
                            and _verification_of(it).kind == "dynamic"
                            and it.get_name() != _DYNAMIC_TOOL))
        assert not others, (
            f"kind='dynamic' says the requested effect is CALLER-AUTHORED, which is true of "
            f"{_DYNAMIC_TOOL} alone - every other tool declares its own effect and can be held to "
            "it. A second consumer is a redesign decision, not a classification:\n  "
            + "\n  ".join(others))

    def test_a_gap_declaration_resolves_to_an_open_ledger_row(self):
        bad, unresolvable = [], []
        for it in register_all_tools():
            v = _verification_of(it)
            if v is None or v.kind != "gap":
                continue
            why = _resolve_defect(v.defect_id)
            if why is None:
                unresolvable.append(f"{it.get_name()} -> {v.defect_id}")
            elif why:
                bad.append(f"{it.get_name()} -> {why}")
        assert not bad, ("a gap names the id of a defect the defect ledger still carries OPEN, so an "
                         "unverifiable tool is tracked where it can be closed:\n  "
                         + "\n  ".join(sorted(bad)))
        if unresolvable:
            pytest.skip(
                "the defect ledger is not in this checkout, so these gap ids could not be resolved: "
                + ", ".join(sorted(unresolvable)) + ". The ledger is untracked (the plans tree is "
                "gitignored) and lives on the working machine, so a clean checkout cannot see it - "
                "this check SKIPS visibly there rather than passing on a file it never opened. Run "
                "it where the ledger is present.")


