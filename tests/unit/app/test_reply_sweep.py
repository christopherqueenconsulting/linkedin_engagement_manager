"""Unit tests for sweep_reply_comments — the recent-posts reply sweep that replaces the 24h loop."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"

# What _reply_to_comments_on_open_post returns since #622: counts, not just a sentence.
_OUTCOME = {"status": "ok", "summary": "Replied to 1 comments", "comments_found": 2,
            "replies_sent": 1}


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
             patch(f"{_RA}._reply_to_comments_on_open_post", return_value=_OUTCOME) as rep, \
             patch(f"{_RA}._record_golden_hour_report") as report, \
             patch(f"{_RA}.quit_gracefully") as quit_:
            result = sweep_reply_comments.run(user_id=1)
        grp.assert_called_once_with(1, days=3)
        assert rep.call_count == 3
        assert report.call_count == 3      # one golden-hour report per swept post (#622)
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
             patch(f"{_RA}._retry_golden_hour_sweep", return_value=False), \
             patch(f"{_RA}.log_warning") as warn:
            result = sweep_reply_comments.run(user_id=1)
        assert "rate limited" in result.lower()
        warn.assert_called_once()

    def test_rate_limited_golden_hour_sweep_retries_inside_the_window(self):
        """#401's amplifier lost the whole hour to one transient 429 — the sweep now asks for one
        more attempt while the window is still open (#622)."""
        from cqc_lem.app.run_automation import sweep_reply_comments
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[10]), \
             patch(f"{_RA}.get_current_profile", side_effect=LinkedInRateLimited("429")), \
             patch(f"{_RA}._retry_golden_hour_sweep", return_value=True) as retry, \
             patch(f"{_RA}.log_warning"):
            result = sweep_reply_comments.run(user_id=1, sweep_slot=2, attempt=0)
        retry.assert_called_once_with(1, 2, 0, "rate limited")
        assert "retry scheduled" in result

    def test_one_post_failure_does_not_abort_sweep(self):
        from cqc_lem.app.run_automation import sweep_reply_comments
        with patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}.get_recent_posted_post_ids", return_value=[10, 11]), \
             patch(f"{_RA}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_RA}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_RA}._reply_to_comments_on_open_post", side_effect=[Exception("boom"), _OUTCOME]), \
             patch(f"{_RA}._record_golden_hour_report") as report, \
             patch(f"{_RA}.log_warning"), \
             patch(f"{_RA}.quit_gracefully"):
            result = sweep_reply_comments.run(user_id=1)
        assert "1/2" in result  # first post errored, second succeeded
        # The failed post still reports — a sweep that crashed on a post is exactly what the
        # golden-hour audit needs to see (#622).
        assert report.call_count == 2
        assert report.call_args_list[0].args[3]["status"] == "error"


