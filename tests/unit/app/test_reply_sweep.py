"""Unit tests for sweep_reply_comments — the recent-posts reply sweep that replaces the 24h loop."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# The reply sweep this file is about moved to `app.engagement.posting` (#1154) — that is the
# module whose globals the sweep, the retry and the reply rail read.
_POST = "cqc_lem.app.engagement.posting"
# The feed funnel store moved with the feed engine to `app.engagement.feed` (#1154).
_FEED = "cqc_lem.app.engagement.feed"
# The reporting pair moved down to `utilities/golden_hour.py` (#1154) and took the post-age read
# and the PostHog ship with it, so those collaborators resolve THERE now. The sweep's OWN view of
# `_record_golden_hour_report` is patched on the module that reads it, `_POST`.
_GH = "cqc_lem.utilities.golden_hour"

# What _reply_to_comments_on_open_post returns since #622: counts, not just a sentence.
_OUTCOME = {"status": "ok", "summary": "Replied to 1 comments", "comments_found": 2,
            "replies_sent": 1}


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_POST}.time.sleep"):
        yield


class TestSweepReplyComments:
    def test_sweeps_each_recent_post(self):
        from cqc_lem.app.engagement.posting import sweep_reply_comments
        with patch(f"{_POST}.get_engagement_preferences", return_value={"reply_max_post_age_days": 3}), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[10, 11, 12]) as grp, \
             patch(f"{_POST}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_POST}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_POST}._reply_to_comments_on_open_post", return_value=_OUTCOME) as rep, \
             patch(f"{_POST}._record_golden_hour_report") as report, \
             patch(f"{_POST}.quit_gracefully") as quit_:
            result = sweep_reply_comments.run(user_id=1)
        grp.assert_called_once_with(1, days=3)
        assert rep.call_count == 3
        assert report.call_count == 3      # one golden-hour report per swept post (#622)
        assert "3/3" in result
        quit_.assert_called_once()

    def test_no_recent_posts_short_circuits_without_session(self):
        from cqc_lem.app.engagement.posting import sweep_reply_comments
        with patch(f"{_POST}.get_engagement_preferences", return_value={}), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[]), \
             patch(f"{_POST}.get_current_profile") as gcp:
            result = sweep_reply_comments.run(user_id=1)
        assert "No recent posts" in result
        gcp.assert_not_called()

    def test_rate_limited_session_returns_clean_skip(self):
        from cqc_lem.app.engagement.posting import sweep_reply_comments
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with patch(f"{_POST}.get_engagement_preferences", return_value={}), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[10]), \
             patch(f"{_POST}.get_current_profile", side_effect=LinkedInRateLimited("429")), \
             patch(f"{_POST}._retry_golden_hour_sweep", return_value=False), \
             patch(f"{_POST}.log_warning") as warn:
            result = sweep_reply_comments.run(user_id=1)
        assert "rate limited" in result.lower()
        warn.assert_called_once()

    def test_rate_limited_golden_hour_sweep_retries_inside_the_window(self):
        """#401's amplifier lost the whole hour to one transient 429 — the sweep now asks for one
        more attempt while the window is still open (#622).
        """
        from cqc_lem.app.engagement.posting import sweep_reply_comments
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with patch(f"{_POST}.get_engagement_preferences", return_value={}), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[10]), \
             patch(f"{_POST}.get_current_profile", side_effect=LinkedInRateLimited("429")), \
             patch(f"{_POST}._retry_golden_hour_sweep", return_value=True) as retry, \
             patch(f"{_POST}.log_warning"):
            result = sweep_reply_comments.run(user_id=1, sweep_slot=2, attempt=0)
        retry.assert_called_once_with(1, 2, 0, "rate_limited")
        assert "retry scheduled" in result

    def test_one_post_failure_does_not_abort_sweep(self):
        from cqc_lem.app.engagement.posting import sweep_reply_comments
        with patch(f"{_POST}.get_engagement_preferences", return_value={}), \
             patch(f"{_POST}.get_recent_posted_post_ids", return_value=[10, 11]), \
             patch(f"{_POST}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_POST}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_POST}._reply_to_comments_on_open_post", side_effect=[Exception("boom"), _OUTCOME]), \
             patch(f"{_POST}._record_golden_hour_report") as report, \
             patch(f"{_POST}.log_warning"), \
             patch(f"{_POST}.quit_gracefully"):
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
        from cqc_lem.utilities.golden_hour import _record_golden_hour_report, _reply_outcome
        with patch(f"{_GH}.get_post_age_minutes", return_value=self._published(22)), \
             patch(f"{_GH}.track_golden_hour_report") as track, \
             patch(f"{_GH}.log_info") as info, \
             patch(f"{_GH}.log_warning") as warn:
            report = _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s", 3, 2))
        assert report["within_window"] is True
        assert report["comments_found"] == 3 and report["replies_sent"] == 2
        assert report["latency_minutes"] == 22.0
        track.assert_called_once_with(1, report)
        info.assert_called_once()
        warn.assert_not_called()

    def test_late_sweep_warns(self):
        from cqc_lem.utilities.golden_hour import _record_golden_hour_report, _reply_outcome
        with patch(f"{_GH}.get_post_age_minutes", return_value=self._published(120)), \
             patch(f"{_GH}.track_golden_hour_report"), \
             patch(f"{_GH}.log_warning") as warn:
            report = _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s"))
        assert report["within_window"] is False
        warn.assert_called_once()

    def test_stale_post_is_not_reported(self):
        """The sweep walks the last couple of days on purpose; only fresh posts say anything about
        the amplifier's timing, so old ones emit nothing rather than permanent out-of-window noise.
        """
        from cqc_lem.utilities.golden_hour import _record_golden_hour_report, _reply_outcome
        with patch(f"{_GH}.get_post_age_minutes", return_value=self._published(3 * 24 * 60)), \
             patch(f"{_GH}.track_golden_hour_report") as track:
            assert _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s")) is None
        track.assert_not_called()

    def test_a_routine_revisit_of_an_older_post_is_not_graded(self):
        """Every sweep walks yesterday's post too. Grading those revisits would put a permanent
        stream of out-of-window readings into the on-time rate — and a WARNING per sweep.
        """
        from cqc_lem.utilities.golden_hour import _record_golden_hour_report, _reply_outcome
        with patch(f"{_GH}.get_post_age_minutes", return_value=self._published(10 * 60)), \
             patch(f"{_GH}.track_golden_hour_report") as track, \
             patch(f"{_GH}.log_warning") as warn:
            assert _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s")) is None
        track.assert_not_called()
        warn.assert_not_called()

    def test_the_second_wave_is_graded_against_its_own_horizon(self):
        """The same 10h-old post IS the second wave's business — its window is 6-8h, not 90 min."""
        from cqc_lem.utilities.golden_hour import (
            PHASE_SECOND_WAVE,
            _record_golden_hour_report,
            _reply_outcome,
        )
        with patch(f"{_GH}.get_post_age_minutes", return_value=self._published(7 * 60)), \
             patch(f"{_GH}.track_golden_hour_report") as track, \
             patch(f"{_GH}.log_info"):
            report = _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s", replies_sent=1),
                                                phase=PHASE_SECOND_WAVE)
        assert report["within_window"] is True
        track.assert_called_once()

    def test_unknown_publish_time_still_reports_out_of_window(self):
        from cqc_lem.utilities.golden_hour import _record_golden_hour_report, _reply_outcome
        with patch(f"{_GH}.get_post_age_minutes", return_value=None), \
             patch(f"{_GH}.track_golden_hour_report") as track, \
             patch(f"{_GH}.log_warning"):
            report = _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s"))
        assert report["latency_minutes"] is None and report["within_window"] is False
        track.assert_called_once()

    def test_an_unreadable_post_age_never_breaks_the_sweep(self):
        """Measurement must not abort the thing it measures — a DB hiccup on the age read reports
        an unknown latency instead of killing the sweep mid-post.
        """
        from cqc_lem.utilities.golden_hour import _record_golden_hour_report, _reply_outcome
        with patch(f"{_GH}.get_post_age_minutes", side_effect=RuntimeError("db down")), \
             patch(f"{_GH}.track_golden_hour_report") as track, \
             patch(f"{_GH}.log_warning"):
            report = _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s"))
        assert report["latency_minutes"] is None and report["within_window"] is False
        track.assert_called_once()

    def test_a_posthog_failure_never_breaks_the_sweep(self):
        from cqc_lem.utilities.golden_hour import _record_golden_hour_report, _reply_outcome
        with patch(f"{_GH}.get_post_age_minutes", return_value=self._published(10)), \
             patch(f"{_GH}.track_golden_hour_report", side_effect=RuntimeError("posthog down")), \
             patch(f"{_GH}.log_info"), patch(f"{_GH}.log_warning") as warn:
            report = _record_golden_hour_report(1, 9, 0, _reply_outcome("ok", "s"))
        assert report is not None
        warn.assert_called_once()


