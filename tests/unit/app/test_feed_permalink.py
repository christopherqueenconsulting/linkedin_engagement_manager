"""Unit tests for _post_permalink_from_card — reading real /feed/update/ permalinks off SDUI cards."""

import pytest
from unittest.mock import MagicMock

from cqc_lem.app.run_automation import _post_permalink_from_card


def _anchor(href):
    a = MagicMock()
    a.get_attribute.return_value = href
    return a


@pytest.mark.unit
class TestPostPermalinkFromCard:
    def test_returns_normalized_permalink_stripping_query(self):
        card = MagicMock()
        card.find_elements.return_value = [
            _anchor("https://www.linkedin.com/feed/update/urn:li:ugcPost:123/?utm=1&x=2")
        ]
        assert (
            _post_permalink_from_card(card)
            == "https://www.linkedin.com/feed/update/urn:li:ugcPost:123/"
        )

    def test_appends_trailing_slash_when_missing(self):
        card = MagicMock()
        card.find_elements.return_value = [
            _anchor("https://www.linkedin.com/feed/update/urn:li:ugcPost:456")
        ]
        assert (
            _post_permalink_from_card(card)
            == "https://www.linkedin.com/feed/update/urn:li:ugcPost:456/"
        )

    def test_returns_none_when_no_anchor(self):
        card = MagicMock()
        card.find_elements.return_value = []
        assert _post_permalink_from_card(card) is None

    def test_returns_none_on_exception(self):
        card = MagicMock()
        card.find_elements.side_effect = RuntimeError("stale element")
        assert _post_permalink_from_card(card) is None