class TestGoldenHourReporting:
    """The per-post report (#622) that makes the #401 amplifier's silence diagnosable."""

    def _published(self, minutes_ago):
        return float(minutes_ago)   # get_post_age_minutes returns minutes, computed in SQL

    def test_in_window_sweep_logs_info_and_tracks(self):
        from cqc_lem.app.run_automation import _record_golden_hour_report, _reply_outcome
        with patch(f"{_RA}.get_post_age_minutes", return_value=self._published(22)), \
             patch(f"{_RA}.track_golden_hour_report") as track, \
             patch(f"{_RA}.log_info") as info, \
             patch(f"{_RA}.log_warning") as warn:
            report = _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s", 3, 2))
        assert report["within_window"] is True
        assert report["comments_found"] == 3 and report["replies_sent"] == 2
        assert report["latency_minutes"] == 22.0
        track.assert_called_once_with(1, report)
        info.assert_called_once()
        warn.assert_not_called()

    def test_late_sweep_warns(self):
        from cqc_lem.app.run_automation import _record_golden_hour_report, _reply_outcome
        with patch(f"{_RA}.get_post_age_minutes", return_value=self._published(200)), \
             patch(f"{_RA}.track_golden_hour_report"), \
             patch(f"{_RA}.log_warning") as warn:
            report = _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s"))
        assert report["within_window"] is False
        warn.assert_called_once()

    def test_stale_post_is_not_reported(self):
        """The sweep walks the last couple of days on purpose; only fresh posts say anything about
        the amplifier's timing, so old ones emit nothing rather than permanent out-of-window noise."""
        from cqc_lem.app.run_automation import _record_golden_hour_report, _reply_outcome
        with patch(f"{_RA}.get_post_age_minutes", return_value=self._published(3 * 24 * 60)), \
             patch(f"{_RA}.track_golden_hour_report") as track:
            assert _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s")) is None
        track.assert_not_called()

    def test_unknown_publish_time_still_reports_out_of_window(self):
        from cqc_lem.app.run_automation import _record_golden_hour_report, _reply_outcome
        with patch(f"{_RA}.get_post_age_minutes", return_value=None), \
             patch(f"{_RA}.track_golden_hour_report") as track, \
             patch(f"{_RA}.log_warning"):
            report = _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s"))
        assert report["latency_minutes"] is None and report["within_window"] is False
        track.assert_called_once()

    def test_a_posthog_failure_never_breaks_the_sweep(self):
        from cqc_lem.app.run_automation import _record_golden_hour_report, _reply_outcome
        with patch(f"{_RA}.get_post_age_minutes", return_value=self._published(10)), \
             patch(f"{_RA}.track_golden_hour_report", side_effect=RuntimeError("posthog down")), \
             patch(f"{_RA}.log_info"), patch(f"{_RA}.log_warning") as warn:
            report = _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s"))
        assert report is not None
        warn.assert_called_once()


class TestGoldenHourSweepRetry:
    def _published(self, minutes_ago):
        return float(minutes_ago)   # get_post_age_minutes returns minutes, computed in SQL

    def test_schedules_one_more_sweep_while_the_window_is_open(self):
        from cqc_lem.app.run_automation import _retry_golden_hour_sweep
        with patch(f"{_RA}.get_recent_posted_post_ids", return_value=[10]), \
             patch(f"{_RA}.get_post_age_minutes", return_value=self._published(15)), \
             patch(f"{_RA}.sweep_reply_comments") as task, \
             patch(f"{_RA}.log_info"):
            assert _retry_golden_hour_sweep(1, 2, 0, "rate limited") is True
        assert task.apply_async.call_args.kwargs["kwargs"] == {"user_id": 1, "sweep_slot": 2,
                                                               "attempt": 1}
        assert task.apply_async.call_args.kwargs["countdown"] > 0

    def test_no_retry_once_the_window_has_closed(self):
        from cqc_lem.app.run_automation import _retry_golden_hour_sweep
        with patch(f"{_RA}.get_recent_posted_post_ids", return_value=[10]), \
             patch(f"{_RA}.get_post_age_minutes", return_value=self._published(200)), \
             patch(f"{_RA}.sweep_reply_comments") as task:
            assert _retry_golden_hour_sweep(1, 0, 0, "rate limited") is False
        task.apply_async.assert_not_called()

    def test_no_retry_without_a_recent_post(self):
        from cqc_lem.app.run_automation import _retry_golden_hour_sweep
        with patch(f"{_RA}.get_recent_posted_post_ids", return_value=[]), \
             patch(f"{_RA}.sweep_reply_comments") as task:
            assert _retry_golden_hour_sweep(1, 0, 0, "session failed") is False
        task.apply_async.assert_not_called()

    def test_retries_are_bounded(self):
        from cqc_lem.app.run_automation import _retry_golden_hour_sweep
        from cqc_lem.utilities.golden_hour import GOLDEN_HOUR_MAX_RETRIES
        with patch(f"{_RA}.get_recent_posted_post_ids", return_value=[10]), \
             patch(f"{_RA}.get_post_age_minutes", return_value=self._published(5)), \
             patch(f"{_RA}.sweep_reply_comments") as task:
            assert _retry_golden_hour_sweep(1, 0, GOLDEN_HOUR_MAX_RETRIES, "429") is False
        task.apply_async.assert_not_called()


