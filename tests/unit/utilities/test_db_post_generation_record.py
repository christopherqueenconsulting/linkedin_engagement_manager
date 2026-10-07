"""Showcase round 5 repository reads and writes.

`posts.audience` / `posts.story_id`, the recent-post record history, the slot-window texts for the
keyword CTA cooldown, and the stored audience mix.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_CONN = "cqc_lem.platform.db.connection.get_db_connection"
_USERS = "cqc_lem.platform.db.repositories.users"


class TestUpdatePostGenerationRecord:
    def test_writes_both_columns(self, fake_cursor):
        conn, cursor = fake_cursor(rowcount=1)
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import update_post_generation_record
            assert update_post_generation_record(5, "secondary", 7) is True
        sql, params = cursor.execute.call_args[0]
        assert "SET audience = %s, story_id = %s WHERE id = %s" in sql
        assert params == ("secondary", 7, 5)

    def test_a_write_failure_is_false(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import update_post_generation_record
            assert update_post_generation_record(5, None, None) is False


class TestGetRecentPostRecords:
    def test_reads_newest_first_excluding_the_post_being_written(self, fake_cursor):
        rows = [{"id": 9, "content": "x", "audience": "primary", "story_id": 7, "topic": None,
                 "created_at": datetime.now()}]
        conn, cursor = fake_cursor(fetch_all=rows)
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_records
            assert get_recent_post_records(1, limit=9, exclude_post_id=4) == rows
        sql, params = cursor.execute.call_args[0]
        assert "ORDER BY id DESC LIMIT %s" in sql and "AND id <> %s" in sql
        assert "audience IS NOT NULL" in sql and "story_id IS NOT NULL" in sql
        assert params[0] == 1 and params[-2:] == (4, 9) and "planning" in params

    def test_the_day_window_keeps_older_posts_inside_it(self, fake_cursor):
        now = datetime.now()
        rows = [{"id": 3, "created_at": now}, {"id": 2, "created_at": now - timedelta(days=3)},
                {"id": 1, "created_at": now - timedelta(days=30)}]
        conn, cursor = fake_cursor(fetch_all=rows)
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_records
            out = get_recent_post_records(1, limit=1, within_days=14)
        assert [r["id"] for r in out] == [3, 2]
        assert cursor.execute.call_args[0][1][-1] == 200  # bounded scan

    def test_a_read_failure_is_none_not_an_empty_history(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_records
            assert get_recent_post_records(1) is None


class TestGetPostTextsNearSlot:
    def test_measures_the_window_on_the_slots(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_all=[("Comment AUDIT",), ("Plain",)])
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_post_texts_near_slot
            assert get_post_texts_near_slot(1, 40, 7) == ["Comment AUDIT", "Plain"]
        sql, params = cursor.execute.call_args[0]
        assert "JOIN posts me ON me.id = %s" in sql and "scheduled_time BETWEEN" in sql
        assert params[:2] == (40, 1) and params[-2:] == (7, 7)

    def test_without_a_post_it_reads_recent_creations(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_all=[])
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_post_texts_near_slot
            assert get_post_texts_near_slot(1, None, 7) == []
        assert "created_at >= NOW() - INTERVAL %s DAY" in cursor.execute.call_args[0][0]

    def test_a_read_failure_is_none(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_post_texts_near_slot
            assert get_post_texts_near_slot(1, 40, 7) is None


class TestStoredAudienceMix:
    MIX = {"primary_audience": "ops leaders", "secondary_audience": "small-business owners",
           "secondary_share": 0.6, "secondary_focus_topics": ["cash flow"]}

    def test_the_read_decodes_the_column(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"audience_mix": json.dumps(self.MIX)})
        with patch(_CONN, return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["audience_mix"] == self.MIX

    def test_the_upsert_normalizes_and_an_empty_mix_is_null(self, fake_cursor):
        for given, stored in (({**self.MIX, "secondary_share": 4}, {**self.MIX,
                                                                    "secondary_share": 1.0}),
                              ({}, None)):
            conn, cursor = fake_cursor(fetch_one=None, rowcount=1)
            with patch(_CONN, return_value=conn), \
                 patch(f"{_USERS}.max_catchup_touches_allowed", return_value=5):
                from cqc_lem.utilities.db import _ENGAGEMENT_COLS, update_engagement_preferences
                assert update_engagement_preferences(1, {"audience_mix": given}) is True
            saved = dict(zip(_ENGAGEMENT_COLS, cursor.execute.call_args[0][1][1:]))
            assert saved["audience_mix"] == (None if stored is None else json.dumps(stored))
