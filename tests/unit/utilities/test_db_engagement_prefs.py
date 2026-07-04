"""Unit tests for engagement-preferences DB helpers."""

import json
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _mock_conn(fetch_row=None, rowcount=1):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetch_row
    cursor.rowcount = rowcount
    conn.cursor.return_value = cursor
    return conn, cursor


class TestGetEngagementPreferences:
    def test_defaults_when_no_row(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            prefs = get_engagement_preferences(1)
        assert prefs["comment_length"] == "short"
        assert prefs["include_topics"] == [] and prefs["max_comments_per_day"] == 20
        assert prefs["reply_to_own_comments"] is True

    def test_decodes_json_and_bools(self):
        row = {
            "tone": "warm", "comment_length": "long", "comment_style": None,
            "use_emojis": 1, "use_hashtags": 0,
            "include_topics": json.dumps(["AI", "SaaS"]), "exclude_topics": None,
            "include_keywords": "[]", "exclude_keywords": None,
            "include_authors": None, "exclude_authors": None, "post_types": None,
            "min_reactions": 5, "reply_to_own_comments": 0,
            "max_comments_per_day": 15, "max_dms_per_day": 10, "default_buyer_stage": "awareness",
        }
        conn, _ = _mock_conn(fetch_row=row)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            prefs = get_engagement_preferences(1)
        assert prefs["include_topics"] == ["AI", "SaaS"]
        assert prefs["use_emojis"] is True and prefs["use_hashtags"] is False
        assert prefs["reply_to_own_comments"] is False and prefs["min_reactions"] == 5


class TestUpdateEngagementPreferences:
    def test_upserts_with_json_encoded_arrays(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            ok = update_engagement_preferences(7, {
                "tone": "bold", "include_topics": ["AI"], "use_hashtags": True, "max_comments_per_day": 5})
        assert ok is True
        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]
        assert "ON DUPLICATE KEY UPDATE" in sql and params[0] == 7
        # JSON array field is json-encoded somewhere in the params
        assert any(p == json.dumps(["AI"]) for p in params)


class TestCountActions:
    def test_count_comments_today(self):
        conn, cursor = _mock_conn(fetch_row=(3,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_comments_today
            assert count_comments_today(1) == 3
        sql = cursor.execute.call_args[0][0]
        assert "CURDATE()" in sql
