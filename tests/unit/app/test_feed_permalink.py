"""Unit tests for _post_permalink_from_card — reading real /feed/update/ permalinks off SDUI cards."""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def _fn():
    # Lazy import: importing run_automation at module scope instantiates the OpenAI client at
    # collection time, which fails in CI (no OPENAI_API_KEY). Match the codebase's in-test pattern.
    from cqc_lem.app.run_automation import _post_permalink_from_card
    return _post_permalink_from_card


def _anchor(href):
    a = MagicMock()
    a.get_attribute.return_value = href
    return a


class TestPostPermalinkFromCard:
    def test_returns_normalized_permalink_stripping_query(self):
        card = MagicMock()
        card.find_elements.return_value = [
            _anchor("https://www.linkedin.com/feed/update/urn:li:ugcPost:123/?utm=1&x=2")
        ]
        assert _fn()(card) == "https://www.linkedin.com/feed/update/urn:li:ugcPost:123/"

    def test_appends_trailing_slash_when_missing(self):
        card = MagicMock()
        card.find_elements.return_value = [
            _anchor("https://www.linkedin.com/feed/update/urn:li:ugcPost:456")
        ]
        assert _fn()(card) == "https://www.linkedin.com/feed/update/urn:li:ugcPost:456/"

    def test_returns_none_when_no_anchor(self):
        card = MagicMock()
        card.find_elements.return_value = []
        assert _fn()(card) is None

    def test_returns_none_on_exception(self):
        card = MagicMock()
        card.find_elements.side_effect = RuntimeError("stale element")
        assert _fn()(card) is None
