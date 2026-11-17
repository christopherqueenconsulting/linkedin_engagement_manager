"""Permalink fallback when a home-feed card has no comment box (issue #2547)."""
from unittest.mock import MagicMock, patch

from cqc_lem.app.engagement import feed as ra
from cqc_lem.domain.models import FeedRunContext

_FEED = "cqc_lem.app.engagement.feed"
_URN_KEY = "feedurn://urn:li:activity:7123456789012345678"


def _ctx() -> FeedRunContext:
    return FeedRunContext(driver=MagicMock(), wait=MagicMock(), my_profile=MagicMock(),
                          user_id=1, prefs={}, profile_synthesis="synth")


def _engage(key: str, inline_side_effect, fallback_return=True):
    ctx = _ctx()
    with patch(f"{_FEED}.claim_post_for_comment", return_value=True), \
         patch(f"{_FEED}.select_blueprint", return_value={"format": "expander"}), \
         patch(f"{_FEED}.generate_ai_response", return_value="A real comment."), \
         patch(f"{_FEED}.record_comment_shape"), \
         patch(f"{_FEED}._author_is_me", return_value=False), \
         patch(f"{_FEED}.react_to_post_inline", return_value=True), \
         patch(f"{_FEED}.mark_post_reacted"), \
         patch(f"{_FEED}.post_comment_inline", side_effect=inline_side_effect), \
         patch(f"{_FEED}._permalink_fallback_comment", return_value=fallback_return) as fallback, \
         patch(f"{_FEED}.mark_post_commented") as commented, \
         patch(f"{_FEED}.release_post_claim") as released, \
         patch(f"{_FEED}._start_author_cooldown"), \
         patch(f"{_FEED}.insert_new_log"), \
         patch(f"{_FEED}.pace_read", return_value=0.0), \
         patch(f"{_FEED}.time.sleep"), \
         patch(f"{_FEED}.record_action") as recorded:
        ok = ra._engage_card(ctx, MagicMock(), key, "a post body", "Jane")
    return ctx, ok, fallback, commented, released, recorded


def _no_composer(*args, **kwargs):
    kwargs["outcome"]["no_composer"] = True
    return False


def test_no_composer_with_urn_goes_through_permalink():
    ctx, ok, fallback, commented, released, recorded = _engage(_URN_KEY, _no_composer)
    assert ok is True
    fallback.assert_called_once()
    commented.assert_called_once()
    released.assert_not_called()
    recorded.assert_called_once()
    assert ctx.permalink_comment_keys == {_URN_KEY}


def test_failed_fallback_releases_the_claim():
    ctx, ok, _, commented, released, _ = _engage(_URN_KEY, _no_composer, fallback_return=False)
    assert ok is False
    commented.assert_not_called()
    released.assert_called_once()
    assert not ctx.permalink_comment_keys


def test_typed_but_unverified_comment_never_retries_through_permalink():
    """The comment may already have landed, so a second route would double-comment."""
    _, ok, fallback, _, released, _ = _engage(_URN_KEY, lambda *a, **k: False)
    assert ok is False
    fallback.assert_not_called()
    released.assert_called_once()


def test_inline_success_does_not_use_the_fallback():
    ctx, ok, fallback, *_ = _engage(_URN_KEY, lambda *a, **k: True)
    assert ok is True
    fallback.assert_not_called()
    assert not ctx.permalink_comment_keys


class TestPermalinkFallbackComment:
    def test_hash_key_has_no_permalink_and_is_skipped(self):
        ctx = _ctx()
        assert ra._permalink_fallback_comment(ctx, "feedpost://abc123", "hi", "body", "Jane",
                                              False) is False
        ctx.driver.switch_to.new_window.assert_not_called()

    def test_comments_in_a_new_tab_and_restores_the_feed_tab(self):
        ctx = _ctx()
        ctx.driver.current_window_handle = "feed-tab"
        with patch(f"{_FEED}.wait_for_ajax"), patch(f"{_FEED}.time.sleep"), \
             patch(f"{_FEED}._permalink_post_card", return_value=MagicMock()), \
             patch(f"{_FEED}._author_is_me", return_value=False), \
             patch(f"{_FEED}.react_to_post_inline", return_value=True) as react, \
             patch(f"{_FEED}.mark_post_reacted"), \
             patch(f"{_FEED}.post_comment_inline", return_value=True) as post:
            ok = ra._permalink_fallback_comment(ctx, _URN_KEY, "hi", "body", "Jane", False)
        assert ok is True
        ctx.driver.switch_to.new_window.assert_called_once_with("tab")
        url = ctx.driver.get.call_args[0][0]
        assert url == "https://www.linkedin.com/feed/update/urn:li:activity:7123456789012345678/"
        react.assert_called_once()
        post.assert_called_once()
        ctx.driver.close.assert_called_once()
        ctx.driver.switch_to.window.assert_called_once_with("feed-tab")

    def test_already_reacted_card_is_not_reacted_twice(self):
        ctx = _ctx()
        with patch(f"{_FEED}.wait_for_ajax"), patch(f"{_FEED}.time.sleep"), \
             patch(f"{_FEED}._permalink_post_card", return_value=MagicMock()), \
             patch(f"{_FEED}.react_to_post_inline") as react, \
             patch(f"{_FEED}.post_comment_inline", return_value=True):
            assert ra._permalink_fallback_comment(ctx, _URN_KEY, "hi", "body", "Jane", True)
        react.assert_not_called()

    def test_no_matching_card_returns_false_and_still_restores_tab(self):
        ctx = _ctx()
        ctx.driver.current_window_handle = "feed-tab"
        with patch(f"{_FEED}.wait_for_ajax"), patch(f"{_FEED}.time.sleep"), \
             patch(f"{_FEED}._permalink_post_card", return_value=None), \
             patch(f"{_FEED}.post_comment_inline") as post:
            assert ra._permalink_fallback_comment(ctx, _URN_KEY, "hi", "body", "Jane",
                                                  False) is False
        post.assert_not_called()
        ctx.driver.switch_to.window.assert_called_once_with("feed-tab")

    def test_an_exception_is_a_failed_fallback_not_a_crash(self):
        ctx = _ctx()
        ctx.driver.get.side_effect = RuntimeError("boom")
        with patch(f"{_FEED}.log_warning"):
            assert ra._permalink_fallback_comment(ctx, _URN_KEY, "hi", "body", "Jane",
                                                  False) is False
        ctx.driver.close.assert_called_once()


def test_post_comment_inline_reports_no_composer_only_when_nothing_was_typed():
    outcome: dict = {}
    with patch(f"{_FEED}.outbound_refusal_reason", return_value=None), \
         patch(f"{_FEED}.click_first", return_value=None):
        assert ra.post_comment_inline(MagicMock(), MagicMock(), MagicMock(), "hello",
                                      user_id=1, outcome=outcome) is False
    assert outcome == {"no_composer": True}

    outcome = {}
    with patch(f"{_FEED}.outbound_refusal_reason", return_value=None), \
         patch(f"{_FEED}.click_first", return_value=MagicMock()), \
         patch(f"{_FEED}.time.sleep"), \
         patch(f"{_FEED}._post_composer_for_card", return_value=None):
        assert ra.post_comment_inline(MagicMock(), MagicMock(), MagicMock(), "hello",
                                      user_id=1, outcome=outcome) is False
    assert outcome == {"no_composer": True}