class TestGoldenHourSweepRetry:
    def _published(self, minutes_ago):
        return float(minutes_ago)   # get_post_age_minutes returns minutes, computed in SQL

    def test_schedules_one_more_sweep_while_the_window_is_open(self):
        from cqc_lem.app.engagement.posting import _retry_golden_hour_sweep
        with patch(f"{_POST}.get_recent_posted_post_ids", return_value=[10]), \
             patch(f"{_GH}.get_post_age_minutes", return_value=self._published(15)), \
             patch(f"{_GH}.track_golden_hour_report"), \
             patch(f"{_POST}.sweep_reply_comments") as task, \
             patch(f"{_POST}.log_info"):
            assert _retry_golden_hour_sweep(1, 2, 0, "rate_limited") is True
        assert task.apply_async.call_args.kwargs["kwargs"] == {"user_id": 1, "sweep_slot": 2,
                                                               "attempt": 1}
        assert task.apply_async.call_args.kwargs["countdown"] > 0

    def test_a_sweep_that_could_not_run_reports_why(self):
        """The audit's whole question was "late, rate-limited, or nothing to reply to?" — a sweep
        that never got a session emits its own report so the silent hour has a cause (#622).
        """
        from cqc_lem.app.engagement.posting import _retry_golden_hour_sweep
        with patch(f"{_POST}.get_recent_posted_post_ids", return_value=[10]), \
             patch(f"{_GH}.get_post_age_minutes", return_value=self._published(15)), \
             patch(f"{_GH}.track_golden_hour_report") as track, \
             patch(f"{_POST}.sweep_reply_comments"), \
             patch(f"{_POST}.log_info"):
            _retry_golden_hour_sweep(1, 0, 0, "rate_limited")
        report = track.call_args.args[1]
        assert report["status"] == "rate_limited"
        assert report["post_id"] == 10 and report["replies_sent"] == 0

    def test_no_retry_once_the_window_has_closed(self):
        from cqc_lem.app.engagement.posting import _retry_golden_hour_sweep
        with patch(f"{_POST}.get_recent_posted_post_ids", return_value=[10]), \
             patch(f"{_GH}.get_post_age_minutes", return_value=self._published(120)), \
             patch(f"{_GH}.track_golden_hour_report"), \
             patch(f"{_POST}.log_warning"), \
             patch(f"{_POST}.sweep_reply_comments") as task:
            assert _retry_golden_hour_sweep(1, 0, 0, "rate_limited") is False
        task.apply_async.assert_not_called()

    def test_no_retry_without_a_recent_post(self):
        from cqc_lem.app.engagement.posting import _retry_golden_hour_sweep
        with patch(f"{_POST}.get_recent_posted_post_ids", return_value=[]), \
             patch(f"{_GH}.track_golden_hour_report") as track, \
             patch(f"{_POST}.sweep_reply_comments") as task:
            assert _retry_golden_hour_sweep(1, 0, 0, "session_failed") is False
        task.apply_async.assert_not_called()
        track.assert_not_called()

    def test_retries_are_bounded(self):
        from cqc_lem.app.engagement.posting import _retry_golden_hour_sweep
        from cqc_lem.utilities.golden_hour import GOLDEN_HOUR_MAX_RETRIES
        with patch(f"{_POST}.get_recent_posted_post_ids", return_value=[10]), \
             patch(f"{_GH}.get_post_age_minutes", return_value=self._published(5)), \
             patch(f"{_GH}.track_golden_hour_report"), \
             patch(f"{_POST}.log_info"), \
             patch(f"{_POST}.sweep_reply_comments") as task:
            assert _retry_golden_hour_sweep(1, 0, GOLDEN_HOUR_MAX_RETRIES, "429") is False
        task.apply_async.assert_not_called()