class TestGoldenHourSweepCountdowns:
    def test_three_sweeps_spread_across_the_hour(self):
        from cqc_lem.app.run_automation import _golden_hour_sweep_countdowns
        assert _golden_hour_sweep_countdowns(3) == [20 * 60, 40 * 60, 60 * 60]

    def test_default_matches_module_constant(self):
        from cqc_lem.app.run_automation import (_golden_hour_sweep_countdowns,
                                                _GOLDEN_HOUR_REPLY_SWEEPS)
        assert len(_golden_hour_sweep_countdowns()) == _GOLDEN_HOUR_REPLY_SWEEPS

    def test_last_sweep_lands_at_end_of_window(self):
        from cqc_lem.app.run_automation import _golden_hour_sweep_countdowns, _GOLDEN_HOUR_MINUTES
        for n in (1, 2, 4, 6):
            cds = _golden_hour_sweep_countdowns(n)
            assert cds[-1] == _GOLDEN_HOUR_MINUTES * 60      # last sweep closes the golden hour
            assert cds == sorted(cds) and len(cds) == n       # strictly ordered, right count

    def test_non_positive_count_floors_to_one(self):
        from cqc_lem.app.run_automation import _golden_hour_sweep_countdowns, _GOLDEN_HOUR_MINUTES
        assert _golden_hour_sweep_countdowns(0) == [_GOLDEN_HOUR_MINUTES * 60]

    def test_oversized_count_is_clamped(self):
        from cqc_lem.app.run_automation import _golden_hour_sweep_countdowns, _GOLDEN_HOUR_MAX_SWEEPS
        # A misconfigured huge value can't schedule an unbounded number of ETA sweeps.
        assert len(_golden_hour_sweep_countdowns(1000)) == _GOLDEN_HOUR_MAX_SWEEPS


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
             patch(f"{_RA}._flag_lead_signal", return_value=None), \
             patch(f"{_RA}._reply_to_comment_inline", return_value=True) as rep, \
             patch(f"{_RA}.insert_new_log") as log:
            result = _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        driver.get.assert_called_once()  # navigated to the post
        rep.assert_called_once()
        log.assert_called_once()
        assert result == {"status": "ok", "summary": "Replied to 1 comments",
                          "comments_found": 1, "replies_sent": 1}

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
             patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}._flag_lead_signal", return_value=None), \
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
             patch(f"{_RA}._flag_lead_signal", return_value=None), \
             patch(f"{_RA}._reply_to_comment_inline", return_value=True), \
             patch(f"{_RA}.insert_new_log"):
            _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        dm.apply_async.assert_called_once()
        rec.assert_called_once()

    def test_bails_when_profile_slug_unresolvable(self):
        """LOOP SAFETY: with no profile slug we can't dedup our own / already-replied comments, so
        the sweep must skip replying entirely rather than risk duplicate/self replies."""
        from cqc_lem.app.run_automation import _reply_to_comments_on_open_post
        from unittest.mock import MagicMock
        prof = MagicMock(); prof.profile_url = None; prof.full_name = "Me"
        with patch(f"{_RA}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_RA}.get_post_content", return_value="body"), \
             patch(f"{_RA}.click_first", return_value=None), \
             patch(f"{_RA}._comment_items_from_thread", return_value=[_FakeComment("hi")]), \
             patch(f"{_RA}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_RA}.log_warning"), \
             patch(f"{_RA}.generate_thread_reply") as gen, \
             patch(f"{_RA}._reply_to_comment_inline") as rep:
            result = _reply_to_comments_on_open_post(MagicMock(), MagicMock(), 1, 9, prof, "s")
        assert "no profile slug" in result["summary"].lower()
        assert result["status"] == "no_profile_slug"
        gen.assert_not_called()
        rep.assert_not_called()

    def test_reply_cap_limits_burst(self):
        from cqc_lem.app.run_automation import _reply_to_comments_on_open_post, _MAX_REPLIES_PER_SWEEP
        from unittest.mock import MagicMock
        boxes = [_FakeComment(f"comment number {i}") for i in range(_MAX_REPLIES_PER_SWEEP + 5)]
        with patch(f"{_RA}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_RA}.get_post_content", return_value="body"), \
             patch(f"{_RA}.click_first", return_value=None), \
             patch(f"{_RA}._comment_items_from_thread", return_value=boxes), \
             patch(f"{_RA}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_RA}.upsert_engager"), \
             patch(f"{_RA}.generate_thread_reply", return_value="reply"), \
             patch(f"{_RA}.get_engagement_preferences", return_value={}), \
             patch(f"{_RA}._flag_lead_signal", return_value=None), \
             patch(f"{_RA}._reply_to_comment_inline", return_value=True) as rep, \
             patch(f"{_RA}.insert_new_log"):
            _reply_to_comments_on_open_post(MagicMock(), MagicMock(), 1, 9, self._profile(), "s")
        assert rep.call_count == _MAX_REPLIES_PER_SWEEP

    def test_no_post_url_returns_early(self):
        from cqc_lem.app.run_automation import _reply_to_comments_on_open_post
        with patch(f"{_RA}.get_post_url_from_log_for_user", return_value=None):
            result = _reply_to_comments_on_open_post(MagicMock(), MagicMock(), 1, 9, self._profile(), "s")
        assert result["summary"] == "No post URL"
        assert result["status"] == "no_post_url"


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
             patch(f"{_RA}._reply_to_comments_on_open_post",
                   return_value={"status": "ok", "summary": "Replied to 2 comments",
                                 "comments_found": 2, "replies_sent": 2}) as helper, \
             patch(f"{_RA}.quit_gracefully"):
            result = automate_reply_commenting.run(user_id=1, post_id=9, loop_for_duration=0)
        helper.assert_called_once()
        assert "Replied to 2 comments" in result


