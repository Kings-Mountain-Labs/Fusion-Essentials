# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The opt-in cloud tier's gate: the local config, and the probe that reads it.

The tier writes into an operator's REAL Autodesk hub, so the only thing standing between a default
sweep and someone's data is this loader answering "no config" and the probe turning that into a
capability the runner holds every cloud act back on. Both halves are pinned here, plus the two
readings that are refusals rather than licences: a config naming a hub the session is not on, and a
config naming a project the hub does not list.
"""

import json
import os
import sys

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TESTS_DIR, "live"))
import cloud_config  # noqa: E402
import tool_verify  # noqa: E402
import verify_core  # noqa: E402


def _write(tmp_path, body):
    path = tmp_path / cloud_config.CONFIG_NAME
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return str(path)


_GOOD = {"hub": "Some Hub", "project": "Some Project", "folder": "Sweep"}


class TestLoadConfig:
    """What the loader accepts, and what its refusal has to say."""

    def test_a_complete_config_loads_the_three_strings_stripped(self, tmp_path):
        path = _write(tmp_path, {"hub": " Some Hub ", "project": "Some Project",
                                 "folder": "Sweep/Runs", "extra": "ignored"})
        config, problem = cloud_config.load_config(path)
        assert problem is None
        assert config == {"hub": "Some Hub", "project": "Some Project", "folder": "Sweep/Runs"}

    def test_a_missing_file_names_the_path_and_the_shape(self, tmp_path):
        path = str(tmp_path / cloud_config.CONFIG_NAME)
        config, problem = cloud_config.load_config(path)
        assert config is None
        assert path in problem and '"hub"' in problem and '"project"' in problem
        assert '"folder"' in problem

    def test_an_empty_or_absent_key_is_named_in_the_refusal(self, tmp_path):
        # A key present but BLANK is as unusable as one absent, and the operator has to be told
        # which - so the names are read out of the refusal's own clause, not out of the shape it
        # quotes afterwards (every key name appears in that, so matching there proves nothing).
        path = _write(tmp_path, {"hub": "Some Hub", "project": "   "})
        config, problem = cloud_config.load_config(path)
        assert config is None
        named = problem.split(" - it holds")[0]
        assert "project" in named and "folder" in named and "hub" not in named

    def test_unreadable_json_and_a_non_object_both_refuse(self, tmp_path):
        path = _write(tmp_path, "{not json")
        assert cloud_config.load_config(path)[0] is None
        path = _write(tmp_path, ["Some Hub"])
        config, problem = cloud_config.load_config(path)
        assert config is None and "not an object" in problem


class TestCloudTierProbe:
    """The probe: config first, then the hub the session is actually on."""

    @staticmethod
    def _wire(monkeypatch, answer, folder=(False, {"file_count": 0, "files": []})):
        """The two reads the probe takes: the hub-level data_get, then the FOLDER one."""
        def call(tool, args):
            return folder if args.get("folder") else answer
        monkeypatch.setattr(tool_verify, "call", call)

    @staticmethod
    def _config(monkeypatch, config, problem=None):
        monkeypatch.setattr(cloud_config, "load_config",
                            lambda path=None: (config, problem))

    def test_no_config_is_a_definite_no_and_costs_no_wire_call(self, monkeypatch):
        # False, not None: "this machine is not configured for the tier" is a settled answer, and a
        # cloud read taken to establish it would be a network call for nothing.
        called = []
        monkeypatch.setattr(tool_verify, "call",
                            lambda tool, args: called.append(tool) or (False, {}))
        self._config(monkeypatch, None, "no config here")
        assert verify_core._cloud_tier_probe() is False
        assert called == []

    def test_the_configured_hub_and_project_are_entitled(self, monkeypatch):
        self._config(monkeypatch, dict(_GOOD))
        self._wire(monkeypatch, (False, {"active_hub": "Some Hub", "project_count": 2,
                                         "projects": [{"name": "Other"},
                                                      {"name": "Some Project"}]}))
        assert verify_core._cloud_tier_probe() is True

    def test_another_hub_is_refused_rather_than_switched_to(self, monkeypatch):
        # the whole safety property: a session signed in elsewhere must not have the tier run
        # against it, and data_switch_hub would close the story document to correct it.
        self._config(monkeypatch, dict(_GOOD))
        self._wire(monkeypatch, (False, {"active_hub": "Someone Else's Hub", "project_count": 1,
                                         "projects": [{"name": "Some Project"}]}))
        assert verify_core._cloud_tier_probe() is False

    def test_a_project_the_hub_does_not_list_is_refused(self, monkeypatch):
        self._config(monkeypatch, dict(_GOOD))
        self._wire(monkeypatch, (False, {"active_hub": "Some Hub", "project_count": 1,
                                         "projects": [{"name": "Other"}]}))
        assert verify_core._cloud_tier_probe() is False

    def test_a_folder_the_project_does_not_answer_with_is_refused(self, monkeypatch):
        # THE mkdir -p HAZARD: data_create_folder creates every missing segment of a parent path, so
        # a mistyped folder does not fail the act - it MINTS folders in the operator's hub, the
        # create's own auto_created_parents check reds, and the run's deletes never name them. The
        # destination is confirmed before any act can reach a create.
        self._config(monkeypatch, dict(_GOOD))
        self._wire(monkeypatch,
                   (False, {"active_hub": "Some Hub", "project_count": 1,
                            "projects": [{"name": "Some Project"}]}),
                   folder=(True, "Folder 'Sweep' not found: no subfolder 'Sweep' in '(project "
                                 "root)'. Subfolders there: Other."))
        assert verify_core._cloud_tier_probe() is False

    def test_the_folder_read_is_scoped_to_the_configured_project_and_folder(self, monkeypatch):
        seen = []
        self._config(monkeypatch, dict(_GOOD))

        def call(tool, args):
            seen.append((tool, dict(args)))
            if args.get("folder"):
                return False, {"file_count": 0, "files": []}
            return False, {"active_hub": "Some Hub", "project_count": 1,
                           "projects": [{"name": "Some Project"}]}
        monkeypatch.setattr(tool_verify, "call", call)
        assert verify_core._cloud_tier_probe() is True
        assert seen == [("data_get", {}),
                        ("data_get", {"project": "Some Project", "folder": "Sweep",
                                      "recursive": False})]

    def test_a_cloud_read_that_did_not_answer_is_unreadable_not_a_licence(self, monkeypatch):
        # None routes as unmet exactly as False does, but it says the hub was never compared -
        # reading it as False would report a verdict nothing measured.
        self._config(monkeypatch, dict(_GOOD))
        self._wire(monkeypatch, (True, "not signed in"))
        assert verify_core._cloud_tier_probe() is None
        self._wire(monkeypatch, (False, "a string, not a payload"))
        assert verify_core._cloud_tier_probe() is None


class TestTheTierIsWired:
    """The probe is reachable by the name the acts declare, and the acts declare it."""

    def test_the_capability_name_resolves_to_this_probe(self):
        assert (verify_core.CAPABILITY_PROBES[tool_verify.CLOUD_TIER]
                is verify_core._cloud_tier_probe)

    def test_every_cloud_act_declares_the_tier(self):
        cloud = [name for name, _p, _n, _f in tool_verify.ACTS if name.startswith("ACT 11")]
        assert len(cloud) == 3, cloud
        assert all(tool_verify.ACT_NEEDS.get(name) == tool_verify.CLOUD_TIER for name in cloud)

    def test_the_acts_address_the_project_the_loader_read(self):
        # a hub or project name never lives in the repo, so every step naming one has to be carrying
        # what the loader read - a literal here would address someone's hub on every run.
        import verify_acts_cloud as acts
        named = [step[1]["project"] for step in acts._CLOUD_DATA
                 if isinstance(step[1], dict) and "project" in step[1]]
        assert named, "no data act step names a project"
        assert set(named) == {cloud_config.PROJECT}
        assert acts.RUN_PATH.startswith(cloud_config.FOLDER + "/")

    def test_an_unconfigured_checkout_carries_no_hub_name(self):
        # the state a reviewer's checkout is in: no config file, so the module-level strings are
        # empty and the probe's False is what stands between the sweep and anyone's data.
        if os.path.isfile(cloud_config.CONFIG_PATH):
            return          # this machine IS configured - the assertion below is not its state
        assert (cloud_config.HUB, cloud_config.PROJECT, cloud_config.FOLDER) == ("", "", "")
