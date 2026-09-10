# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The opt-in cloud tier's operator config: the local file naming the hub, project and folder.

No hub or project name is ever written in the repo. An operator who wants the cloud and drawing
acts driven writes `cloud_config.local.json` (gitignored) beside this module; with it absent or
incomplete `load_config` returns the problem as one sentence, the cloud_tier probe answers false,
and every row of the tier lands in the receipt's skipped bucket instead of touching anyone's data.
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))

CONFIG_NAME = "cloud_config.local.json"
CONFIG_PATH = os.path.join(_HERE, CONFIG_NAME)

# The three strings a run of the tier needs, and the shape every refusal quotes back.
KEYS = ("hub", "project", "folder")
CONFIG_SHAPE = ('{"hub": "<the hub name data_get publishes as active_hub>", '
                '"project": "<a project in that hub>", '
                '"folder": "<the folder path under it the acts build in>"}')


def load_config(path=None):
    """(config, problem): the three named strings stripped, or (None, one sentence saying why not).

    The problem sentence names the FILE and the shape it holds, because it is what an operator
    reading a skipped receipt row has to act on."""
    path = path or CONFIG_PATH
    if not os.path.isfile(path):
        return None, (f"{path} does not exist - the cloud tier is opt-in. Write it holding "
                      f"{CONFIG_SHAPE} to drive the cloud and drawing acts against that hub.")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as e:
        return None, f"{path} would not read as JSON ({e}) - it holds {CONFIG_SHAPE}."
    if not isinstance(raw, dict):
        return None, (f"{path} holds a {type(raw).__name__}, not an object - it holds "
                      f"{CONFIG_SHAPE}.")
    missing = [k for k in KEYS if not str(raw.get(k) or "").strip()]
    if missing:
        return None, (f"{path} names no {', '.join(missing)} - it holds {CONFIG_SHAPE}.")
    return {k: str(raw[k]).strip() for k in KEYS}, None


# What the config held when this module was imported, so an act's rows can be written as plain
# dicts. Every one of them is gated by the cloud_tier capability, which reads the same file - so an
# unconfigured run carries these empty strings into no wire call at all.
CONFIG = load_config()[0] or {}
HUB = CONFIG.get("hub", "")
PROJECT = CONFIG.get("project", "")
FOLDER = CONFIG.get("folder", "")
