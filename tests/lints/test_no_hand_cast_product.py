# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Lint: acquire the active Design / CAM product through the resolver, not a hand cast.

`Design.cast(...activeProduct...)` outside _common.py fails - it returns None when a CAM product is
active, so call `_common.design()`. `CAM.cast(...)` outside _cam_common.py fails - call get_cam()."""

import os
import re

import _corpus
from conftest import TOOLS_DIR

_DESIGN_CAST = re.compile(r"Design\.cast\([^)]*activeProduct")
_CAM_CAST = re.compile(r"\bCAM\.cast\(")


def _scan(regex, home):
    offenders = []
    for fn in sorted(os.listdir(TOOLS_DIR)):
        if not fn.endswith(".py") or fn == home:
            continue
        src = _corpus.text(os.path.join(TOOLS_DIR, fn))
        for i, line in enumerate(src.splitlines(), 1):
            if regex.search(line):
                offenders.append(f"{fn}:{i}: {line.strip()}")
    return offenders


class TestNoHandCastProduct:
    def test_no_design_cast_of_active_product(self):
        offenders = _scan(_DESIGN_CAST, "_common.py")
        assert not offenders, (
            "hand cast of the active product to a Design - call `_common.design()` (it falls back "
            "when a CAM product is active, which Design.cast(activeProduct) does not):\n  "
            + "\n  ".join(offenders))

    def test_no_manual_cam_cast(self):
        offenders = _scan(_CAM_CAST, "_cam_common.py")
        assert not offenders, (
            "hand cast to a CAM product - call `_cam_common.get_cam()` (the one shared CAM resolver):\n  "
            + "\n  ".join(offenders))
