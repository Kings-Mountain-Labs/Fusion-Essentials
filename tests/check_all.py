# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The one command: every generator check, the whole suite, and the live gate, in order.

Stages (each fails loudly with the command that repairs it):
  1. generators --check   (TOOL_MANIFEST / TOOL_POINTER_MAP stay in sync with the code)
  2. pytest               (unit tests + lints)
  3. tool_verify --check  (the receipt: the last green live run saw exactly this tool source;
                           recomputed offline, no Fusion needed)
  4. live gate            (Fusion reachable -> facts stamp check; --live -> full measurement run)

Green comes in two honest flavors: LIVE-VERIFIED (stage 4 ran against a reachable Fusion) and
OFFLINE (--offline was passed; the mocks were NOT re-confirmed against the installed Fusion).
A missing Fusion without --offline is a FAILURE - skipping live verification is always a visible
choice, never an accident. For the same reason --live and --offline are refused together (one of
them would have to be discarded silently).

Usage:
  py -3 tests/check_all.py                 # generators + suite + receipt + facts stamp check
  py -3 tests/check_all.py --live          # ...with a FULL measurement run (regenerates facts)
  py -3 tests/check_all.py --offline       # no Fusion here: skip the live gate, visibly
  py -3 tests/check_all.py --fast          # generators + lints only
  py -3 tests/check_all.py --gen           # generator checks only

The quality system, layer by layer (the one guarantee each makes):
  constitution docs      the rules, taught once at the point of use (CLAUDE.md beside the code)
  unit tests             tool logic proven against fakes - which are POPULATED from measured facts
  lints                  the repo polices its own conventions (naming, wire text, comments, fakes)
  generators             TOOL_MANIFEST / TOOL_POINTER_MAP regenerate from code; gen_all --check gates
  api measurement        live Fusion MEASURES enum values + behavior flags -> live_api_facts.py
  facts + guard lints    mocks seeded from measurement; a hand-typed or unmeasured API claim is red
  tool verify + evals    every tool called once against real Fusion, receipted in VERIFIED_TOOLS.md
                         (source-hash stamp; --check = stale gate); cold-agent scenario evals
"""

import argparse
import os
import subprocess
import sys
import urllib.request

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
HEALTH = "http://127.0.0.1:27182/health"


def _run(label, cmd, repair):
    # flush before handing the console to the child, so stage headers precede child output
    print("== " + label, flush=True)
    rc = subprocess.run(cmd, cwd=REPO).returncode
    if rc != 0:
        print("\nFAILED at: " + label, flush=True)
        print("repair:    " + repair, flush=True)
    return rc == 0


def _fusion_up():
    try:
        with urllib.request.urlopen(HEALTH, timeout=3) as resp:
            return b"Fusion-Essentials" in resp.read()
    except Exception:
        return False


def build_parser():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # --live RUNS the live gate and --offline SKIPS it, so together one flag must be discarded.
    # Silently following --offline would print an OFFLINE GREEN for a run the caller asked to be
    # live-verified - the opposite of "skipping live verification is always a visible choice".
    # A mutually exclusive group refuses the pair up front, naming both flags.
    gate = ap.add_mutually_exclusive_group()
    gate.add_argument("--live", action="store_true",
                      help="run the FULL live measurement (regenerates facts + ledger + stamp)")
    gate.add_argument("--offline", action="store_true",
                      help="no Fusion available: skip the live gate, visibly (refused with --live)")
    ap.add_argument("--fast", action="store_true",
                    help="generators + lints only")
    ap.add_argument("--gen", action="store_true",
                    help="generator staleness checks only")
    return ap


def main():
    args = build_parser().parse_args()

    if not _run("gen_all --check", [sys.executable, os.path.join(TESTS, "gen_all.py"), "--check"],
                "py -3 tests/gen_all.py   (then commit the regenerated files)"):
        return 1
    if args.gen:
        print("\nGEN GREEN: every generated artifact is current. Run the full button before "
              "calling the change done.")
        return 0

    pytest_cmd = [sys.executable, "-m", "pytest", "-q"]
    if args.fast:
        pytest_cmd.append(os.path.join("tests", "lints"))
    if not _run("pytest" + (" (lints only)" if args.fast else ""), pytest_cmd,
                "read the failure above - a lint names its own repair; a unit test names the "
                "behavior that changed"):
        return 1

    if args.fast:
        print("\nFAST GREEN: generators current + lints pass. Run the full button before "
              "calling the change done.")
        return 0

    verify = os.path.join(TESTS, "live", "tool_verify.py")
    if not _run("tool_verify --check", [sys.executable, verify, "--check"],
                "py -3 tests/live/tool_verify.py   (Fusion up - a green run rewrites "
                "VERIFIED_TOOLS.md; --run <id> / --resume walks it in chunks)"):
        return 1

    if args.offline:
        print("\nOFFLINE GREEN: generators current, suite green, and the live-run receipt matches")
        print("this tool source - but the mocks were NOT re-confirmed against the installed")
        print("Fusion. Re-run without --offline (Fusion up, add-in enabled) before releasing.")
        return 0
    if not _fusion_up():
        print("\nLIVE GATE FAILED: Fusion is not reachable on 127.0.0.1:27182.")
        print("Start Fusion with the add-in enabled and re-run - or pass --offline to accept an")
        print("unverified-mocks green, visibly.")
        return 1

    measure = os.path.join(TESTS, "live", "measure_api.py")
    measure_cmd = [sys.executable, measure] + ([] if args.live else ["--check"])
    if not _run("measure_api" + ("" if args.live else " --check"), measure_cmd,
                "py -3 tests/live/measure_api.py   (a full run refreshes the stamp)"):
        return 1

    print("\nLIVE-VERIFIED GREEN: generators current, suite green, the live-run receipt matches")
    print("this tool source, and the facts stamp matches the installed Fusion.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
