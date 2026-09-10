# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Read each corpus file once, parse it once - the shared I/O behind the lints in this directory.

Memoized per path for ONE pytest process, so a test that writes a file then scans it must use a
fresh path; tree() hands every caller the SAME AST object, which they walk read-only."""

import ast
import os
from functools import lru_cache
from pathlib import Path


def text(path):
    """The file's source, decoded UTF-8. Accepts a str or a Path (the same file either way)."""
    return _text(os.fspath(path))


def tree(path):
    """The file's parsed AST - shared, so walk it read-only (see the module docstring)."""
    return _tree(os.fspath(path))


def py_files(root):
    """Every .py file under `root` as Paths, __pycache__ excluded. A tuple, so a caller sorting or
    filtering it cannot disturb the cached listing."""
    return _py_files(os.fspath(root))


@lru_cache(maxsize=None)
def _text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@lru_cache(maxsize=None)
def _tree(path):
    return ast.parse(_text(path), filename=path)


@lru_cache(maxsize=None)
def _py_files(root):
    return tuple(p for p in Path(root).rglob("*.py") if "__pycache__" not in p.parts)
