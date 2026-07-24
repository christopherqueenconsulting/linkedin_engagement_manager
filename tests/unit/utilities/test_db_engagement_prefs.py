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
        assert prefs["comment_length"] == "medium"
        assert prefs["include_topics"] == [] and prefs["max_comments_per_day"] == 20
        assert prefs["reply_to_own_comments"] is True
        # Focus/goal steering fields default to empty
        assert prefs["focus_topics"] == []
        assert prefs["business_goals"] is None and prefs["personal_goals"] is None

    def test_decodes_focus_and_goals(self):
        conn, _ = _mock_conn(fetch_row={
            "focus_topics": json.dumps(["B2B sales", "leadership"]),
            "business_goals": "Book discovery calls", "personal_goals": "Grow authority",
        })
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            prefs = get_engagement_preferences(1)
        assert prefs["focus_topics"] == ["B2B sales", "leadership"]
        assert prefs["business_goals"] == "Book discovery calls"
        assert prefs["personal_goals"] == "Grow authority"

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

    def test_persists_focus_and_goals(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            ok = update_engagement_preferences(9, {
                "focus_topics": ["AI adoption"], "business_goals": "5 calls/mo",
                "personal_goals": "thought leader"})
        assert ok is True
        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]
        assert "focus_topics" in sql and "business_goals" in sql and "personal_goals" in sql
        assert any(p == json.dumps(["AI adoption"]) for p in params)
        assert "5 calls/mo" in params and "thought leader" in params


class TestReplyCheckConfig:
    def test_defaults_include_reply_config(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            prefs = get_engagement_preferences(1)
        assert prefs["reply_check_mode"] == "event"
        assert prefs["reply_sweeps_per_day"] == 2
        assert prefs["reply_max_post_age_days"] == 2

    def test_clamps_bad_mode_and_out_of_range_numbers(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            update_engagement_preferences(3, {
                "reply_check_mode": "bogus", "reply_sweeps_per_day": 99, "reply_max_post_age_days": 0})
        cols = list(__import__("cqc_lem.utilities.db", fromlist=["_ENGAGEMENT_COLS"])._ENGAGEMENT_COLS)
        params = cursor.execute.call_args[0][1]
        # params = [user_id] + one per col, in _ENGAGEMENT_COLS order
        by_col = dict(zip(cols, params[1:]))
        assert by_col["reply_check_mode"] == "event"      # bad → safe default
        assert by_col["reply_sweeps_per_day"] == 12        # clamped to max
        assert by_col["reply_max_post_age_days"] == 1      # clamped to min

    def test_valid_mode_and_floor_preserved(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            update_engagement_preferences(3, {"reply_check_mode": "scheduled", "reply_sweeps_per_day": 1})
        cols = list(__import__("cqc_lem.utilities.db", fromlist=["_ENGAGEMENT_COLS"])._ENGAGEMENT_COLS)
        by_col = dict(zip(cols, cursor.execute.call_args[0][1][1:]))
        assert by_col["reply_check_mode"] == "scheduled"
        assert by_col["reply_sweeps_per_day"] == 2          # floor

    def test_connection_request_mode_default_and_coercion(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["connection_request_mode"] == "auto_approve"
        conn, cursor = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            update_engagement_preferences(3, {"connection_request_mode": "bogus"})
        cols = list(__import__("cqc_lem.utilities.db", fromlist=["_ENGAGEMENT_COLS"])._ENGAGEMENT_COLS)
        by_col = dict(zip(cols, cursor.execute.call_args[0][1][1:]))
        assert by_col["connection_request_mode"] == "auto_approve"  # bad → safe default

    def test_connection_request_mode_valid_preserved(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            update_engagement_preferences(3, {"connection_request_mode": "pre_review"})
        cols = list(__import__("cqc_lem.utilities.db", fromlist=["_ENGAGEMENT_COLS"])._ENGAGEMENT_COLS)
        by_col = dict(zip(cols, cursor.execute.call_args[0][1][1:]))
        assert by_col["connection_request_mode"] == "pre_review"


class TestFeedFallbackPref:
    def test_default_true(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["feed_fallback_when_empty"] is True

    def test_decodes_as_bool(self):
        conn, _ = _mock_conn(fetch_row={"feed_fallback_when_empty": 0})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["feed_fallback_when_empty"] is False

    def test_persists_as_int(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            update_engagement_preferences(3, {"feed_fallback_when_empty": False})
        cols = list(__import__("cqc_lem.utilities.db", fromlist=["_ENGAGEMENT_COLS"])._ENGAGEMENT_COLS)
        by_col = dict(zip(cols, cursor.execute.call_args[0][1][1:]))
        assert by_col["feed_fallback_when_empty"] == 0


class TestReplyInboundToken:
    def test_returns_existing_token(self):
        conn, cursor = _mock_conn(fetch_row=("existingtoken",))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_or_create_reply_inbound_token
            assert get_or_create_reply_inbound_token(1) == "existingtoken"
        # no UPDATE issued when a token already exists
        assert all("UPDATE" not in (c.args[0] if c.args else "") for c in cursor.execute.call_args_list)

    def test_mints_when_missing(self):
        conn, cursor = _mock_conn(fetch_row=(None,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_or_create_reply_inbound_token
            token = get_or_create_reply_inbound_token(1)
        assert token and len(token) == 20
        assert any("UPDATE users SET reply_inbound_token" in (c.args[0] if c.args else "")
                   for c in cursor.execute.call_args_list)

    def test_reverse_lookup(self):
        conn, _ = _mock_conn(fetch_row=(42,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_id_by_reply_token
            assert get_user_id_by_reply_token("abc") == 42

    def test_reverse_lookup_empty_token(self):
        from cqc_lem.utilities.db import get_user_id_by_reply_token
        assert get_user_id_by_reply_token("") is None

    def test_users_with_reply_mode(self):
        conn, cursor = _mock_conn()
        cursor.fetchall.return_value = [(1,), (5,), (9,)]
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_users_with_reply_mode
            assert get_users_with_reply_mode("scheduled") == [1, 5, 9]
        assert "reply_check_mode = %s" in cursor.execute.call_args[0][0]


class TestCountActions:
    def test_count_comments_today(self):
        conn, cursor = _mock_conn(fetch_row=(3,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_comments_today
            assert count_comments_today(1) == 3
        sql = cursor.execute.call_args[0][0]
        assert "CURDATE()" in sql
