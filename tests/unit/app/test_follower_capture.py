"""Follower & audience telemetry capture (issue #627): the Selenium snapshot, its task, the daily
dispatcher, and the db accessors that persist/read it. All I/O mocked.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# The snapshot and its task moved to `app.engagement.posting` (#1154) — that is the module whose
# globals they read, and since #1206 deleted the `run_automation` shim it is also where the daily
# dispatcher's lazy `from cqc_lem.app.engagement.posting import capture_follower_stats` INSIDE the
# beat resolves, so the dispatch patches below target it too.
_POST = "cqc_lem.app.engagement.posting"
_RS = "cqc_lem.app.run_scheduler"

_PROFILE_TEXT = "Christopher Queen\nFounder\n4,312 followers\n500+ connections"
_ANALYTICS_TEXT = "48\nProfile views\nPast 90 days\n12\nSearch appearances\nPrevious week"


def _driver(texts):
    """A driver whose <main> text is taken from `texts` in navigation order."""
    driver = MagicMock()
    seq = list(texts)

    def _find(_by, _tag):
        el = MagicMock()
        el.text = seq.pop(0) if seq else ""
        return el

    driver.find_element.side_effect = _find
    return driver


class TestCaptureAudienceSnapshot:
    def test_reads_every_signal(self):
        from cqc_lem.app.engagement.posting import capture_audience_snapshot
        driver = _driver([_PROFILE_TEXT, _ANALYTICS_TEXT])
        with patch(f"{_POST}.time.sleep"):
            counts = capture_audience_snapshot(driver, "https://www.linkedin.com/in/me/")
        assert counts == {"follower_count": 4312, "connection_count": 500,
                          "profile_views": 48, "search_appearances": 12}
        assert driver.get.call_args_list[0].args[0] == "https://www.linkedin.com/in/me/"

    def test_missing_anchor_is_none_never_zero(self):
        # A DOM change must record "not measured", not a zero that reads as a lost audience.
        from cqc_lem.app.engagement.posting import capture_audience_snapshot
        driver = _driver(["Christopher Queen\nFounder", "", ""])
        with patch(f"{_POST}.time.sleep"):
            counts = capture_audience_snapshot(driver, "https://www.linkedin.com/in/me/")
        assert set(counts.values()) == {None}

    def test_no_profile_url_skips_the_profile_load(self):
        from cqc_lem.app.engagement.posting import capture_audience_snapshot
        driver = _driver([_ANALYTICS_TEXT])
        with patch(f"{_POST}.time.sleep"):
            counts = capture_audience_snapshot(driver, None)
        assert counts["follower_count"] is None and counts["profile_views"] == 48
        assert len(driver.get.call_args_list) == 1

    def test_search_appearances_page_only_opened_when_needed(self):
        from cqc_lem.app.engagement.posting import capture_audience_snapshot
        # Analytics page yielded views but no appearances → second analytics page is opened.
        driver = _driver([_PROFILE_TEXT, "48\nProfile views", "31\nSearch appearances"])
        with patch(f"{_POST}.time.sleep"):
            counts = capture_audience_snapshot(driver, "https://www.linkedin.com/in/me/")
        assert counts["search_appearances"] == 31
        assert len(driver.get.call_args_list) == 3

    def test_navigation_failure_does_not_raise(self):
        from cqc_lem.app.engagement.posting import capture_audience_snapshot
        driver = MagicMock()
        driver.get.side_effect = Exception("auth wall")
        with patch(f"{_POST}.time.sleep"):
            counts = capture_audience_snapshot(driver, "https://www.linkedin.com/in/me/")
        assert set(counts.values()) == {None}

    def test_falls_back_to_body_when_main_is_absent(self):
        from cqc_lem.app.engagement.posting import capture_audience_snapshot
        driver = MagicMock()
        body = MagicMock()
        body.text = _PROFILE_TEXT

        def _find(_by, tag):
            if tag == "main":
                raise Exception("no main")
            return body

        driver.find_element.side_effect = _find
        with patch(f"{_POST}.time.sleep"):
            counts = capture_audience_snapshot(driver, "https://www.linkedin.com/in/me/")
        assert counts["follower_count"] == 4312

    def test_falls_back_to_body_when_main_is_empty(self):
        # A half-hydrated <main> reads blank; that is as unread as a missing one, so the body
        # fallback must still run instead of recording an all-NULL snapshot.
        from cqc_lem.app.engagement.posting import capture_audience_snapshot
        driver = MagicMock()

        def _find(_by, tag):
            el = MagicMock()
            el.text = "   \n " if tag == "main" else _PROFILE_TEXT
            return el

        driver.find_element.side_effect = _find
        with patch(f"{_POST}.time.sleep"):
            counts = capture_audience_snapshot(driver, "https://www.linkedin.com/in/me/")
        assert counts["follower_count"] == 4312 and counts["connection_count"] == 500


class TestCaptureFollowerStatsTask:
    def _patches(self, counts, profile_url="https://www.linkedin.com/in/me/"):
        return (patch(f"{_POST}.get_current_profile",
                      return_value=(MagicMock(), MagicMock(), "e@x.com", MagicMock())),
                patch(f"{_POST}.get_linkedin_profile_url_by_user_id", return_value=profile_url),
                patch(f"{_POST}.capture_audience_snapshot", return_value=counts),
                patch(f"{_POST}.quit_gracefully"))

    def test_records_and_tracks_the_snapshot(self):
        from cqc_lem.app.engagement.posting import capture_follower_stats
        counts = {"follower_count": 4312, "connection_count": 500,
                  "profile_views": 48, "search_appearances": 12}
        p1, p2, p3, p4 = self._patches(counts)
        with p1, p2, p3, p4, \
             patch(f"{_POST}.record_follower_stat", return_value=True) as rec, \
             patch(f"{_POST}.track_audience_snapshot") as track:
            result = capture_follower_stats.run(user_id=1)
        assert rec.call_args.kwargs == counts
        assert track.call_args.kwargs["follower_count"] == 4312
        assert "4312" in result

    def test_unreadable_snapshot_writes_nothing(self):
        from cqc_lem.app.engagement.posting import capture_follower_stats
        counts = dict.fromkeys(
            ("follower_count", "connection_count", "profile_views", "search_appearances"))
        p1, p2, p3, p4 = self._patches(counts)
        with p1, p2, p3, p4, \
             patch(f"{_POST}.record_follower_stat", return_value=False), \
             patch(f"{_POST}.track_audience_snapshot") as track:
            result = capture_follower_stats.run(user_id=1)
        assert result == "No audience signals readable"
        track.assert_not_called()

    def test_falls_back_to_the_scraped_profile_url(self):
        from cqc_lem.app.engagement.posting import capture_follower_stats
        profile = MagicMock()
        profile.profile_url = "https://www.linkedin.com/in/scraped/"
        with patch(f"{_POST}.get_current_profile",
                   return_value=(MagicMock(), MagicMock(), "e", profile)), \
             patch(f"{_POST}.get_linkedin_profile_url_by_user_id", return_value=None), \
             patch(f"{_POST}.capture_audience_snapshot", return_value={}) as snap, \
             patch(f"{_POST}.record_follower_stat", return_value=True), \
             patch(f"{_POST}.track_audience_snapshot"), patch(f"{_POST}.quit_gracefully"):
            capture_follower_stats.run(user_id=1)
        assert snap.call_args.args[1] == "https://www.linkedin.com/in/scraped/"

    def test_login_failure_returns_without_raising(self):
        from cqc_lem.app.engagement.posting import capture_follower_stats
        with patch(f"{_POST}.get_current_profile", side_effect=Exception("429")):
            assert "Failed" in capture_follower_stats.run(user_id=1)

    def test_capture_error_still_quits_the_driver(self):
        from cqc_lem.app.engagement.posting import capture_follower_stats
        with patch(f"{_POST}.get_current_profile",
                   return_value=(MagicMock(), MagicMock(), "e", MagicMock())), \
             patch(f"{_POST}.get_linkedin_profile_url_by_user_id", return_value="u"), \
             patch(f"{_POST}.capture_audience_snapshot", side_effect=Exception("boom")), \
             patch(f"{_POST}.quit_gracefully") as quit_:
            result = capture_follower_stats.run(user_id=1)
        assert "error" in result.lower()
        quit_.assert_called_once()


class TestDispatcher:
    def test_dispatches_only_users_with_a_session(self):
        from cqc_lem.app.run_scheduler import auto_capture_follower_stats
        task = MagicMock()
        with patch(f"{_RS}._skip_if_throttled", return_value=False), \
             patch(f"{_RS}.get_active_user_ids", return_value=[1, 2, 3]), \
             patch(f"{_RS}.has_linkedin_session", side_effect=lambda uid: uid != 2), \
             patch(f"{_POST}.capture_follower_stats", task):
            result = auto_capture_follower_stats.run()
        assert task.apply_async.call_count == 2
        assert "2/3" in result

    def test_throttled_run_dispatches_nothing(self):
        from cqc_lem.app.run_scheduler import auto_capture_follower_stats
        task = MagicMock()
        with patch(f"{_RS}._skip_if_throttled", return_value=True), \
             patch(f"{_POST}.capture_follower_stats", task):
            assert auto_capture_follower_stats.run() == "Automation throttled"
        task.apply_async.assert_not_called()

    def test_beat_schedule_registers_the_nightly_capture(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["capture-follower-stats"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_capture_follower_stats"


class TestFollowerStatDb:
    def _conn(self, fetchall=None, rowcount=1):
        conn, cur = MagicMock(), MagicMock()
        cur.fetchall.return_value = fetchall or []
        cur.rowcount = rowcount
        conn.cursor.return_value = cur
        return conn, cur

    def test_record_persists_nullable_counts(self):
        from cqc_lem.utilities.db import record_follower_stat
        conn, cur = self._conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert record_follower_stat(1, follower_count=4312, connection_count=None,
                                        profile_views=48, search_appearances=None) is True
        assert "INSERT INTO follower_stats" in cur.execute.call_args[0][0]
        assert cur.execute.call_args[0][1] == (1, 4312, None, 48, None)

    def test_all_null_snapshot_is_not_written(self):
        from cqc_lem.utilities.db import record_follower_stat
        conn, cur = self._conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn) as get_conn:
            assert record_follower_stat(1) is False
        get_conn.assert_not_called()
        cur.execute.assert_not_called()

    def test_record_swallows_db_error(self):
        import mysql.connector

        from cqc_lem.utilities.db import record_follower_stat
        conn, cur = self._conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert record_follower_stat(1, follower_count=5) is False

    def test_get_stats_windows_by_days(self):
        from cqc_lem.utilities.db import get_follower_stats
        rows = [{"id": 1, "follower_count": 10, "captured_at": datetime(2026, 7, 26)}]
        conn, cur = self._conn(fetchall=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_follower_stats(1, days=30) == rows
        sql, params = cur.execute.call_args[0]
        assert "INTERVAL %s DAY" in sql and params == (1, 30, 400)

    def test_get_stats_without_window_omits_the_interval(self):
        from cqc_lem.utilities.db import get_follower_stats
        conn, cur = self._conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            get_follower_stats(1)
        sql, params = cur.execute.call_args[0]
        assert "INTERVAL" not in sql and params == (1, 400)

    def test_get_stats_swallows_db_error(self):
        import mysql.connector

        from cqc_lem.utilities.db import get_follower_stats
        conn, cur = self._conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_follower_stats(1) == []

    def test_daily_action_counts_only_counts_successes(self):
        from cqc_lem.utilities.db import get_daily_action_counts
        conn, cur = self._conn(fetchall=[{"date": "2026-07-26", "action_type": "comment", "count": 4}])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_daily_action_counts(1, days=30)[0]["count"] == 4
        sql, params = cur.execute.call_args[0]
        assert "GROUP BY DATE(created_at), action_type" in sql
        assert params == (1, "success", "post", "comment", "reply", "dm", 30)

    def test_daily_action_counts_honours_explicit_types(self):
        from cqc_lem.utilities.db import get_daily_action_counts
        conn, cur = self._conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            get_daily_action_counts(1, days=7, action_types=["post"])
        assert cur.execute.call_args[0][1] == (1, "success", "post", 7)

    def test_daily_action_counts_with_no_types_hits_no_db(self):
        from cqc_lem.utilities.db import get_daily_action_counts
        with patch("cqc_lem.platform.db.connection.get_db_connection") as get_conn:
            assert get_daily_action_counts(1, action_types=[]) == []
        get_conn.assert_not_called()

    def test_daily_action_counts_swallows_db_error(self):
        import mysql.connector

        from cqc_lem.utilities.db import get_daily_action_counts
        conn, cur = self._conn()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert get_daily_action_counts(1) == []


class TestAudienceSnapshotEvent:
    def test_emits_posthog_event_with_null_preserved(self):
        from cqc_lem.utilities.observability import track_audience_snapshot
        with patch("cqc_lem.utilities.observability.posthog") as ph:
            track_audience_snapshot(user_id=1, follower_count=4312, profile_views=None)
        props = ph.capture.call_args.kwargs["properties"]
        assert ph.capture.call_args.kwargs["event"] == "audience_snapshot"
        assert props["follower_count"] == 4312 and props["profile_views"] is None
