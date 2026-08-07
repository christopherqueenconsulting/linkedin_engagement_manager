"""Unit tests for load_profile_for_user."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

VALID = '{"full_name": "Jane Doe", "job_title": "CTO"}'
_TARGET = "cqc_lem.utilities.linkedin.helper.get_linked_in_profile_by_user_id"


class TestLoadProfileForUser:
    def test_none_when_no_row(self):
        with patch(_TARGET, return_value=None):
            from cqc_lem.utilities.linkedin.helper import load_profile_for_user
            assert load_profile_for_user(1) is None

    def test_parses_tuple_row(self):
        with patch(_TARGET, return_value=(VALID,)):
            from cqc_lem.utilities.linkedin.helper import load_profile_for_user
            p = load_profile_for_user(1)
            assert p is not None and p.full_name == "Jane Doe"

    def test_parses_plain_string(self):
        with patch(_TARGET, return_value=VALID):
            from cqc_lem.utilities.linkedin.helper import load_profile_for_user
            assert load_profile_for_user(1).job_title == "CTO"

    def test_bad_json_returns_none(self):
        with patch(_TARGET, return_value=("{not valid json",)):
            from cqc_lem.utilities.linkedin.helper import load_profile_for_user
            assert load_profile_for_user(1) is None
