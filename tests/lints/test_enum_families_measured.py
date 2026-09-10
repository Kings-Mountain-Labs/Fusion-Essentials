# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: every adsk enum family the tools reference is MEASURED - first contact fails loudly.

A referenced family missing from BOTH live_api_facts.ENUMS and NOT_ENUMS fails, as does a BEHAVIOR
key the harness consumes or measure_api emits that the facts file does not carry - regenerate with
measure_api."""

import os
import re
import sys

import live_api_facts

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TESTS_DIR, "live"))
import measure_api  # noqa: E402  the scraper's home - one scraper, two consumers

_BEHAVIOR_KEY = re.compile(r"BEHAVIOR\[\s*\"([a-z0-9_]+)\"\s*\]")


class TestEnumFamiliesMeasured:
    def test_every_referenced_family_is_measured(self):
        # Either table counts as measured: ENUMS carries a family's int members, NOT_ENUMS names a
        # family that resolved to a factory-object class carrying none.
        measured = set(live_api_facts.ENUMS) | set(live_api_facts.NOT_ENUMS)
        missing = [f for f in measure_api.referenced_enum_families() if f not in measured]
        assert not missing, (
            "Tool code references adsk enum families that live_api_facts.py has not MEASURED. "
            "With Fusion running: py -3 tests/live/measure_api.py  (the enum-sweep measures "
            "them automatically) - then commit the regenerated file. Missing: "
            + ", ".join(missing))

    def test_every_consumed_behavior_key_is_measured(self):
        # A PENDING key is read through an `in` gate, so the harness imports without it; the
        # staleness check below is what stops the excuse outliving the regen.
        missing = sorted(_consumed_behavior_keys() - set(live_api_facts.BEHAVIOR)
                         - set(_PENDING_REGEN))
        assert not missing, (
            "The harness consumes BEHAVIOR keys live_api_facts.py does not carry - add a "
            "measurement row (facts_on_pass or a FACT line) to tests/live/measure_api.py and "
            "regenerate against live Fusion, or a reasoned _PENDING_REGEN entry until that run "
            "happens. Missing: " + ", ".join(missing))

    def test_every_flag_a_shared_fake_stands_on_is_read_by_it(self):
        # The other direction, for the rows that name a SHARED fake as what encodes them: the
        # flag they measured must be READ by the shared fakes, or the fake still hard-codes the
        # belief the row exists to check and a changed answer on a new build changes nothing.
        unread = sorted(_shared_fake_flags() - _consumed_behavior_keys(shared_fakes_only=True)
                        - set(_UNCONSUMED_OK))
        assert not unread, (
            "measurement rows that name a shared fake as their encoding emit flags the "
            "shared fakes never read - make the fake read BEHAVIOR[\"<key>\"] instead of "
            "hard-coding the behaviour, or add a reasoned _UNCONSUMED_OK entry: "
            + ", ".join(unread))

    def test_unconsumed_ok_entries_still_trip(self):
        consumed = _consumed_behavior_keys(shared_fakes_only=True)
        stale = []
        for key, reason in _UNCONSUMED_OK.items():
            assert reason.strip(), f"{key} _UNCONSUMED_OK entry needs a plain-English reason"
            if key not in _shared_fake_flags():
                stale.append(f"{key}: no row naming a shared fake emits it - remove the entry")
            elif key in consumed:
                stale.append(f"{key}: a shared fake reads it now - remove the entry")
        assert not stale, "stale _UNCONSUMED_OK entries:\n  " + "\n  ".join(stale)


def _shared_fake_flags():
    """Behavior keys emitted by rows whose encoded_in names a shared-fake file - tests/conftest.py
    or a module of tests/fakes/."""
    keys = set()
    for row in measure_api.ROWS:
        encoded_in = row.get("encoded_in", "")
        if "conftest" not in encoded_in and "tests/fakes" not in encoded_in:
            continue
        for key in (row.get("facts_on_pass") or {}):
            if key.startswith("behavior."):
                keys.add(key[len("behavior."):])
        body = row["body_fn"]() if "body_fn" in row else row.get("body", "")
        keys |= set(measure_api._FACT_BEHAVIOR_PRINT.findall(body))
    return keys


def _consumed_behavior_keys(shared_fakes_only=False):
    """Every BEHAVIOR["<key>"] read in the harness - all of tests/ (minus live/), or the shared
    fakes alone: the fakes package plus conftest.py, which keeps the harness's own factories."""
    consumed = set()
    for root, dirs, files in os.walk(TESTS_DIR):
        keep = ["fakes"] if shared_fakes_only else [d for d in dirs
                                                    if d not in ("__pycache__", "live")]
        dirs[:] = [d for d in dirs if d in keep]
        for fn in files:
            if shared_fakes_only and fn != "conftest.py" and os.path.basename(root) != "fakes":
                continue
            if fn.endswith(".py") and fn != "live_api_facts.py":
                with open(os.path.join(root, fn), encoding="utf-8") as fh:
                    consumed |= set(_BEHAVIOR_KEY.findall(fh.read()))
    return consumed


# Flags a shared fake deliberately does not read yet. Shrink-only; each names why.
_UNCONSUMED_OK = {}


# Emitted keys the generated facts file does not carry YET - each is a measurement-row rename or
# addition awaiting the next live regen (a fully-PASSING py -3 tests/live/measure_api.py rewrites
# BEHAVIOR and empties this table), and a harness read of one goes through an `in` gate meanwhile.
# Shrink-only; the staleness check below fails the moment the regen lands the key.
_PENDING_REGEN = {}


def _uncarried_emitted_keys(emitted, carried, pending):
    """Emitted behavior keys that live_api_facts.BEHAVIOR does not carry and no pending-regen
    entry excuses."""
    return sorted(set(emitted) - set(carried) - set(pending))


class TestEmittedBehaviorKeysAreCarried:
    def test_every_emitted_behavior_key_is_carried(self):
        missing = _uncarried_emitted_keys(measure_api.emitted_behavior_keys(),
                                          live_api_facts.BEHAVIOR, _PENDING_REGEN)
        assert not missing, (
            "measure_api.py can emit behavior keys the generated live_api_facts.BEHAVIOR does not "
            "carry - a renamed/new flag in a measurement row needs a live regen (py -3 "
            "tests/live/measure_api.py with Fusion up, commit the regenerated file), or a "
            "reasoned _PENDING_REGEN entry until that run happens. Missing: " + ", ".join(missing))

    def test_pending_regen_entries_are_still_pending(self):
        emitted = set(measure_api.emitted_behavior_keys())
        stale = []
        for key, reason in _PENDING_REGEN.items():
            assert reason.strip(), f"{key} _PENDING_REGEN entry needs a plain-English reason"
            if key not in emitted:
                stale.append(f"{key}: measure_api no longer emits it - remove the entry")
            elif key in live_api_facts.BEHAVIOR:
                stale.append(f"{key}: the regen landed it in BEHAVIOR - remove the entry")
        assert not stale, "stale _PENDING_REGEN entries:\n  " + "\n  ".join(stale)
