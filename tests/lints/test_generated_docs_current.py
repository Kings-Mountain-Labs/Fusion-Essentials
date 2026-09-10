"""Lint: the generated docs (TOOL_MANIFEST/TOOL_POINTER_MAP/PERMISSION_POSTURE + the CLAUDE.md map) match the live tree.
This shells ``gen_all.py --check`` so a stale artifact fails as a normal test - skipped only when a
content FINGERPRINT over every generator INPUT and OUTPUT matches one a passing check already saw.
A generator reading something outside those two sets joins ``_fingerprinted_paths`` in the same change."""

import glob
import hashlib
import os
import subprocess
import sys

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tests/lints/ -> tests/
REPO_ROOT = os.path.dirname(TESTS_DIR)

_CACHE_KEY = "fusion_essentials/generated_docs_fingerprint"

# What the generators READ: the whole add-in tree (tools + primitives + server - gen_manifest and
# gen_posture load every module through the registry, gen_wiring parses every tool's source) and the
# scripts under tests/ that do the deriving.
_INPUT_TREE = os.path.join(REPO_ROOT, "commands", "mcpServer")
# What they WRITE. The two CLAUDE.md files are SPLICED, so only a block of each is generated - the
# whole file is fingerprinted anyway, which costs a needless miss on an unrelated edit and never a
# missed staleness.
_OUTPUT_TREE = os.path.join(TESTS_DIR, "generated")
# gen_guidance writes a whole PACKAGE - the map plus one playbook per section - so the tree is
# walked rather than named file by file: a playbook added, renamed or deleted moves the digest.
_SKILL_TREE = os.path.join(REPO_ROOT, ".claude", "skills", "parametric-cad-design")
_OUTPUT_FILES = (
    os.path.join(TESTS_DIR, "api_surface.py"),
    os.path.join(REPO_ROOT, "CLAUDE.md"),
    os.path.join(REPO_ROOT, "commands", "mcpServer", "tools", "CLAUDE.md"),
)


def _bindings_identity():
    """A cheap stand-in for hashing the Fusion bindings' megabytes: their directory plus each
    scanned module's size and mtime. A Fusion update installs a NEW webdeploy build directory, so
    the path itself moves; size+mtime cover a module rewritten in place. gen_api_surface reads
    these and nothing in the repo records their content, so without this a Fusion update would
    leave tests/api_surface.py stale behind a fingerprint that never noticed."""
    if TESTS_DIR not in sys.path:
        sys.path.insert(0, TESTS_DIR)
    import gen_api_surface
    root = gen_api_surface.find_bindings()
    if root is None:
        # gen_api_surface --check FAILS without bindings; the miss lets it say so itself.
        return "bindings:absent"
    parts = [root]
    for mod in gen_api_surface._MODULES:
        path = os.path.join(root, mod + ".py")
        if os.path.isfile(path):
            st = os.stat(path)
            parts.append(f"{mod}:{st.st_size}:{st.st_mtime_ns}")
        else:
            parts.append(f"{mod}:absent")
    return "|".join(parts)


def _generated_artifacts():
    """Every file a generator WRITES: the two spliced CLAUDE.md files, tests/api_surface.py, the
    generated tree and the guidance skill package."""
    paths = set(_OUTPUT_FILES)
    for tree in (_OUTPUT_TREE, _SKILL_TREE):
        for root, _dirs, names in os.walk(tree):
            paths.update(os.path.join(root, n) for n in names)
    return tuple(sorted(p for p in paths if os.path.isfile(p)))


def _fingerprinted_paths():
    """Every file whose bytes decide whether the committed artifacts are current, sorted and
    de-duplicated (an output that also sits under the input tree is hashed once)."""
    paths = set(_generated_artifacts())
    paths.update(glob.glob(os.path.join(TESTS_DIR, "*.py")))
    # the live act modules are generator INPUTS too: gen_strategies reads verify_acts_census's
    # CENSUS, so a PROVEN/MEASURED flip there rides a stale fingerprint without them.
    paths.update(glob.glob(os.path.join(TESTS_DIR, "live", "*.py")))
    # gen_enforcement reads lint module docstrings for ENFORCEMENT_MAP.
    paths.update(glob.glob(os.path.join(TESTS_DIR, "lints", "**", "*.py"), recursive=True))
    for root, _dirs, names in os.walk(_INPUT_TREE):
        if "__pycache__" in root:
            continue
        # .json admits the guidance data gen_guidance reads - an input, so a JSON-only edit misses
        # the cache and pays the full check instead of riding a stale fingerprint.
        paths.update(os.path.join(root, n) for n in names if n.endswith((".py", ".json")))
    return tuple(sorted(p for p in paths if os.path.isfile(p)))


