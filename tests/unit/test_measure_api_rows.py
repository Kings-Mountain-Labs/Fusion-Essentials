# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The measurement registry's shape: every ROW composes into a script Fusion can run.

A row's body is source text that only ever executes inside live Fusion, so a syntax error in one
reaches the run as an ERROR row hours later - and a duplicate id silently overwrites a claim's
ledger cell. Both are decidable offline, against the same _compose the runner calls.
"""

import os
import sys
import textwrap
from types import SimpleNamespace

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "live"))
import measure_api  # noqa: E402


class TestRowRegistry:
    def test_every_row_composes_into_compilable_python(self):
        broken = []
        for row in measure_api.ROWS:
            try:
                compile(measure_api._compose(row), row["id"], "exec")
            except SyntaxError as e:
                broken.append(f"{row['id']}: line {e.lineno}: {e.msg}")
        assert not broken, (
            "measurement row bodies that do not compile - they would reach live Fusion as ERROR "
            "rows:\n  " + "\n  ".join(broken))

    def test_row_ids_are_unique(self):
        seen, dupes = set(), []
        for row in measure_api.ROWS:
            if row["id"] in seen:
                dupes.append(row["id"])
            seen.add(row["id"])
        assert not dupes, "duplicate measurement row ids: " + ", ".join(sorted(set(dupes)))

    def test_every_row_carries_a_claim_and_an_encoding(self):
        # the two columns write_ledger publishes - a row missing either lands a blank ledger cell
        thin = [row["id"] for row in measure_api.ROWS
                if not str(row.get("claim", "")).strip()
                or not str(row.get("encoded_in", "")).strip()]
        assert not thin, "rows with an empty claim or encoded_in: " + ", ".join(thin)


_METRIC_DESIGNATIONS = ("M5x0.8", "M10x1.5", "M6x1")
_METRIC_TYPES = ("ANSI Metric M Profile", "GB Metric profile", "ISO Metric profile")
_UNC_DESIGNATION = "1/4-20 UNC"
_UNC_TYPE = "ANSI Unified Screw Threads"


class _ThreadQuery:
    """ThreadDataQuery double for the unmeasured query surface used by this row."""

    def __init__(self, classes):
        self.classes = classes
        self.allThreadTypes = list(_METRIC_TYPES) + [_UNC_TYPE]

    def allSizes(self, thread_type):
        return ["size"]

    def allDesignations(self, thread_type, _size):
        return [desig for desig, carried_by in self.classes if carried_by == thread_type]

    def allClasses(self, _internal, thread_type, designation):
        return list(self.classes.get((designation, thread_type), ()))


class _ThreadFeatures:
    """ThreadFeatures double for its unmeasured thread-info creation surface."""

    def __init__(self, query):
        self.threadDataQuery = query
        self.create_calls = []

    def createThreadInfo(self, _internal, thread_type, designation, thread_class):
        allowed = set(self.threadDataQuery.allClasses(False, thread_type, designation))
        assert thread_class in allowed, (
            f"createThreadInfo received noncommon class {thread_class!r} for {thread_type!r}")
        self.create_calls.append((thread_type, designation, thread_class))
        return SimpleNamespace(
            majorDiameter=1.0, minorDiameter=0.8, pitchDiameter=0.9,
            threadPitch=0.1, threadAngle=60.0)


def _thread_classes(per_type):
    classes = {(_UNC_DESIGNATION, _UNC_TYPE): ("2A",)}
    for designation in _METRIC_DESIGNATIONS:
        for thread_type, carried in zip(_METRIC_TYPES, per_type):
            classes[(designation, thread_type)] = tuple(carried)
    return classes


def _run_thread_identity_row(classes):
    row = next(row for row in measure_api.ROWS
               if row["id"] == "thread-designation-multi-type-identity")
    query = _ThreadQuery(classes)
    features = _ThreadFeatures(query)
    des = SimpleNamespace(rootComponent=SimpleNamespace(
        features=SimpleNamespace(threadFeatures=features)))
    emitted = []
    exec(textwrap.dedent(row["body"]), {
        "des": des,
        "emit": lambda passed, detail: emitted.append((passed, detail)),
    })
    assert len(emitted) == 1
    return emitted[0], features.create_calls


class TestThreadDesignationMultiTypeIdentity:
    def test_disjoint_early_carriers_are_not_reset_by_a_later_carrier(self):
        (passed, detail), calls = _run_thread_identity_row(
            _thread_classes((("ClassA",), ("ClassB",), ("ClassC",))))
        assert passed is False
        assert calls == []
        assert all(designation in detail for designation in _METRIC_DESIGNATIONS)

    def test_a_truly_shared_class_is_used_for_every_carrier(self):
        (passed, _detail), calls = _run_thread_identity_row(
            _thread_classes((("Shared", "A"), ("Shared", "B"), ("Shared", "C"))))
        assert passed is True
        assert len(calls) == 9
        assert {call[2] for call in calls} == {"Shared"}

    def test_an_empty_first_carrier_keeps_the_intersection_empty(self):
        (passed, _detail), calls = _run_thread_identity_row(
            _thread_classes(((), ("Shared",), ("Shared",))))
        assert passed is False
        assert calls == []

    def test_absent_metric_carriers_fail_without_creating_thread_info(self):
        classes = {(_UNC_DESIGNATION, _UNC_TYPE): ("2A",)}
        (passed, detail), calls = _run_thread_identity_row(classes)
        assert passed is False
        assert calls == []
        assert all(designation in detail for designation in _METRIC_DESIGNATIONS)
