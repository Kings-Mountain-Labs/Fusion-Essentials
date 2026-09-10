# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Packaged static guidance: the authored JSON document, the loader, and the one render of it.

The document is DATA, read three ways from this one file: ``loader`` serves its records to
``sys_get_guidance``, ``render`` turns it into the Markdown that ``resources`` publishes as an MCP
resource, and ``gen_guidance.py`` writes that same Markdown into the checked-in Claude skill. Every
surface therefore states what one file says. Nothing here imports ``adsk``: this package knows
about a file, not about Fusion.
"""