class TestGoldenHourSweepCountdowns:
    def test_three_sweeps_spread_across_the_hour(self):
        from cqc_lem.app.engagement.posting import _golden_hour_sweep_countdowns
        assert _golden_hour_sweep_countdowns(3) == [20 * 60, 40 * 60, 60 * 60]

    def test_default_matches_module_constant(self):
        from cqc_lem.app.engagement.posting import _GOLDEN_HOUR_REPLY_SWEEPS, _golden_hour_sweep_countdowns
        assert len(_golden_hour_sweep_countdowns()) == _GOLDEN_HOUR_REPLY_SWEEPS

    def test_last_sweep_lands_at_end_of_window(self):
        from cqc_lem.app.engagement.posting import _GOLDEN_HOUR_MINUTES, _golden_hour_sweep_countdowns
        for n in (1, 2, 4, 6):
            cds = _golden_hour_sweep_countdowns(n)
            assert cds[-1] == _GOLDEN_HOUR_MINUTES * 60      # last sweep closes the golden hour
            assert cds == sorted(cds) and len(cds) == n       # strictly ordered, right count

    def test_non_positive_count_floors_to_one(self):
        from cqc_lem.app.engagement.posting import _GOLDEN_HOUR_MINUTES, _golden_hour_sweep_countdowns
        assert _golden_hour_sweep_countdowns(0) == [_GOLDEN_HOUR_MINUTES * 60]

    def test_oversized_count_is_clamped(self):
        from cqc_lem.app.engagement.posting import _GOLDEN_HOUR_MAX_SWEEPS, _golden_hour_sweep_countdowns
        # A misconfigured huge value can't schedule an unbounded number of ETA sweeps.
        assert len(_golden_hour_sweep_countdowns(1000)) == _GOLDEN_HOUR_MAX_SWEEPS


