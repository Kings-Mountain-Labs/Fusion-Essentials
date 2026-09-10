# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Gate: no tool renames an entity through a swallowed setattr - apply_rename is the one home.

The platform declines a rename SILENTLY and can land a DEDUPED variant ('Foo' -> 'Foo(1)'), so the
banned ``setattr(<entity>, "name", ...)`` publishes the REQUESTED name; ``entity.name = v`` raises."""

import re
from pathlib import Path

import _corpus

_TOOLS = Path(__file__).resolve().parents[2] / "commands" / "mcpServer" / "tools"

_SWALLOWED_RENAME = re.compile(r"""setattr\(\s*[^,]+,\s*["']name["']""")

# apply_rename's own home may spell the raw form; no tool module may.
_HOME = "_common.py"


class TestRenameAdoption:
    def test_no_tool_setattrs_a_name(self):
        offenders = []
        for path in sorted(_TOOLS.glob("*.py")):
            if path.name == _HOME:
                continue
            for i, line in enumerate(_corpus.text(path).splitlines(), 1):
                if _SWALLOWED_RENAME.search(line):
                    offenders.append(f"{path.name}:{i}: {line.strip()}")
        assert not offenders, (
            "swallowed-rename shape (setattr(<entity>, 'name', ...)) in a tool module - route it "
            "through _common.apply_rename (set + read-back + disclosed decline) and publish the "
            "returned final name + warning:\n  " + "\n  ".join(offenders))
