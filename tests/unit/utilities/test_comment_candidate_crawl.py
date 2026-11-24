from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.app.engagement import feed
from cqc_lem.utilities import comment_candidate_crawl as crawl

M = "cqc_lem.utilities.comment_candidate_crawl"


def _utc(day: int, hour: int) -> datetime:
    # 2026-11-23 is a Monday
    return datetime(2026, 11, day, hour, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("day,hour,expected", [
    (23, 15, True),    # Monday 10:00 New York
    (23, 14, True),    # 09:00 — first hour
    (23, 13, False),   # 08:00
    (23, 21, True),    # 16:00 — last hour
    (23, 22, False),   # 17:00 — window closed
    (24, 1, False),    # Monday 20:00 local
    (28, 16, False),   # Saturday
    (29, 16, False),   # Sunday
])
def test_window_is_weekday_daytime_local(day, hour, expected):
    assert crawl.crawl_window_open("America/New_York", _utc(day, hour)) is expected


def test_window_never_opens_between_19_and_07_in_any_zone():
    for h in range(24):
        for zone in ("America/New_York", "Asia/Tokyo", "Europe/London"):
            now = _utc(25, h)
            local_hour = now.astimezone(__import__("zoneinfo").ZoneInfo(zone)).hour
            if local_hour >= 19 or local_hour < 7:
                assert crawl.crawl_window_open(zone, now) is False


def test_unknown_timezone_is_closed():
    assert crawl.crawl_window_open("Not/AZone", _utc(23, 15)) is False


def test_enabled_needs_both_flags():
    with patch(f"{M}.flag_enabled", return_value=True), patch(f"{M}.pool_enabled", return_value=False):
        assert crawl.crawl_enabled(1) is False
    with patch(f"{M}.flag_enabled", return_value=False), patch(f"{M}.pool_enabled", return_value=True):
        assert crawl.crawl_enabled(1) is False
    with patch(f"{M}.flag_enabled", return_value=True), patch(f"{M}.pool_enabled", return_value=True):
        assert crawl.crawl_enabled(1) is True


def test_enabled_false_on_fault():
    with patch(f"{M}.flag_enabled", side_effect=RuntimeError):
        assert crawl.crawl_enabled(1) is False


def test_cap_env_and_budget(monkeypatch):
    monkeypatch.setenv("COMMENT_CRAWL_DAILY_CAP", "10")
    with patch(f"{M}.count_comment_candidates", return_value=4) as count:
        assert crawl.crawl_budget(1) == 6
        count.assert_called_once_with(1, source="crawl")
    with patch(f"{M}.count_comment_candidates", return_value=15):
        assert crawl.crawl_budget(1) == 0
    with patch(f"{M}.count_comment_candidates", return_value=None):
        assert crawl.crawl_budget(1) == 0
    monkeypatch.setenv("COMMENT_CRAWL_DAILY_CAP", "junk")
    assert crawl.crawl_daily_cap() == crawl.DEFAULT_DAILY_CAP


def test_due_combines_flags_and_window():
    with patch(f"{M}.crawl_enabled", return_value=True), \
            patch(f"{M}.get_user_timezone", return_value="America/New_York"):
        assert crawl.crawl_due(1, _utc(23, 15)) is True
        assert crawl.crawl_due(1, _utc(28, 15)) is False
    with patch(f"{M}.crawl_enabled", return_value=False):
        assert crawl.crawl_due(1, _utc(23, 15)) is False


# --- the task and the walk -------------------------------------------------

def test_walk_with_no_budget_reads_nothing():
    driver = MagicMock()
    assert feed.crawl_roster_candidates(driver, 1, {}, 0)["written"] == 0
    driver.get.assert_not_called()


def test_walk_records_crawl_source_and_stops_at_budget():
    driver = MagicMock()
    boxes = [MagicMock(text="x" * 40) for _ in range(4)]
    driver.find_elements.return_value = boxes
    with patch.object(feed, "get_engagement_targets", return_value=[{"id": 1, "profile_url": "u"}]), \
            patch.object(feed, "select_roster_targets", side_effect=lambda t, n: t), \
            patch.object(feed, "_roster_activity_url", return_value="https://x/recent"), \
            patch.object(feed, "wait_for_ajax"), patch.object(feed.time, "sleep"), \
            patch.object(feed, "record_roster_visit"), \
            patch.object(feed, "_card_for_textbox", return_value=MagicMock()), \
            patch.object(feed, "_post_author_from_card", return_value="Ann"), \
            patch.object(feed, "_feed_post_identity", side_effect=[("k%d" % i, "urn") for i in range(4)]), \
            patch.object(feed, "_passes_hard_excludes", return_value=True), \
            patch.object(feed, "_post_social_counts", return_value={"reactions": 5}), \
            patch.object(feed, "_post_age_minutes", return_value=10), \
            patch.object(feed, "_literal_relevant", return_value=True), \
            patch.object(feed, "focus_affinity", return_value=0.5), \
            patch.object(feed, "_post_permalink_from_card", return_value="https://p"), \
            patch.object(feed, "record_comment_candidate", return_value=True) as rec:
        stats = feed.crawl_roster_candidates(driver, 1, {}, 2)
    assert stats["written"] == 2
    assert rec.call_count == 2
    assert rec.call_args.args[4] == "crawl"


def test_walk_skips_excluded_posts():
    driver = MagicMock()
    driver.find_elements.return_value = [MagicMock(text="y" * 40)]
    with patch.object(feed, "get_engagement_targets", return_value=[{"id": 1}]), \
            patch.object(feed, "select_roster_targets", side_effect=lambda t, n: t), \
            patch.object(feed, "_roster_activity_url", return_value="https://x"), \
            patch.object(feed, "wait_for_ajax"), patch.object(feed.time, "sleep"), \
            patch.object(feed, "record_roster_visit"), \
            patch.object(feed, "_card_for_textbox", return_value=MagicMock()), \
            patch.object(feed, "_post_author_from_card", return_value="Ann"), \
            patch.object(feed, "_feed_post_identity", return_value=("k", "urn")), \
            patch.object(feed, "_passes_hard_excludes", return_value=False), \
            patch.object(feed, "record_comment_candidate") as rec:
        stats = feed.crawl_roster_candidates(driver, 1, {}, 5)
    assert stats["written"] == 0 and stats["examined"] == 1
    rec.assert_not_called()


def test_task_skips_when_flag_off():
    with patch.object(feed, "skip_browser_lane", return_value=False), \
            patch.object(feed, "crawl_enabled", return_value=False), \
            patch.object(feed, "get_current_profile") as session:
        assert "off" in feed.crawl_comment_candidates.run(1)
    session.assert_not_called()


def test_task_skips_when_cap_reached():
    with patch.object(feed, "skip_browser_lane", return_value=False), \
            patch.object(feed, "crawl_enabled", return_value=True), \
            patch.object(feed, "crawl_budget", return_value=0), \
            patch.object(feed, "get_current_profile") as session:
        assert "cap" in feed.crawl_comment_candidates.run(1)
    session.assert_not_called()


def test_task_runs_walk_reports_and_closes():
    driver = MagicMock()
    with patch.object(feed, "skip_browser_lane", return_value=False), \
            patch.object(feed, "crawl_enabled", return_value=True), \
            patch.object(feed, "crawl_budget", return_value=50), \
            patch.object(feed, "acquire_run_lock", return_value="tok"), \
            patch.object(feed, "release_run_lock") as release, \
            patch.object(feed, "get_current_profile", return_value=(driver, None, "e", None)), \
            patch.object(feed, "get_engagement_preferences", return_value={}), \
            patch.object(feed, "crawl_roster_candidates",
                         return_value={"written": 7, "targets_visited": 3, "examined": 9}), \
            patch.object(feed, "count_comment_candidates", return_value=12), \
            patch.object(feed, "track_comment_candidates") as report, \
            patch.object(feed, "quit_gracefully") as quit_:
        out = feed.crawl_comment_candidates.run(1)
    assert "wrote 7" in out
    report.assert_called_once_with(1, "crawl", 7, 12)
    quit_.assert_called_once_with(driver)
    release.assert_called_once()


def test_task_lock_loss_skips():
    with patch.object(feed, "skip_browser_lane", return_value=False), \
            patch.object(feed, "crawl_enabled", return_value=True), \
            patch.object(feed, "crawl_budget", return_value=5), \
            patch.object(feed, "acquire_run_lock", return_value=None), \
            patch.object(feed, "get_current_profile") as session:
        assert "another" in feed.crawl_comment_candidates.run(1)
    session.assert_not_called()


# --- the dispatcher --------------------------------------------------------

def test_dispatcher_claims_once_and_skips_closed_users():
    from cqc_lem.app import run_scheduler as rs
    client = MagicMock()
    client.set.return_value = True
    with patch.object(rs, "_skip_if_throttled", return_value=False), \
            patch.object(rs, "get_active_user_ids", return_value=[1, 2]), \
            patch.object(rs, "_browser_lane_users", side_effect=lambda u, n: u), \
            patch.object(rs, "has_linkedin_session", return_value=True), \
            patch("cqc_lem.utilities.linkedin.rate_limit._redis_client", return_value=client), \
            patch("cqc_lem.utilities.comment_candidate_crawl.crawl_due", side_effect=[True, False]), \
            patch.object(rs, "dispatch_jitter_seconds", return_value=0), \
            patch.object(rs.crawl_comment_candidates, "apply_async") as dispatch:
        out = rs.dispatch_comment_candidate_crawls()
    assert "1/2" in out
    dispatch.assert_called_once_with(kwargs={"user_id": 1}, countdown=0)


def test_dispatcher_without_redis_dispatches_nothing():
    from cqc_lem.app import run_scheduler as rs
    with patch.object(rs, "_skip_if_throttled", return_value=False), \
            patch.object(rs, "get_active_user_ids", return_value=[1]), \
            patch.object(rs, "_browser_lane_users", side_effect=lambda u, n: u), \
            patch("cqc_lem.utilities.linkedin.rate_limit._redis_client", return_value=None), \
            patch.object(rs.crawl_comment_candidates, "apply_async") as dispatch:
        assert "Redis" in rs.dispatch_comment_candidate_crawls()
    dispatch.assert_not_called()