class _FakeComment:
    """One SDUI comment card, with its header anchors in the order LinkedIn renders them (#1091).

    The AVATAR link comes first — an /in/ href with no text — then the name link. Reading the first
    anchor therefore names nobody, which is why the sweep must read the whole header.
    """

    def __init__(self, text, author="Jane Doe", href="https://www.linkedin.com/in/jane", already=False):
        self._text, self._href, self._already = text, href, already
        self.text = text
        self.anchors = [{"href": href, "text": "", "aria": ""}]
        if author:
            self.anchors.append({"href": href, "text": author, "aria": ""})

    def find_elements(self, by, sel):
        if "expandable-text-box" in sel:
            tb = MagicMock(); tb.text = self._text
            return [tb]
        return [MagicMock()] if self._already else []   # already-replied probe

    def find_element(self, by, sel):
        raise Exception("not found")


def _sweep_driver(current_url="x"):
    """A driver that answers `comment_author_identity`'s anchor read off the card it is handed.

    Anything else (a bare MagicMock) reads as no anchors.
    """
    driver = MagicMock()
    driver.current_url = current_url
    driver.execute_script.side_effect = lambda script, *args: (
        args[0].anchors if args and isinstance(getattr(args[0], "anchors", None), list) else None)
    return driver


class TestReplyToCommentsOnOpenPost:
    def _profile(self):
        p = MagicMock(); p.profile_url = "https://www.linkedin.com/in/me"; p.full_name = "Me Myself"
        return p

    def test_replies_to_new_comment(self):
        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        driver = _sweep_driver("other")
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="post body"), \
             patch(f"{_POST}.click_first", return_value=None), \
             patch(f"{_POST}._comment_items_from_thread", return_value=[_FakeComment("Nice post")]), \
             patch(f"{_POST}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_POST}.upsert_engager"), \
             patch(f"{_POST}.generate_thread_reply", return_value="Thanks! What resonated most?"), \
             patch(f"{_POST}.get_engagement_preferences", return_value={}), \
             patch(f"{_POST}._flag_lead_signal", return_value=None), \
             patch(f"{_POST}._reply_to_comment_inline", return_value=True) as rep, \
             patch(f"{_POST}.insert_new_log") as log:
            result = _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        driver.get.assert_called_once()  # navigated to the post
        rep.assert_called_once()
        log.assert_called_once()
        assert result == {"status": "ok", "summary": "Replied to 1 comments",
                          "comments_found": 1, "replies_sent": 1}

    def test_engager_is_recorded_when_the_avatar_anchor_comes_first(self):
        """#1091: an avatar-first card must still record its commenter.

        The sweep read the FIRST /in/ anchor on the card, which is the avatar link — no text, so
        `clean_person_name` returned '' and the capture was skipped in silence while the reply on
        that same card still landed.
        """
        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        driver = _sweep_driver()
        card = _FakeComment("Nice post", author="Jane Doe Verified Profile 2nd",
                            href="https://www.linkedin.com/in/jane")
        assert card.anchors[0]["text"] == ""      # the anchor the old reader took
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="post body"), \
             patch(f"{_POST}.click_first", return_value=None), \
             patch(f"{_POST}._comment_items_from_thread", return_value=[card]), \
             patch(f"{_POST}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_POST}.upsert_engager") as upsert, \
             patch(f"{_POST}.generate_thread_reply", return_value="Thanks!"), \
             patch(f"{_POST}.get_engagement_preferences", return_value={}), \
             patch(f"{_POST}._flag_lead_signal", return_value=None) as flag, \
             patch(f"{_POST}._reply_to_comment_inline", return_value=True), \
             patch(f"{_POST}.insert_new_log"):
            _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        upsert.assert_called_once_with(1, "Jane Doe", "https://www.linkedin.com/in/jane",
                                       connection_degree="2nd")
        flag.assert_called_once()   # the lead-signal half rode the same dead read (#483)

    def test_unnamed_commenter_is_a_countable_debug_skip_not_a_crash(self):
        """A card where nothing is name-like has no key for a name-keyed capture.

        The sweep records that as DEBUG (an expected no-op per utilities/CLAUDE.md) and still replies.
        """
        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        driver = _sweep_driver()
        card = _FakeComment("Nice post", author="", href="https://www.linkedin.com/in/ghost")
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="post body"), \
             patch(f"{_POST}.click_first", return_value=None), \
             patch(f"{_POST}._comment_items_from_thread", return_value=[card]), \
             patch(f"{_POST}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_POST}.upsert_engager") as upsert, \
             patch(f"{_POST}.generate_thread_reply", return_value="Thanks!"), \
             patch(f"{_POST}.get_engagement_preferences", return_value={}), \
             patch(f"{_POST}._reply_to_comment_inline", return_value=True) as rep, \
             patch(f"{_POST}.log_debug") as debug, \
             patch(f"{_POST}.log_warning") as warn, \
             patch(f"{_POST}.insert_new_log"):
            _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        upsert.assert_not_called()
        rep.assert_called_once()    # the reply half only needs the href, and still works
        warn.assert_not_called()
        assert any("name unreadable" in str(c.args[0]) for c in debug.call_args_list)

    def test_load_more_miss_never_warns(self):
        """Issue #1041: the miss IS the expansion loop's exit condition — every sweep ends on one,
        so warning would escalate to a grouped $exception for working behaviour.
        """
        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        driver = _sweep_driver()
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="post body"), \
             patch(f"{_POST}.click_first", return_value=None) as click, \
             patch(f"{_POST}._comment_items_from_thread", return_value=[]), \
             patch(f"{_POST}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_POST}.get_engagement_preferences", return_value={}), \
             patch(f"{_POST}.log_warning") as warn:
            _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        click.assert_called_once()                       # the miss breaks the loop, no re-clicking
        assert click.call_args.args[3] == "Load more comments"
        assert click.call_args.kwargs["required"] is False
        assert click.call_args.kwargs["warn_on_miss"] is False
        warn.assert_not_called()

    def test_load_more_expands_until_the_control_is_gone(self):
        """Silencing the miss must not silence the expansion: a rendered control still gets clicked
        until LinkedIn stops rendering it.
        """
        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        driver = _sweep_driver()
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="post body"), \
             patch(f"{_POST}.click_first", side_effect=[MagicMock(), MagicMock(), None]) as click, \
             patch(f"{_POST}._comment_items_from_thread", return_value=[]), \
             patch(f"{_POST}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_POST}.get_engagement_preferences", return_value={}):
            _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        assert click.call_count == 3

    def test_skips_already_replied(self):
        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        driver = _sweep_driver()
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="post body"), \
             patch(f"{_POST}.click_first", return_value=None), \
             patch(f"{_POST}._comment_items_from_thread",
                   return_value=[_FakeComment("hi", author="Me Myself", href="https://www.linkedin.com/in/me", already=True)]), \
             patch(f"{_POST}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_POST}.upsert_engager"), \
             patch(f"{_POST}.generate_thread_reply") as gen, \
             patch(f"{_POST}.get_engagement_preferences", return_value={}), \
             patch(f"{_POST}._flag_lead_signal", return_value=None), \
             patch(f"{_POST}._reply_to_comment_inline") as rep, \
             patch(f"{_POST}.insert_new_log"):
            _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        gen.assert_not_called()   # our own reply already present → skip
        rep.assert_not_called()

    def test_lead_magnet_delivery_is_queued_for_approval_never_sent(self):
        """Issue #624: the comment-keyword artifact goes to the approval queue, not out the door."""
        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        driver = _sweep_driver()
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="post body"), \
             patch(f"{_POST}.click_first", return_value=None), \
             patch(f"{_POST}._comment_items_from_thread", return_value=[_FakeComment("Send me GUIDE please")]), \
             patch(f"{_POST}.get_lead_magnet_settings",
                   return_value={"enabled": True, "keyword": "GUIDE", "message": "Here: {blog_url}"}), \
             patch(f"{_POST}.get_user_blog_url", return_value="https://blog"), \
             patch(f"{_POST}.has_received_lead_magnet", return_value=False), \
             patch(f"{_POST}.has_open_scheduled_dm", return_value=False), \
             patch(f"{_POST}.count_scheduled_dms_created_today", return_value=0), \
             patch(f"{_POST}.render_dm_placeholders", return_value="Here: https://blog"), \
             patch(f"{_POST}.insert_scheduled_dm", return_value=77) as ins, \
             patch(f"{_POST}.record_lead_magnet_sent") as rec, \
             patch(f"{_POST}.upsert_engager"), \
             patch(f"{_POST}.generate_thread_reply", return_value="reply"), \
             patch(f"{_POST}.get_engagement_preferences", return_value={"max_dms_per_day": 5}), \
             patch(f"{_POST}._flag_lead_signal", return_value=None), \
             patch(f"{_POST}._reply_to_comment_inline", return_value=True), \
             patch(f"{_POST}.insert_new_log"):
            _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        ins.assert_called_once()
        rec.assert_called_once()
        # "Never sent" asserted STRUCTURALLY (#1154): this used to patch `send_private_dm` on
        # `run_automation` and assert the mock was never called, which passed whether the artifact
        # path avoided the direct-send task or merely reached it through another namespace. The DM
        # tasks live in `app.engagement.outreach`; the delivery module has no binding for one at
        # all, and an absent binding cannot be reached.
        from cqc_lem.app.engagement import posting as _posting
        assert not hasattr(_posting, "send_private_dm")
        assert not hasattr(_posting, "send_scheduled_dm")

    def test_lead_magnet_is_not_drafted_for_a_commenter_we_cannot_dm(self):
        """The sweep already read the degree badge off the card (issue #1528).

        So a 2nd-degree commenter's un-sendable draft never has to reach the queue at all.
        """
        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        driver = _sweep_driver()
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="post body"), \
             patch(f"{_POST}.click_first", return_value=None), \
             patch(f"{_POST}._comment_items_from_thread",
                   return_value=[_FakeComment("Send me GUIDE please", author="Jane Doe • 2nd")]), \
             patch(f"{_POST}.get_lead_magnet_settings",
                   return_value={"enabled": True, "keyword": "GUIDE", "message": "Here: {blog_url}"}), \
             patch(f"{_POST}.get_user_blog_url", return_value="https://blog"), \
             patch(f"{_POST}.has_received_lead_magnet", return_value=False), \
             patch(f"{_POST}.has_open_scheduled_dm", return_value=False), \
             patch(f"{_POST}.count_scheduled_dms_created_today", return_value=0), \
             patch(f"{_POST}.insert_scheduled_dm") as ins, \
             patch(f"{_POST}.record_lead_magnet_sent") as rec, \
             patch(f"{_POST}.upsert_engager"), \
             patch(f"{_POST}.generate_thread_reply", return_value="reply"), \
             patch(f"{_POST}.get_engagement_preferences", return_value={"max_dms_per_day": 5}), \
             patch(f"{_POST}._flag_lead_signal", return_value=None), \
             patch(f"{_POST}._reply_to_comment_inline", return_value=True) as rep, \
             patch(f"{_POST}.insert_new_log"):
            _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        ins.assert_not_called()
        rec.assert_not_called()
        # The PUBLIC reply is unaffected — a 2nd-degree commenter is still someone to talk to.
        rep.assert_called_once()

    def test_bails_when_profile_slug_unresolvable(self):
        """LOOP SAFETY: with no profile slug we can't dedup our own / already-replied comments, so
        the sweep must skip replying entirely rather than risk duplicate/self replies.
        """
        from unittest.mock import MagicMock

        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        prof = MagicMock(); prof.profile_url = None; prof.full_name = "Me"
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="body"), \
             patch(f"{_POST}.click_first", return_value=None), \
             patch(f"{_POST}._comment_items_from_thread", return_value=[_FakeComment("hi")]), \
             patch(f"{_POST}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_POST}.log_warning"), \
             patch(f"{_POST}.generate_thread_reply") as gen, \
             patch(f"{_POST}._reply_to_comment_inline") as rep:
            result = _reply_to_comments_on_open_post(_sweep_driver(), MagicMock(), 1, 9, prof, "s")
        assert "no profile slug" in result["summary"].lower()
        assert result["status"] == "no_profile_slug"
        gen.assert_not_called()
        rep.assert_not_called()

    def test_reply_cap_limits_burst(self):
        from unittest.mock import MagicMock

        from cqc_lem.app.engagement.posting import _MAX_REPLIES_PER_SWEEP, _reply_to_comments_on_open_post
        boxes = [_FakeComment(f"comment number {i}") for i in range(_MAX_REPLIES_PER_SWEEP + 5)]
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="body"), \
             patch(f"{_POST}.click_first", return_value=None), \
             patch(f"{_POST}._comment_items_from_thread", return_value=boxes), \
             patch(f"{_POST}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_POST}.upsert_engager"), \
             patch(f"{_POST}.generate_thread_reply", return_value="reply"), \
             patch(f"{_POST}.get_engagement_preferences", return_value={}), \
             patch(f"{_POST}._flag_lead_signal", return_value=None), \
             patch(f"{_POST}._reply_to_comment_inline", return_value=True) as rep, \
             patch(f"{_POST}.insert_new_log"):
            _reply_to_comments_on_open_post(_sweep_driver(), MagicMock(), 1, 9, self._profile(), "s")
        assert rep.call_count == _MAX_REPLIES_PER_SWEEP

    def test_no_post_url_returns_early(self):
        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value=None):
            result = _reply_to_comments_on_open_post(_sweep_driver(), MagicMock(), 1, 9, self._profile(), "s")
        assert result["summary"] == "No post URL"
        assert result["status"] == "no_post_url"

    def test_skips_own_comment(self):
        """A seed or second-wave self-comment must never be treated as a target for a reply;
        replying to our own comment looks like the user talking to themselves in the activity feed.
        """
        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        driver = _sweep_driver()
        own = _FakeComment("Here is my seed comment insight", author="Me Myself",
                           href="https://www.linkedin.com/in/me")
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="post body"), \
             patch(f"{_POST}.click_first", return_value=None), \
             patch(f"{_POST}._comment_items_from_thread", return_value=[own]), \
             patch(f"{_POST}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_POST}.generate_thread_reply") as gen, \
             patch(f"{_POST}.get_engagement_preferences", return_value={}), \
             patch(f"{_POST}._reply_to_comment_inline") as rep:
            result = _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        gen.assert_not_called()
        rep.assert_not_called()
        assert result == {"status": "ok", "summary": "Replied to 0 comments",
                          "comments_found": 1, "replies_sent": 0}

    def test_redis_dedup_prevents_cross_sweep_duplicate(self):
        """Issue #775: even if the DOM no longer shows our previous reply, Redis remembers we already
        replied to this commenter+text on this post and stops a second reply.
        """
        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        driver = _sweep_driver()
        redis = MagicMock(); redis.get.return_value = b"1"
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="post body"), \
             patch(f"{_POST}.click_first", return_value=None), \
             patch(f"{_POST}._comment_items_from_thread", return_value=[_FakeComment("Great post")]), \
             patch(f"{_POST}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_POST}.generate_thread_reply") as gen, \
             patch(f"{_POST}.get_engagement_preferences", return_value={"reply_max_post_age_days": 2}), \
             patch(f"{_POST}._redis_client", return_value=redis), \
             patch(f"{_POST}._reply_to_comment_inline") as rep, \
             patch(f"{_POST}.insert_new_log"):
            result = _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        gen.assert_not_called()
        rep.assert_not_called()
        assert result == {"status": "ok", "summary": "Replied to 0 comments",
                          "comments_found": 1, "replies_sent": 0}

    def test_records_replied_to_comment_after_successful_post(self):
        """After a reply lands, a Redis marker is written so later sweeps deduplicate the target."""
        from cqc_lem.app.engagement.posting import _reply_to_comments_on_open_post
        driver = _sweep_driver()
        redis = MagicMock(); redis.get.return_value = None
        with patch(f"{_POST}.get_post_url_from_log_for_user", return_value="https://li/feed/update/urn:li:share:1/"), \
             patch(f"{_POST}.get_post_content", return_value="post body"), \
             patch(f"{_POST}.click_first", return_value=None), \
             patch(f"{_POST}._comment_items_from_thread", return_value=[_FakeComment("Nice post")]), \
             patch(f"{_POST}.get_lead_magnet_settings", return_value={"enabled": False}), \
             patch(f"{_POST}.generate_thread_reply", return_value="Thanks!"), \
             patch(f"{_POST}.get_engagement_preferences", return_value={"reply_max_post_age_days": 3}), \
             patch(f"{_POST}._redis_client", return_value=redis), \
             patch(f"{_POST}._reply_to_comment_inline", return_value=True), \
             patch(f"{_POST}.insert_new_log"):
            _reply_to_comments_on_open_post(driver, MagicMock(), 1, 9, self._profile(), "synth")
        assert redis.set.call_count == 1
        key = redis.set.call_args.args[0]
        assert key.startswith("linkedin:replied_to_own_comment:1:9:")
        # TTL = (look-back days + 1) * 24h, clamped; 3 + 1 = 4 days.
        assert redis.set.call_args.kwargs["ex"] == 4 * 24 * 60 * 60


