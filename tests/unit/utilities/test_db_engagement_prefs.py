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


class TestPartialUpdateKeepsTheRest:
    """Issue #639 — the upsert writes EVERY column, so a partial dict must merge over the user's
    SAVED row. Merging over `_ENGAGEMENT_DEFAULTS` let one sparse caller wipe the whole row."""

    # What a fully-customized row looks like coming back out of MySQL (JSON columns as text,
    # booleans as tinyints), keyed by column, plus what it must decode to in the upsert params.
    _STORED = {
        "tone": "wry", "comment_length": "long", "comment_style": "socratic",
        "use_emojis": 0, "use_hashtags": 1,
        "include_topics": '["AI"]', "exclude_topics": '["crypto"]',
        "include_keywords": '["rag"]', "exclude_keywords": '["nft"]',
        "include_authors": '["https://x/in/a"]', "exclude_authors": '["https://x/in/b"]',
        "post_types": '["text"]', "focus_topics": '["B2B sales"]',
        "business_goals": "5 calls/mo", "personal_goals": "thought leader",
        "authenticity_score_min": 80, "post_similarity_max_pct": 40,
        "min_reactions": 7, "max_post_age_hours": 12, "reply_to_own_comments": 0,
        "max_comments_per_day": 9, "max_dms_per_day": 4, "max_invites_per_day": 3,
        "max_company_page_invites_per_day": 2,
        "connection_request_mode": "pre_review", "connection_targeting_mode": "auto_queue",
        "connection_target_authors": '["https://x/in/guru"]', "min_connection_icp_score": 70,
        "default_buyer_stage": "consideration", "default_video_quality": "standard",
        "reply_check_mode": "scheduled", "reply_sweeps_per_day": 6, "reply_max_post_age_days": 5,
        "feed_fallback_when_empty": 0, "link_in_first_comment": 0,
        "max_catchup_touches_per_day": 4, "catchup_touch_mode": "auto_approve",
        "catchup_event_types": '["promotion"]', "catchup_message_source": "ai",
        "posts_per_week": 5, "posting_days": '[0, 2, 4, 6]',
        "text_post_images": 0,
    }
    # Round-tripped through the upsert, every column persists back exactly as it was stored.
    _EXPECTED = dict(_STORED)

    def test_stored_row_covers_every_column(self):
        from cqc_lem.utilities.db import _ENGAGEMENT_COLS
        assert set(self._STORED) == set(_ENGAGEMENT_COLS)

    def _upsert(self, call):
        conn, cursor = _mock_conn(fetch_row=dict(self._STORED), rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn), \
             patch(f"{_DB}.max_catchup_touches_allowed", return_value=10):
            call()
        cols = list(__import__("cqc_lem.utilities.db", fromlist=["_ENGAGEMENT_COLS"])._ENGAGEMENT_COLS)
        return dict(zip(cols, cursor.execute.call_args[0][1][1:]))

    def test_set_default_video_quality_preserves_every_other_field(self):
        from cqc_lem.utilities.db import set_default_video_quality
        saved = self._upsert(lambda: set_default_video_quality(1, "premium_top"))
        assert saved["default_video_quality"] == "premium_top"
        for col, expected in self._EXPECTED.items():
            if col == "default_video_quality":
                continue
            assert saved[col] == expected, f"{col} was reset by a partial update"

    def test_single_key_update_preserves_every_other_field(self):
        from cqc_lem.utilities.db import update_engagement_preferences
        saved = self._upsert(lambda: update_engagement_preferences(1, {"tone": "blunt"}))
        assert saved["tone"] == "blunt"
        for col, expected in self._EXPECTED.items():
            if col == "tone":
                continue
            assert saved[col] == expected, f"{col} was reset by a partial update"

    def test_explicit_values_still_win_over_the_saved_row(self):
        from cqc_lem.utilities.db import update_engagement_preferences
        saved = self._upsert(lambda: update_engagement_preferences(
            1, {"include_topics": [], "tone": None, "use_hashtags": False}))
        assert saved["include_topics"] == "[]" and saved["tone"] is None
        assert saved["use_hashtags"] == 0
        assert saved["max_comments_per_day"] == 9  # untouched neighbour survives

    def test_new_row_still_gets_the_code_defaults(self):
        conn, cursor = _mock_conn(fetch_row=None, rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn), \
             patch(f"{_DB}.max_catchup_touches_allowed", return_value=5):
            from cqc_lem.utilities.db import set_default_video_quality
            set_default_video_quality(2, "premium")
        cols = list(__import__("cqc_lem.utilities.db", fromlist=["_ENGAGEMENT_COLS"])._ENGAGEMENT_COLS)
        saved = dict(zip(cols, cursor.execute.call_args[0][1][1:]))
        assert saved["default_video_quality"] == "premium"
        assert saved["comment_length"] == "medium" and saved["max_comments_per_day"] == 20

    def test_unreadable_row_aborts_instead_of_overwriting(self):
        """A failed SELECT must not become "write all 39 columns as defaults"."""
        import mysql.connector
        conn, cursor = _mock_conn(rowcount=1)
        cursor.execute.side_effect = mysql.connector.Error(msg="db down")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            assert update_engagement_preferences(1, {"tone": "warm"}) is False
        assert not any("INSERT INTO engagement_preferences" in (c.args[0] if c.args else "")
                       for c in cursor.execute.call_args_list)


