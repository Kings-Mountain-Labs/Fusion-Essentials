# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.
#
# Adapted from Autodesk's Fusion MCP add-in sample (MIT-licensed).

"""MCP Annotations schema for tool metadata."""

from typing import Optional


class Annotations:
    """MCP Annotations schema; converts to JSON-friendly dicts."""

    def __init__(
        self,
        read_only: Optional[bool] = None,
        destructive: Optional[bool] = None
    ):
        # MCP tool behaviour hints. read_only: the tool does not modify state. destructive: a write
        # whose effect is hard to reverse (deletes, history-discarding conversions, closing docs).
        # Serialized as readOnlyHint / destructiveHint (the MCP spec names).
        self.read_only = read_only
        self.destructive = destructive

    def set_read_only(self, read_only: bool = True) -> 'Annotations':
        self.read_only = read_only
        return self

    def set_destructive(self, destructive: bool = True) -> 'Annotations':
        self.destructive = destructive
        return self

    def to_dict(self) -> dict:
        result = {}
        if self.read_only is not None:
            result['readOnlyHint'] = self.read_only
        if self.destructive is not None:
            result['destructiveHint'] = self.destructive
        return result

    def __str__(self) -> str:
        return f"Annotations({self.to_dict()})"

    def __repr__(self) -> str:
        return f"Annotations(read_only={self.read_only}, destructive={self.destructive})"
