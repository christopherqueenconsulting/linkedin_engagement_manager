"""Unit tests for sweep_reply_comments — the recent-posts reply sweep that replaces the 24h loop."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_RA}.time.sleep"):
        yield


class TestSweepReplyComments:
    def test_sweeps_each_recent_post(self):
        from cqc_lem.app.run_automation import sweep_reply_comments
        with patch(f"{_RA}.get_engagement_preferences", return_value={"reply_max_post_age_days": 3}), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[10, 11, 12]) as grp, \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RA}._reply_to_comments_on_open_post", return_value="Replied to 1 comments") as rep, \
             patch(f"{_RA}.quit_gracefully") as quit_:
            result = sweep_reply_comments.run(user_id=1)
        grp.assert_called_once_with(1, days=3)
        assert rep.call_count == 3
        assert "3/3" in result
        quit_.assert_called_once()

    def test_no_recent_posts_short_circuits_without_session(self):
        from cqc_lem.app.run_automation import sweep_reply_comments
        with patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[]), \
             patch(f"{_RA}.get_current_profile") as gcp:
            result = sweep_reply_comments.run(user_id=1)
        assert "No recent posts" in result
        gcp.assert_not_called()

    def test_rate_limited_session_returns_clean_skip(self):
        from cqc_lem.app.run_automation import sweep_reply_comments
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[10]), \
             patch(f"{_RA}.get_current_profile", side_effect=LinkedInRateLimited("429")), \
             patch(f"{_RA}.log_warning") as warn:
            result = sweep_reply_comments.run(user_id=1)
        assert "rate limited" in result.lower()
        warn.assert_called_once()

    def test_one_post_failure_does_not_abort_sweep(self):
        from cqc_lem.app.run_automation import sweep_reply_comments
        with patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[10, 11]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RA}._reply_to_comments_on_open_post", side_effect=[Exception("boom"), "ok"]), \
             patch(f"{_RA}.log_warning"), \
             patch(f"{_RA}.quit_gracefully"):
            result = sweep_reply_comments.run(user_id=1)
        assert "1/2" in result  # first post errored, second succeeded


class _FakeComment:
    def __init__(self, text, author="Jane Doe", href="https://www.linkedin.com/in/jane", already=False):
        self._text, self._author, self._href, self._already = text, author, href, already
        self.text = text

    def find_elements(self, by, sel):
        if "expandable-text-box" in sel:
            tb = MagicMock(); tb.text = self._text
            return [tb]
        return [MagicMock()] if self._already else []   # already-replied probe

    def find_element(self, by, sel):
        if "/in/" in sel:
            link = MagicMock(); link.text = self._author
            link.get_attribute = lambda a: self._href if a == "href" else ""
            return link
        raise Exception("not found")


class TestReplyToCommentsOnOpenPost:
    def _profile(self):
        p = MagicMock(); p.profile_url = "https://www.linkedin.com/in/me"; p.full_name = "Me Myself"
        return p

    def test_replies_to_new_comment(self):
        from cqc_lem.app.run_automation import _reply_to_comments_on_open_post
        driver = MagicMock(); driver.current_url = "other"
        with patch(f"{_RA}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_RA}.get_post_content", return_value="post body"), \
             patch(f"{_RA}.click_first", return_value=None), \
             patch(f"{_RA}._comment_items_from_thread", return_value=[_FakeComment("Nice post")]), \
             patch(f"{_RA}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_RA}.upsert_engager"), \
             patch(f"{_RA}.generate_thread_reply", return_value="Thanks! What resonated most?"), \
             patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}._reply_to_comment_inline", return_value=True) as rep, \
             patch(f"{_RA}.insert_new_log") as log:
            result = _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        driver.get.assert_called_once()  # navigated to the post
        rep.assert_called_once()
        log.assert_called_once()
        assert "Replied to 1 comments" in result

    def test_skips_already_replied(self):
        from cqc_lem.app.run_automation import _reply_to_comments_on_open_post
        driver = MagicMock(); driver.current_url = "x"
        with patch(f"{_RA}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_RA}.get_post_content", return_value="post body"), \
             patch(f"{_RA}.click_first", return_value=None), \
             patch(f"{_RA}._comment_items_from_thread",
                   return_value=[_FakeComment("hi", author="Me Myself", href="https://www.linkedin.com/in/me", already=True)]), \
             patch(f"{_RA}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_RA}.upsert_engager"), \
             patch(f"{_RA}.generate_thread_reply") as gen, \
             patch(f"{_RA}._reply_to_comment_inline") as rep, \
             patch(f"{_RA}.insert_new_log"):
            _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        gen.assert_not_called()   # our own reply already present → skip
        rep.assert_not_called()

    def test_lead_magnet_dm_on_keyword(self):
        from cqc_lem.app.run_automation import _reply_to_comments_on_open_post
        driver = MagicMock(); driver.current_url = "x"
        with patch(f"{_RA}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_RA}.get_post_content", return_value="post body"), \
             patch(f"{_RA}.click_first", return_value=None), \
             patch(f"{_RA}._comment_items_from_thread", return_value=[_FakeComment("Send me GUIDE please")]), \
             patch(f"{_RA}.get_lead_magnet_settings",
                   return_value={"enabled": True, "keyword": "GUIDE", "message": "Here: {blog_url}"}), \
             patch(f"{_RA}.get_user_blog_url", return_value="https://blog"), \
             patch(f"{_RA}.has_received_lead_magnet", return_value=False), \
             patch(f"{_RA}.render_dm_placeholders", return_value="Here: https://blog"), \
             patch(f"{_RA}.send_private_dm") as dm, \
             patch(f"{_RA}.record_lead_magnet_sent") as rec, \
             patch(f"{_RA}.upsert_engager"), \
             patch(f"{_RA}.generate_thread_reply", return_value="reply"), \
             patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}._reply_to_comment_inline", return_value=True), \
             patch(f"{_RA}.insert_new_log"):
            _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        dm.apply_async.assert_called_once()
        rec.assert_called_once()

    def test_no_post_url_returns_early(self):
        from cqc_lem.app.run_automation import _reply_to_comments_on_open_post
        with patch(f"{_RA}.get_post_url_from_log_for_user", return_value=None):
            result = _reply_to_comments_on_open_post(MagicMock(), MagicMock(), 1, 9, self._profile(), "s")
        assert "No post URL" in result


class TestAutomateReplyCommenting:
    def test_rate_limited_returns_clean_skip(self):
        from cqc_lem.app.run_automation import automate_reply_commenting
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with patch(f"{_RA}.get_current_profile", side_effect=LinkedInRateLimited("429")), \
             patch(f"{_RA}.log_warning") as warn:
            result = automate_reply_commenting.run(user_id=1, post_id=9, loop_for_duration=0)
        assert "rate limited" in result.lower()
        warn.assert_called_once()

    def test_single_pass_no_requeue_when_loop_zero(self):
        from cqc_lem.app.run_automation import automate_reply_commenting
        with patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RA}._reply_to_comments_on_open_post", return_value="Replied to 2 comments") as helper, \
             patch(f"{_RA}.quit_gracefully"):
            result = automate_reply_commenting.run(user_id=1, post_id=9, loop_for_duration=0)
        helper.assert_called_once()
        assert "Replied to 2 comments" in result