class TestEngagementPreferencesAreConfigured:
    """Three-valued (issue #952): a caller that would otherwise write policy defaults over the
    user's own settings has to tell "never configured" from "could not read"."""

    def test_true_when_a_row_exists(self):
        conn, _ = _mock_conn(fetch_row={"tone": "warm"})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import engagement_preferences_are_configured
            assert engagement_preferences_are_configured(1) is True

    def test_false_when_the_user_never_saved_one(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import engagement_preferences_are_configured
            assert engagement_preferences_are_configured(1) is False

    def test_none_and_an_error_when_the_row_cannot_be_read(self):
        import mysql.connector
        conn, cursor = _mock_conn()
        cursor.execute.side_effect = mysql.connector.Error(msg="db down")
        with patch(f"{_DB}.get_db_connection", return_value=conn), \
             patch(f"{_DB}.log_error") as err:
            from cqc_lem.utilities.db import engagement_preferences_are_configured
            assert engagement_preferences_are_configured(1) is None
        assert err.call_count == 1

    def test_it_only_asks_whether_the_row_exists(self):
        """Existence is a `SELECT 1`, not a read of all 41 columns — one query, one semantics, and
        `has_engagement_preferences` is this same question."""
        conn, cursor = _mock_conn(fetch_row=(1,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import engagement_preferences_are_configured
            engagement_preferences_are_configured(1)
        sql = cursor.execute.call_args.args[0]
        assert "SELECT 1 FROM engagement_preferences" in sql and "LIMIT 1" in sql

    def test_has_engagement_preferences_is_the_two_valued_view(self):
        """The bool helper folds the unreadable case back into False, exactly as before — but there
        is only ONE query behind both, so they can never drift apart."""
        import mysql.connector
        conn, cursor = _mock_conn(fetch_row=(1,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_engagement_preferences
            assert has_engagement_preferences(1) is True
        conn, cursor = _mock_conn()
        cursor.execute.side_effect = mysql.connector.Error(msg="db down")
        with patch(f"{_DB}.get_db_connection", return_value=conn), patch(f"{_DB}.log_error"):
            from cqc_lem.utilities.db import has_engagement_preferences
            assert has_engagement_preferences(1) is False


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


class TestPostsPerWeekPref:
    """Publishing cadence (issue #621) — 3/week by default, never daily by accident."""

    def _saved(self, cursor):
        cols = list(__import__("cqc_lem.utilities.db", fromlist=["_ENGAGEMENT_COLS"])._ENGAGEMENT_COLS)
        return dict(zip(cols, cursor.execute.call_args[0][1][1:]))

    def test_default_is_three_a_week(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences, DEFAULT_POSTS_PER_WEEK
            assert get_engagement_preferences(1)["posts_per_week"] == DEFAULT_POSTS_PER_WEEK == 3

    def test_null_column_reads_as_the_default(self):
        conn, _ = _mock_conn(fetch_row={"posts_per_week": None})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["posts_per_week"] == 3

    def test_saved_value_is_preserved(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            update_engagement_preferences(3, {"posts_per_week": 4})
        assert self._saved(cursor)["posts_per_week"] == 4

    def test_out_of_range_values_are_clamped(self):
        from cqc_lem.utilities.db import POSTS_PER_WEEK_MIN, POSTS_PER_WEEK_MAX
        for given, expected in ((0, POSTS_PER_WEEK_MIN), (99, POSTS_PER_WEEK_MAX),
                                ("nonsense", 3), (None, 3)):
            conn, cursor = _mock_conn(rowcount=1)
            with patch(f"{_DB}.get_db_connection", return_value=conn):
                from cqc_lem.utilities.db import update_engagement_preferences
                update_engagement_preferences(3, {"posts_per_week": given})
            assert self._saved(cursor)["posts_per_week"] == expected


class TestPostingDays:
    """The publishing day allow-list (issue #581) — Mon-Fri by default, weekends opt-in, and never
    an empty set that would schedule nothing."""

    def _saved(self, cursor):
        cols = list(__import__("cqc_lem.utilities.db", fromlist=["_ENGAGEMENT_COLS"])._ENGAGEMENT_COLS)
        return dict(zip(cols, cursor.execute.call_args[0][1][1:]))

    def test_default_is_monday_to_friday(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences, DEFAULT_POSTING_DAYS
            assert get_engagement_preferences(1)["posting_days"] == DEFAULT_POSTING_DAYS == [0, 1, 2, 3, 4]

    def test_null_column_reads_as_monday_to_friday(self):
        conn, _ = _mock_conn(fetch_row={"posting_days": None})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["posting_days"] == [0, 1, 2, 3, 4]

    def test_saved_row_decodes_its_own_days(self):
        conn, _ = _mock_conn(fetch_row={"posting_days": "[5, 6]"})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["posting_days"] == [5, 6]

    def test_all_seven_days_stay_selectable(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            update_engagement_preferences(3, {"posting_days": [6, 5, 4, 3, 2, 1, 0]})
        assert self._saved(cursor)["posting_days"] == "[0, 1, 2, 3, 4, 5, 6]"

    def test_bad_values_fall_back_to_the_default_instead_of_rolling_back(self):
        # The whole prefs row upserts at once (the V52 lesson), so nothing unusable may reach MySQL.
        for given in ([], None, "nonsense", [9, -1], ["mon", "tue"], {}):
            conn, cursor = _mock_conn(rowcount=1)
            with patch(f"{_DB}.get_db_connection", return_value=conn):
                from cqc_lem.utilities.db import update_engagement_preferences
                update_engagement_preferences(3, {"posting_days": given})
            assert self._saved(cursor)["posting_days"] == "[0, 1, 2, 3, 4]", given

    def test_duplicates_and_order_are_normalised(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            update_engagement_preferences(3, {"posting_days": [4, 0, 4, "2"]})
        assert self._saved(cursor)["posting_days"] == "[0, 2, 4]"

    def test_partial_valid_input_keeps_only_the_valid_days(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            update_engagement_preferences(3, {"posting_days": [1, 42, "x"]})
        assert self._saved(cursor)["posting_days"] == "[1]"


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