def _digest(paths, salt=""):
    """One hash over `paths`' NAMES and contents. The name is hashed beside the bytes so a file
    that moved - same content, different path - is a different fingerprint, not an invisible one."""
    h = hashlib.sha256()
    h.update(salt.encode("utf-8"))
    h.update(b"\0")
    for path in paths:
        h.update(os.path.relpath(path, REPO_ROOT).replace("\\", "/").encode("utf-8"))
        h.update(b"\0")
        with open(path, "rb") as fh:
            h.update(fh.read())
        h.update(b"\0")
    return h.hexdigest()


def _fingerprint():
    return _digest(_fingerprinted_paths(), salt=_bindings_identity())


class TestGeneratedDocsAreCurrent:
    def test_generator_check_passes(self, pytestconfig):
        fingerprint = _fingerprint()
        cache = getattr(pytestconfig, "cache", None)
        if cache is not None and cache.get(_CACHE_KEY, None) == fingerprint:
            return   # every byte this check reads and writes is what a passing check already saw
        proc = subprocess.run(
            [sys.executable, os.path.join(TESTS_DIR, "gen_all.py"), "--check"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == 0, (
            "a generated doc is stale. Regenerate with `py -3 tests/gen_all.py` and commit the "
            f"result.\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
        if cache is not None:
            cache.set(_CACHE_KEY, fingerprint)


class TestGeneratedFilesUseLfNewlines:
    def test_no_generated_artifact_holds_a_carriage_return(self):
        # git normalizes CRLF on add and then reports the whole file as changed on every diff.
        offenders = []
        for path in _generated_artifacts():
            with open(path, "rb") as fh:
                carriage = fh.read().count(b"\r")
            if carriage:
                offenders.append(f"{os.path.relpath(path, REPO_ROOT)}: {carriage} CR bytes")
        assert not offenders, (
            "generated files hold CR bytes - regenerate with `py -3 tests/gen_all.py`. If they "
            "come back, the generator writing them either opens its output with the platform "
            "newline, or decides the file is already current by comparing universal-newline text "
            "instead of its raw bytes:\n  " + "\n  ".join(offenders))


class TestTheFingerprintCoversEveryArtifact:
    """A path missing from the set is a hole the cache would sit in permanently: that artifact could
    be hand-edited, or its source changed, and the check would never be asked again."""

    def test_the_bindings_identity_names_the_installed_build(self):
        # a Fusion update changes nothing in the repo, so only the salt can carry it into the digest
        ident = _bindings_identity()
        assert ident == "bindings:absent" or ident.count("|") == 4, ident

    def test_every_generated_artifact_is_fingerprinted(self):
        watched = set(_fingerprinted_paths())
        for artifact in ("tests/generated/TOOL_MANIFEST.md",
                         "tests/generated/TOOL_POINTER_MAP.md",
                         "tests/generated/PERMISSION_POSTURE.md",
                         "tests/api_surface.py",
                         "CLAUDE.md",
                         "commands/mcpServer/tools/CLAUDE.md",
                         ".claude/skills/parametric-cad-design/SKILL.md",
                         ".claude/skills/parametric-cad-design/playbooks/plan.md"):
            path = os.path.join(REPO_ROOT, *artifact.split("/"))
            assert os.path.isfile(path), f"{artifact} is not where this lint looks for it"
            assert path in watched, f"{artifact} is generated but not fingerprinted"

    def test_every_generator_script_is_fingerprinted(self):
        watched = set(_fingerprinted_paths())
        for script in ("gen_all.py", "gen_manifest.py", "gen_wiring.py", "gen_posture.py",
                       "gen_api_surface.py", "gen_guidance.py", "conftest.py"):
            path = os.path.join(TESTS_DIR, script)
            assert path in watched, f"tests/{script} decides the output and must be fingerprinted"

    def test_the_whole_tool_tree_is_fingerprinted(self):
        """gen_wiring parses every tool module, so any one of them can stale TOOL_POINTER_MAP."""
        watched = set(_fingerprinted_paths())
        tools = glob.glob(os.path.join(_INPUT_TREE, "tools", "*.py"))
        assert len(tools) > 150, f"only {len(tools)} tool modules found - the tree moved"
        missing = sorted(p for p in tools if p not in watched)
        assert not missing, f"tool modules outside the fingerprint: {missing[:5]}"
