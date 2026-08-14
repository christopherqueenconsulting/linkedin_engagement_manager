"""Unit tests for catch-up touch DB helpers and the catch-up engagement preferences (issue #482)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"
_OUTREACH = "cqc_lem.platform.db.repositories.outreach"
_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"


class TestCatchupTouchDb:
    def test_insert_returns_id(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=42)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import CatchupEventType, insert_catchup_touch
            got = insert_catchup_touch(1, "https://x/in/jane", CatchupEventType.JOB_CHANGE, "2026-07",
                                       person_name="Jane", event_detail="started a new position",
                                       message="Congrats!", score=50)
        assert got == 42
        assert "INSERT INTO catchup_touches" in cur.execute.call_args[0][0]

    def test_insert_duplicate_returns_none(self, fake_cursor):
        """The unique key is the dedup guarantee — a duplicate milestone must not raise."""
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="duplicate entry")
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import CatchupEventType, insert_catchup_touch
            assert insert_catchup_touch(1, "u", CatchupEventType.PROMOTION, "2026-07") is None

    def test_get_touch_and_user_id(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"id": 3, "user_id": 9}, lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_catchup_touch, get_catchup_touch_user_id
            assert get_catchup_touch(3)["user_id"] == 9
            assert get_catchup_touch_user_id(3) == 9

    def test_get_touch_user_id_none_when_missing(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None, lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_catchup_touch_user_id
            assert get_catchup_touch_user_id(3) is None

    def test_get_touch_error_returns_none(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_catchup_touch
            assert get_catchup_touch(3) is None

    def test_has_catchup_touch_true_and_false(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(1,), lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import CatchupEventType, has_catchup_touch
            assert has_catchup_touch(1, "https://x/in/jane", CatchupEventType.JOB_CHANGE, "2026-07") is True
        sql = cur.execute.call_args[0][0]
        assert "event_period = %s" in sql
        conn, _ = fake_cursor(fetch_one=None, lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import CatchupEventType, has_catchup_touch
            assert has_catchup_touch(1, "u", CatchupEventType.JOB_CHANGE, "2026-07") is False

    def test_has_catchup_touch_error_fails_open(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import CatchupEventType, has_catchup_touch
            assert has_catchup_touch(1, "u", CatchupEventType.BIRTHDAY, "2026") is False

    def test_list_returns_pagination_shape_and_filters(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7, fetch_one={"c": 2})
        cur.fetchall.return_value = [{"id": 1, "status": "pending", "created_at": None, "updated_at": None}]
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_catchup_touches
            out = get_catchup_touches(1, status_filter="pending", event_type_filter="job_change")
        assert out["total"] == 2 and out["page"] == 1 and len(out["touches"]) == 1
        list_sql = cur.execute.call_args_list[-1][0][0]
        assert "status = %s" in list_sql and "event_type = %s" in list_sql
        assert "ORDER BY score DESC" in list_sql

    def test_list_serializes_datetimes(self, fake_cursor):
        from datetime import datetime
        conn, cur = fake_cursor(lastrowid=7, fetch_one={"c": 1})
        cur.fetchall.return_value = [{"id": 1, "created_at": datetime(2026, 7, 24),
                                      "updated_at": datetime(2026, 7, 24)}]
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_catchup_touches
            out = get_catchup_touches(1)
        assert out["touches"][0]["created_at"] == "2026-07-24T00:00:00"

    def test_list_error_returns_empty_shape(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_catchup_touches
            out = get_catchup_touches(1)
        assert out == {"touches": [], "total": 0, "page": 1, "page_size": 25}

    def test_approved_touches_ordered_by_score(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[(1, 5), (2, 5)], lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_approved_catchup_touches
            assert get_approved_catchup_touches() == [(1, 5), (2, 5)]
        sql = cur.execute.call_args[0][0]
        assert "status = 'approved'" in sql and "ORDER BY score DESC" in sql

    def test_approved_touches_error_returns_empty(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_approved_catchup_touches
            assert get_approved_catchup_touches() == []

    def test_count_pending_backlog(self, fake_cursor):
        """The send drip reads this on every beat (issue #792) — it is what separates a queue sitting
        behind approval from an empty lane, so it must count 'pending' and nothing else.
        """
        conn, cur = fake_cursor(fetch_one=(6,), lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_pending_catchup_touches
            assert count_pending_catchup_touches() == 6
        sql = cur.execute.call_args[0][0]
        assert "status = 'pending'" in sql and "COUNT(*)" in sql

    def test_count_pending_backlog_no_row_returns_zero(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None, lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_pending_catchup_touches
            assert count_pending_catchup_touches() == 0

    def test_count_pending_backlog_error_returns_zero(self, fake_cursor):
        """A DB error must read as 'no backlog', not blow up the beat — the drip still has approved
        touches to dispatch and the report is diagnostic, not load-bearing. But 0 makes the beat
        report `nothing_to_send`, so the failure itself has to surface at ERROR, not at INFO.
        """
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_GET_CONN}", return_value=conn), \
             patch(f"{_OUTREACH}.log_error") as err:
            from cqc_lem.utilities.db import count_pending_catchup_touches
            assert count_pending_catchup_touches() == 0
        err.assert_called_once()

    def test_orphaned_touches_use_lookback(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[(9, 1)], lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_orphaned_catchup_touches
            assert get_orphaned_catchup_touches(lookback_hours=3) == [(9, 1)]
        assert "status = 'sending'" in cur.execute.call_args[0][0]

    def test_orphaned_touches_error_returns_empty(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_orphaned_catchup_touches
            assert get_orphaned_catchup_touches() == []

    def test_count_sent_today(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(4,), lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_catchup_touches_sent_today
            assert count_catchup_touches_sent_today(1) == 4
        assert "status = 'sent'" in cur.execute.call_args[0][0]

    def test_count_sent_today_error_returns_zero(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_catchup_touches_sent_today
            assert count_catchup_touches_sent_today(1) == 0

    def test_update_status(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import CatchupTouchStatus, update_catchup_touch_status
            assert update_catchup_touch_status(3, CatchupTouchStatus.SENT) is True
        assert cur.execute.call_args[0][1] == ("sent", 3)

    def test_update_status_error_returns_false(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import CatchupTouchStatus, update_catchup_touch_status
            assert update_catchup_touch_status(3, CatchupTouchStatus.SENT) is False

    def test_update_fields(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import CatchupTouchStatus, update_catchup_touch
            assert update_catchup_touch(3, message="edited", person_name="Jane",
                                        status=CatchupTouchStatus.APPROVED) is True
        sql = cur.execute.call_args[0][0]
        assert "message = %s" in sql and "person_name = %s" in sql and "status = %s" in sql

    def test_update_with_nothing_to_change_is_a_noop(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import update_catchup_touch
            assert update_catchup_touch(3) is False
        cur.execute.assert_not_called()

    def test_update_error_returns_false(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import update_catchup_touch
            assert update_catchup_touch(3, message="x") is False


class TestCatchupEngagementPrefs:
    def test_defaults_include_the_bd_subset_only(self):
        from cqc_lem.utilities.db import _ENGAGEMENT_DEFAULTS
        assert _ENGAGEMENT_DEFAULTS["catchup_event_types"] == ["job_change", "promotion"]
        assert _ENGAGEMENT_DEFAULTS["catchup_touch_mode"] == "pre_review"
        assert _ENGAGEMENT_DEFAULTS["max_catchup_touches_per_day"] == 5
        # LinkedIn's own pre-drafted response is the baseline — our AI is opt-in (PR #509 owner review).
        assert _ENGAGEMENT_DEFAULTS["catchup_message_source"] == "linkedin"

    def test_null_event_types_column_falls_back_to_the_default(self, fake_cursor):
        """Rows predating the migration have NULL — that means 'never configured', not 'all off'."""
        conn, _ = fake_cursor(fetch_one={"catchup_event_types": None, "include_topics": None,
                                   "exclude_topics": None, "include_keywords": None,
                                   "exclude_keywords": None, "include_authors": None,
                                   "exclude_authors": None, "post_types": None, "focus_topics": None,
                                   "use_emojis": 1, "use_hashtags": 0, "reply_to_own_comments": 1,
                                   "feed_fallback_when_empty": 1, "link_in_first_comment": 1}, lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            prefs = get_engagement_preferences(1)
        assert prefs["catchup_event_types"] == ["job_change", "promotion"]

    def test_explicit_empty_event_types_stays_empty(self, fake_cursor):
        """An explicit [] means the user turned catch-up off — it must NOT be re-defaulted on."""
        conn, _ = fake_cursor(fetch_one={"catchup_event_types": "[]", "include_topics": None,
                                   "exclude_topics": None, "include_keywords": None,
                                   "exclude_keywords": None, "include_authors": None,
                                   "exclude_authors": None, "post_types": None, "focus_topics": None,
                                   "use_emojis": 1, "use_hashtags": 0, "reply_to_own_comments": 1,
                                   "feed_fallback_when_empty": 1, "link_in_first_comment": 1}, lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            prefs = get_engagement_preferences(1)
        assert prefs["catchup_event_types"] == []

    @pytest.mark.parametrize("given,expected", [
        (100, 5), (10, 5), (-3, 0), ("nope", 5), (None, 5), (4, 4),
    ])
    def test_cap_is_clamped_to_the_standard_allowance_on_upsert(self, given, expected, fake_cursor):
        """Without a premium plan the cap tops out at 5/day, whatever the client sent."""
        conn, cur = fake_cursor(lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn), \
             patch(f"{_DB}.get_user_subscription_info", return_value=None):
            from cqc_lem.utilities.db import _ENGAGEMENT_COLS, update_engagement_preferences
            update_engagement_preferences(1, {"max_catchup_touches_per_day": given})
        values = cur.execute.call_args[0][1]
        assert values[1 + _ENGAGEMENT_COLS.index("max_catchup_touches_per_day")] == expected

    @pytest.mark.parametrize("given,expected", [(100, 10), (10, 10), (7, 7)])
    def test_premium_plan_unlocks_the_higher_cap_on_upsert(self, given, expected, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn), \
             patch(f"{_DB}.get_user_subscription_info",
                   return_value={"subscription_tier": "professional", "subscription_status": "active"}):
            from cqc_lem.utilities.db import _ENGAGEMENT_COLS, update_engagement_preferences
            update_engagement_preferences(1, {"max_catchup_touches_per_day": given})
        values = cur.execute.call_args[0][1]
        assert values[1 + _ENGAGEMENT_COLS.index("max_catchup_touches_per_day")] == expected

    @pytest.mark.parametrize("given,expected", [("ai", "ai"), ("linkedin", "linkedin"),
                                                ("yolo", "linkedin"), (None, "linkedin")])
    def test_message_source_is_validated_on_upsert(self, given, expected, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn), \
             patch(f"{_DB}.get_user_subscription_info", return_value=None):
            from cqc_lem.utilities.db import _ENGAGEMENT_COLS, update_engagement_preferences
            update_engagement_preferences(1, {"catchup_message_source": given})
        values = cur.execute.call_args[0][1]
        assert values[1 + _ENGAGEMENT_COLS.index("catchup_message_source")] == expected

    def test_bad_stored_message_source_reads_back_as_linkedin(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"catchup_message_source": "gpt", "catchup_event_types": None,
                                   "include_topics": None, "exclude_topics": None,
                                   "include_keywords": None, "exclude_keywords": None,
                                   "include_authors": None, "exclude_authors": None,
                                   "post_types": None, "focus_topics": None,
                                   "use_emojis": 1, "use_hashtags": 0, "reply_to_own_comments": 1,
                                   "feed_fallback_when_empty": 1, "link_in_first_comment": 1}, lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            assert get_engagement_preferences(1)["catchup_message_source"] == "linkedin"

    def test_bad_mode_falls_back_to_pre_review(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import _ENGAGEMENT_COLS, update_engagement_preferences
            update_engagement_preferences(1, {"catchup_touch_mode": "yolo"})
        values = cur.execute.call_args[0][1]
        assert values[1 + _ENGAGEMENT_COLS.index("catchup_touch_mode")] == "pre_review"

    def test_unknown_event_types_are_dropped_before_the_enum_column(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import _ENGAGEMENT_COLS, update_engagement_preferences
            update_engagement_preferences(1, {"catchup_event_types": ["job_change", "nonsense"]})
        values = cur.execute.call_args[0][1]
        assert values[1 + _ENGAGEMENT_COLS.index("catchup_event_types")] == '["job_change"]'


class TestPremiumCatchupAllowance:
    """10 catch-up touches/day is a premium-plan feature; everyone else tops out at 5 (PR #509)."""

    @pytest.mark.parametrize("info,premium", [
        ({"subscription_tier": "professional", "subscription_status": "active"}, True),
        ({"subscription_tier": "enterprise", "subscription_status": "trial"}, True),
        ({"subscription_tier": "starter", "subscription_status": "active"}, False),
        ({"subscription_tier": "professional", "subscription_status": "cancelled"}, False),
        ({"subscription_tier": None, "subscription_status": None}, False),
        (None, False),
    ])
    def test_premium_detection(self, info, premium):
        with patch(f"{_DB}.get_user_subscription_info", return_value=info):
            from cqc_lem.utilities.db import is_premium_subscriber, max_catchup_touches_allowed
            assert is_premium_subscriber(1) is premium
            assert max_catchup_touches_allowed(1) == (10 if premium else 5)

    def test_lookup_failure_is_never_premium(self):
        with patch(f"{_DB}.get_user_subscription_info", side_effect=RuntimeError("db down")):
            from cqc_lem.utilities.db import is_premium_subscriber, max_catchup_touches_allowed
            assert is_premium_subscriber(1) is False
            assert max_catchup_touches_allowed(1) == 5


class TestCatchupDmTemplates:
    @pytest.mark.parametrize("event_type", ["job_change", "promotion", "work_anniversary",
                                            "birthday", "education", "in_the_news"])
    def test_every_milestone_type_has_a_default_template(self, event_type, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="no row")
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_dm_template
            tmpl = get_dm_template(1, event_type)
        assert tmpl and tmpl["template_text"]
