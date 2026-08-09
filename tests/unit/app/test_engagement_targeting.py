"""Unit tests for post_matches_preferences (engagement targeting)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_FEED = "cqc_lem.app.engagement.feed"


def _prefs(**kw):
    base = {"exclude_keywords": [], "exclude_topics": [], "exclude_authors": [],
            "include_keywords": [], "include_authors": [], "include_topics": []}
    base.update(kw)
    return base


class TestPostMatchesPreferences:
    def test_no_constraints_matches(self):
        from cqc_lem.app.engagement.feed import post_matches_preferences
        assert post_matches_preferences("anything at all", "Jane", _prefs()) is True

    def test_exclude_keyword_blocks(self):
        from cqc_lem.app.engagement.feed import post_matches_preferences
        assert post_matches_preferences("a Crypto scam post", "Jane", _prefs(exclude_keywords=["crypto"])) is False

    def test_exclude_author_blocks(self):
        from cqc_lem.app.engagement.feed import post_matches_preferences
        assert post_matches_preferences("great post", "Spammy Bob", _prefs(exclude_authors=["spammy"])) is False

    def test_include_keyword_required_and_matches(self):
        from cqc_lem.app.engagement.feed import post_matches_preferences
        assert post_matches_preferences("all about AI agents", "Jane", _prefs(include_keywords=["ai"])) is True

    def test_include_keyword_required_no_match(self):
        from cqc_lem.app.engagement.feed import post_matches_preferences
        assert post_matches_preferences("gardening tips", "Jane", _prefs(include_keywords=["ai"])) is False

    def test_include_topics_uses_llm_when_literal_misses(self):
        from cqc_lem.app.engagement.feed import post_matches_preferences
        with patch(f"{_FEED}.post_is_relevant", return_value=True) as rel:
            assert post_matches_preferences("a post about ML", "Jane", _prefs(include_topics=["AI"])) is True
        rel.assert_called_once()

    def test_include_topics_llm_says_no(self):
        from cqc_lem.app.engagement.feed import post_matches_preferences
        with patch(f"{_FEED}.post_is_relevant", return_value=False):
            assert post_matches_preferences("gardening", "Jane", _prefs(include_topics=["AI"])) is False

    def test_literal_include_short_circuits_llm(self):
        from cqc_lem.app.engagement.feed import post_matches_preferences
        with patch(f"{_FEED}.post_is_relevant") as rel:
            assert post_matches_preferences("about ai stuff", "Jane",
                                            _prefs(include_keywords=["ai"], include_topics=["X"])) is True
        rel.assert_not_called()

    def test_exclude_wins_over_include(self):
        from cqc_lem.app.engagement.feed import post_matches_preferences
        assert post_matches_preferences("ai and crypto", "Jane",
                                        _prefs(include_keywords=["ai"], exclude_keywords=["crypto"])) is False
