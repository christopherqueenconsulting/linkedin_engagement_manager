"""Unit tests for feed post recency + social-count extraction."""

import pytest
from unittest.mock import MagicMock

pytestmark = pytest.mark.unit


class TestPostAgeMinutes:
    def _driver(self, token):
        d = MagicMock(); d.execute_script.return_value = token
        return d

    @pytest.mark.parametrize("token,expected", [
        ("now", 0), ("45m •", 45), ("3h •", 180), ("5d •", 7200),
        ("2w •", 20160), ("10mo •", 432000), ("1h", 60),
    ])
    def test_parses_relative_age(self, token, expected):
        from cqc_lem.app.run_automation import _post_age_minutes
        assert _post_age_minutes(self._driver(token), MagicMock()) == expected

    def test_none_when_no_timestamp(self):
        from cqc_lem.app.run_automation import _post_age_minutes
        assert _post_age_minutes(self._driver(None), MagicMock()) is None


class TestPostSocialCounts:
    def _card(self, text):
        c = MagicMock(); c.text = text
        return c

    def test_parses_comments_and_reactions(self):
        from cqc_lem.app.run_automation import _post_social_counts
        card = self._card("Jane Doe\n3h •\nGreat post body\n1,204 reactions\n8 comments • 2 reposts")
        assert _post_social_counts(card) == {"reactions": 1204, "comments": 8}

    def test_zero_when_no_counts(self):
        from cqc_lem.app.run_automation import _post_social_counts
        assert _post_social_counts(self._card("Just a fresh post with no engagement yet")) == {"reactions": 0, "comments": 0}
