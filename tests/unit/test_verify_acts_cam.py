# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The parse the unlocked-row verdict stands on, and the operation-address verdict beside it."""

import os
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TESTS_DIR, "live"))
import verify_acts_cam  # noqa: E402


class TestLeadingNumber:
    def test_a_read_back_states_its_unit_and_a_wordy_one_carries_no_number(self):
        assert verify_acts_cam._leading_number("0.5mm") == 0.5
        assert verify_acts_cam._leading_number("3") == 3.0
        assert verify_acts_cam._leading_number("-2.5deg") == -2.5
        assert verify_acts_cam._leading_number(".5mm") == 0.5
        assert verify_acts_cam._leading_number("true") is None
        assert verify_acts_cam._leading_number(None) is None


class TestReadsBack:
    def test_a_number_survives_its_unit_and_a_word_is_compared_as_text(self):
        assert verify_acts_cam._reads_back("0.5mm", "0.5mm") is True
        assert verify_acts_cam._reads_back("0.5mm", 0.5) is True
        assert verify_acts_cam._reads_back("10.5mm", "0.5mm") is False
        assert verify_acts_cam._reads_back("3", 3) is True
        assert verify_acts_cam._reads_back("true", "true") is True
        assert verify_acts_cam._reads_back("false", "true") is False


class TestParamValue:
    def test_a_length_read_back_states_its_unit_and_a_wrong_number_still_fails(self):
        landed = verify_acts_cam._param_value("stockToLeave", 0.5)
        assert landed({"edited": True,
                       "changed": [{"name": "stockToLeave", "after": "0.5 mm"}]}) is True
        with pytest.raises(AssertionError):
            landed({"edited": True,
                    "changed": [{"name": "stockToLeave", "after": "10.5 mm"}]})


def _ops_payload(*rows):
    """cam_get(include=['operations']) shaped down to what the address verdict reads."""
    return {"operations": {"setups": [{"setup": "CamJob", "operations": list(rows)}]}}


class TestPathsAddressEveryOperation:
    """Either half alone admits the ambiguity the verdict exists to refuse, so both are pinned."""

    VERDICT = staticmethod(verify_acts_cam._paths_address_every_operation("CamJob"))

    def test_a_foldered_row_and_a_root_row_each_carry_their_own_full_path(self):
        assert self.VERDICT(_ops_payload(
            {"name": "Face1", "path": "CamJob / Face1"},
            {"name": "Drill1", "folder": "Drilling", "path": "CamJob / Drilling / Drill1"})) is True

    def test_two_rows_sharing_a_name_fail_even_with_every_path_well_formed(self):
        # the ordinal '<name>#<n>' address this verdict refuses: both paths are correct for their
        # own row, and the pair is still unaddressable.
        with pytest.raises(AssertionError, match="duplicate_names"):
            self.VERDICT(_ops_payload(
                {"name": "Face1", "path": "CamJob / Face1"},
                {"name": "Face1", "folder": "Drilling", "path": "CamJob / Drilling / Face1"}))

    def test_a_path_that_drops_its_folder_segment_fails(self):
        with pytest.raises(AssertionError, match="mismatched"):
            self.VERDICT(_ops_payload(
                {"name": "Drill1", "folder": "Drilling", "path": "CamJob / Drill1"}))

    def test_a_setup_with_no_operations_fails_rather_than_passing_vacuously(self):
        with pytest.raises(AssertionError):
            self.VERDICT(_ops_payload())