class TestFeedFunnelStorage:
    def test_set_and_get_round_trip(self):
        import json
        redis = MagicMock()
        with patch(f"{_RA}._redis_client", return_value=redis):
            from cqc_lem.app.run_automation import set_feed_funnel, get_feed_funnel
            set_feed_funnel(1, {"examined": 5, "commented": 2})
            stored = redis.set.call_args
            assert stored.args[0] == "linkedin:feed_funnel:1"
            assert json.loads(stored.args[1]) == {"examined": 5, "commented": 2}
            assert stored.kwargs["ex"] == 30 * 24 * 60 * 60
            redis.get.return_value = stored.args[1]
            assert get_feed_funnel(1) == {"examined": 5, "commented": 2}

    def test_get_returns_none_when_absent(self):
        redis = MagicMock(); redis.get.return_value = None
        with patch(f"{_RA}._redis_client", return_value=redis):
            from cqc_lem.app.run_automation import get_feed_funnel
            assert get_feed_funnel(1) is None

    def test_no_redis_is_safe(self):
        with patch(f"{_RA}._redis_client", return_value=None):
            from cqc_lem.app.run_automation import set_feed_funnel, get_feed_funnel
            set_feed_funnel(1, {"x": 1})     # must not raise
            assert get_feed_funnel(1) is None

    def test_set_swallows_redis_error(self):
        redis = MagicMock(); redis.set.side_effect = RuntimeError("down")
        with patch(f"{_RA}._redis_client", return_value=redis), patch(f"{_RA}.log_warning"):
            from cqc_lem.app.run_automation import set_feed_funnel
            set_feed_funnel(1, {"x": 1})     # must not raise

    def test_get_handles_bad_json(self):
        redis = MagicMock(); redis.get.return_value = b"not json"
        with patch(f"{_RA}._redis_client", return_value=redis):
            from cqc_lem.app.run_automation import get_feed_funnel
            assert get_feed_funnel(1) is None
