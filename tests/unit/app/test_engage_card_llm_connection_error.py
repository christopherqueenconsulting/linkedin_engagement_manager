"""A proxy connection error during comment generation skips the post, not the run (issue #2829)."""
from unittest.mock import MagicMock, patch

import httpx
from openai import APIConnectionError

from cqc_lem.app.engagement import feed as ra
from cqc_lem.domain.models import FeedRunContext

_FEED = "cqc_lem.app.engagement.feed"


def _connection_error() -> APIConnectionError:
    return APIConnectionError(request=httpx.Request("POST", "http://litellm:4000/chat/completions"))


def test_connection_error_releases_claim_and_returns_false():
    ctx = FeedRunContext(driver=MagicMock(), wait=MagicMock(), my_profile=MagicMock(),
                         user_id=1, prefs={}, profile_synthesis="synth")
    with patch(f"{_FEED}.claim_post_for_comment", return_value=True), \
         patch(f"{_FEED}.select_blueprint", return_value={"format": "expander"}), \
         patch(f"{_FEED}.generate_ai_response", side_effect=_connection_error()), \
         patch(f"{_FEED}.record_comment_shape") as shape, \
         patch(f"{_FEED}.post_comment_inline") as posted, \
         patch(f"{_FEED}.release_post_claim") as released:
        ok = ra._engage_card(ctx, MagicMock(), "feedurn://urn:li:activity:1", "a post body", "Jane")
    assert ok is False
    released.assert_called_once_with(1, "feedurn://urn:li:activity:1")
    shape.assert_not_called()
    posted.assert_not_called()