class TestAutomateReplyCommenting:
    def test_rate_limited_returns_clean_skip(self):
        from cqc_lem.app.engagement.posting import automate_reply_commenting
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        with patch(f"{_POST}.get_current_profile", side_effect=LinkedInRateLimited("429")), \
             patch(f"{_POST}.log_warning") as warn:
            result = automate_reply_commenting.run(user_id=1, post_id=9, loop_for_duration=0)
        assert "rate limited" in result.lower()
        warn.assert_called_once()

    def test_single_pass_no_requeue_when_loop_zero(self):
        from cqc_lem.app.engagement.posting import automate_reply_commenting
        with patch(f"{_POST}.get_current_profile", return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_POST}.get_or_create_profile_synthesis", return_value="synth"), \
             patch(f"{_POST}._reply_to_comments_on_open_post",
                   return_value={"status": "ok", "summary": "Replied to 2 comments",
                                 "comments_found": 2, "replies_sent": 2}) as helper, \
             patch(f"{_POST}.quit_gracefully"):
            result = automate_reply_commenting.run(user_id=1, post_id=9, loop_for_duration=0)
        helper.assert_called_once()
        assert "Replied to 2 comments" in result


class TestFeedFunnelStorage:
    def test_set_and_get_round_trip(self):
        import json
        redis = MagicMock()
        with patch(f"{_FEED}._redis_client", return_value=redis):
            from cqc_lem.app.engagement.feed import get_feed_funnel, set_feed_funnel
            set_feed_funnel(1, {"examined": 5, "commented": 2})
            stored = redis.set.call_args
            assert stored.args[0] == "linkedin:feed_funnel:1"
            assert json.loads(stored.args[1]) == {"examined": 5, "commented": 2}
            assert stored.kwargs["ex"] == 30 * 24 * 60 * 60
            redis.get.return_value = stored.args[1]
            assert get_feed_funnel(1) == {"examined": 5, "commented": 2}

    def test_get_returns_none_when_absent(self):
        redis = MagicMock(); redis.get.return_value = None
        with patch(f"{_FEED}._redis_client", return_value=redis):
            from cqc_lem.app.engagement.feed import get_feed_funnel
            assert get_feed_funnel(1) is None

    def test_no_redis_is_safe(self):
        with patch(f"{_FEED}._redis_client", return_value=None):
            from cqc_lem.app.engagement.feed import get_feed_funnel, set_feed_funnel
            set_feed_funnel(1, {"x": 1})     # must not raise
            assert get_feed_funnel(1) is None

    def test_set_swallows_redis_error(self):
        redis = MagicMock(); redis.set.side_effect = RuntimeError("down")
        with patch(f"{_FEED}._redis_client", return_value=redis), patch(f"{_FEED}.log_warning"):
            from cqc_lem.app.engagement.feed import set_feed_funnel
            set_feed_funnel(1, {"x": 1})     # must not raise

    def test_get_handles_bad_json(self):
        redis = MagicMock(); redis.get.return_value = b"not json"
        with patch(f"{_FEED}._redis_client", return_value=redis):
            from cqc_lem.app.engagement.feed import get_feed_funnel
            assert get_feed_funnel(1) is None
