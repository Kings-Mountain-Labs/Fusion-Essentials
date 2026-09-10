# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Run every doc generator (or every --check) in ONE process.

Each generator pays a full mocked-registry load when run as its own process; importing them here
means that load happens once, so the staleness check stays fast. Same contracts as running them
individually: writes tests/generated/* (plus the CLAUDE.md spliced maps and the guidance-generated
Claude skill), or with --check exits 1 naming what is stale.

    py -3 tests/gen_all.py           # regenerate everything
    py -3 tests/gen_all.py --check   # exit 1 if any generated artifact is stale
"""

import argparse
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

_GENERATORS = ("gen_manifest", "gen_wiring", "gen_posture", "gen_guidance", "gen_api_surface",
               "gen_strategies", "gen_enforcement")


def exit_code(value):
    """The exit code a generator's verdict stands for - its main()'s RETURN value or its
    SystemExit's code, judged by one rule.

    None is success: a generator that exits by SystemExit, or simply returns nothing, has reported
    no failure. A bool is a freshness verdict and False is STALE - checked before int, because bool
    IS an int subclass and False would otherwise read as exit code 0. Any other int is its own exit
    code. Anything else follows SystemExit's own convention: a non-empty object is a failure message,
    so it counts as 1.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0 if value else 1
    if isinstance(value, int):
        return value
    return 1 if value else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any generated artifact is stale (writes nothing)")
    args = ap.parse_args()
    worst, failed = 0, []
    for name in _GENERATORS:
        mod = __import__(name)
        sys.argv = [name + ".py"] + (["--check"] if args.check else [])
        try:
            # The RETURN value is a verdict too. Discarding it - reading only SystemExit - means a
            # generator that reports failure by returning a non-zero code can never fail
            # `gen_all --check`, and the stale doc it found is aggregated as a pass.
            code = exit_code(mod.main())
        except SystemExit as e:
            code = exit_code(e.code)
        if code:
            failed.append(f"{name} (exit {code})")
            worst = worst or code
    if failed:
        print("gen_all: FAILED - " + ", ".join(failed), file=sys.stderr)
    return worst


if __name__ == "__main__":
    sys.exit(main())
