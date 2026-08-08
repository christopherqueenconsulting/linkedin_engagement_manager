"""Error-fallback contract tests for db.py.

Every DB helper catches mysql.connector.Error and returns a documented, safe fallback
(empty list, None, False, defaults, or an explicit fail-safe like True for
has_scheduled_post_today). These tests pin that contract for the helpers whose error
branches were previously untested, plus the success paths of the small post-field getters.
"""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


def _conn(fetch_one=None, fetch_all=None, rowcount=1, lastrowid=1):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetch_one
    cur.fetchall.return_value = fetch_all if fetch_all is not None else []
    cur.rowcount = rowcount
    cur.lastrowid = lastrowid
    conn.cursor.return_value = cur
    return conn, cur


def _err_conn():
    conn, cur = _conn()
    cur.execute.side_effect = mysql.connector.Error(msg="db down")
    return conn


# (function name, args, expected fallback on mysql.connector.Error)
_ERROR_CASES = [
    ("has_linkedin_session", (1,), False),
    ("get_linkedin_session_email_sent_at", (1,), None),
    ("set_linkedin_session_email_sent_at", (1,), False),
    ("get_user_id", ("a@x.com",), None),
    ("get_post_content", (1,), None),
    ("get_post_user_id", (1,), None),
    ("get_post_video_url", (1,), None),
    ("get_post_buyer_stage", (1,), None),
    ("get_post_type", (1,), None),
    ("get_carousel_slides", (1,), []),
    ("get_post_type_counts", (1,), {}),  # empty dict so callers can safely .values()
    ("get_last_planned_post_date_for_user", (1,), None),
    ("get_user_blog_url", (1,), None),
    ("get_user_sitemap_url", (1,), None),
    ("get_user_location", (1,), None),
    ("insert_new_log", None, False),  # args built in the test body (enums)
    ("has_user_commented_on_post_url", (1, "u"), False),
    ("get_post_url_from_log_for_user", (1, 2), None),
    ("get_post_message_from_log_for_user", (1, 2), None),
    ("has_engaged_url_with_x_days", (1, "u", 7), False),
    ("get_dm_history_for_profile", (1, "u"), []),
    ("get_post_status", (1,), None),
    ("get_company_linked_in_url_for_user", (1,), None),
    ("update_company_linked_in_url_for_user", (1, "u"), False),
    ("get_recent_logs", (1,), []),
    ("update_user_linkedin_password", (1, "pw"), False),
    ("update_user_settings", (1, "b", "s"), False),
    ("get_user_email", (1,), None),
    ("get_user_token_info", (1,), None),
    ("update_user_access_token", (1, "at", 3600), False),
    ("update_user_linkedin_token", (1, "sub", "at", 3600), False),
    ("update_linkedin_connection_status", (1, "expired"), False),
    ("get_user_subscription_info", (1,), None),
    ("update_subscription_from_stripe", ("cus_1", "active", "pro", "sub_1"), False),
    ("get_users_with_stripe_subscriptions", (), []),
    ("update_engagement_preferences", (1, {"tone": "warm"}), False),
    ("_count_actions_today", None, 0),  # args built in the test body (enums)
    ("insert_scheduled_dm", (1, "u", "msg", datetime(2026, 7, 10)), None),
    ("get_scheduled_dm", (1,), None),
    ("get_due_scheduled_dms", (), []),
    ("update_scheduled_dm", None, False),  # kwargs built in the test body
    ("has_scheduled_post_today", (1,), True),  # fail-safe: skip standalone run
    ("upsert_engager", (1, "Jane Doe"), False),
    ("get_recent_engagers", (1,), set()),
    ("claim_post_for_comment", (1, "key"), False),
    ("mark_post_commented", (1, "key"), False),
    ("mark_post_reacted", (1, "key"), False),
    ("release_post_claim", (1, "key"), False),
    ("has_commented_post", (1, "key"), False),
    ("get_duplicate_comment_posts", (1,), []),
    ("upsert_user_group", (1, "g1"), False),
    ("get_enabled_group_ids", (1,), []),
    ("set_groups_enabled", (1, {"g1": True}), False),
    ("get_next_group_for_post", (1,), None),
    ("record_group_post", (1, "g1"), False),
    ("record_group_post_run", (1, "g1"), False),
    ("record_post_stats", (1, 2, 3, 4), False),
    ("get_recent_posted_post_ids", (1,), []),
    ("get_post_engagement_rows", (1,), []),
    ("record_shipped_variant", (1, 2, "flux|gen4|1:1"), False),
    ("get_variant_outcome_rows", (1,), []),
    ("has_received_lead_magnet", (1, "u"), True),  # fail-safe: never double-DM
    ("record_lead_magnet_sent", (1, "u"), False),
    ("update_newsletter_settings", (1, {"enabled": True}), False),
    ("mark_newsletter_published", (1,), False),
    ("get_newsletter_due_user_ids", (datetime(2026, 7, 6),), []),
    ("get_enabled_newsletter_user_ids", (), []),
    ("create_newsletter_edition", (1, "t", "s", "b", datetime(2026, 7, 10)), 0),
    ("get_pending_newsletter_edition", (1,), None),
    ("count_pending_newsletter_editions", (1,), 0),
    ("get_pending_newsletter_editions", (1,), []),
    ("get_latest_edition_scheduled_for", (1,), None),
    ("update_newsletter_edition", (1, 1), False),
    ("get_recent_newsletter_subjects", (1,), []),
    ("get_recent_newsletter_blueprint_history", (1,), []),
    ("get_editions_due_to_publish", (datetime(2026, 7, 6),), []),
    ("get_newsletter_edition", (1,), None),
    ("mark_edition_published", (1, "url"), False),
    ("mark_edition_failed", (1,), False),
    ("get_dm_templates", (1,), []),
    ("upsert_dm_templates", (1, [{"event_type": "manual"}]), False),
    ("enqueue_followup", (1, "u", "Jane", "manual", 1, datetime(2026, 7, 10)), False),
    ("get_due_followups", (datetime(2026, 7, 6),), []),
    ("mark_followup", (1, "sent"), False),
    ("stop_followups_for_profile", (1, "u"), 0),
    ("get_user_proxy", (1,), None),
    ("update_user_proxy", (1, "http://p"), False),
    ("get_user_timezone", (1,), "America/New_York"),
    ("update_user_timezone", (1, "UTC"), False),
    ("get_user_by_stripe_customer_id", ("cus_1",), None),
    ("get_avatar_credit_balance", (1,), 0),
    ("add_avatar_credits", (1, 5, "purchase"), False),
    ("deduct_avatar_credit", (1, "t1"), False),
    ("get_video_credit_balance", (1,), 0),
    ("get_video_credit_ledger_entry_by_session", ("cs_1",), None),
    ("add_video_credits", (1, 5, "purchase"), False),
    ("deduct_video_credits", (1, 2), False),
    ("refund_video_credits", (1, 2), False),
    ("get_post_video_quality", (1,), "standard"),
    ("update_post_video_quality", (1, "premium"), False),
    ("get_post_carousel_slides", (1,), None),
    ("get_unposted_posts_missing_assets", (), []),
    ("insert_avatar_training", (1, "t1", "TOK"), None),
    ("get_active_avatar", (1,), None),
]


class TestMysqlErrorFallbacks:
    @pytest.mark.parametrize("fname,args,expected",
                             _ERROR_CASES, ids=[c[0] for c in _ERROR_CASES])
    def test_error_returns_documented_fallback(self, fname, args, expected):
        import cqc_lem.utilities.db as db
        if args is None:
            if fname == "insert_new_log":
                args = (1, db.LogActionType.COMMENT, db.LogResultType.SUCCESS)
            elif fname == "_count_actions_today":
                args = (1, db.LogActionType.DM)
            elif fname == "update_scheduled_dm":
                with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=_err_conn()):
                    assert db.update_scheduled_dm(1, message="x") == expected
                return
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=_err_conn()):
            result = getattr(db, fname)(*args)
        assert result == expected

    def test_get_engagement_preferences_error_returns_defaults(self):
        import cqc_lem.utilities.db as db
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=_err_conn()):
            assert db.get_engagement_preferences(1) == dict(db._ENGAGEMENT_DEFAULTS)

    def test_get_lead_magnet_settings_error_returns_defaults(self):
        import cqc_lem.utilities.db as db
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=_err_conn()):
            assert db.get_lead_magnet_settings(1) == {"enabled": False, "keyword": None,
                                                      "message": None}

    def test_get_newsletter_settings_error_returns_defaults(self):
        import cqc_lem.utilities.db as db
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=_err_conn()):
            assert db.get_newsletter_settings(1) == dict(db._NEWSLETTER_DEFAULTS)

    def test_get_scheduled_dms_error_returns_empty_page(self):
        import cqc_lem.utilities.db as db
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=_err_conn()):
            assert db.get_scheduled_dms(1, page=3, page_size=5) == {
                "dms": [], "total": 0, "page": 3, "page_size": 5}

    def test_get_dm_template_error_falls_back_to_default_for_step0(self):
        import cqc_lem.utilities.db as db
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=_err_conn()):
            tpl = db.get_dm_template(1, "manual", step=0)
        assert tpl == {"template_text": db._DM_DEFAULT_TEMPLATES["manual"],
                       "delay_hours": 0, "step": 0}

    def test_get_dm_template_error_returns_none_for_higher_steps(self):
        import cqc_lem.utilities.db as db
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=_err_conn()):
            assert db.get_dm_template(1, "manual", step=2) is None

    def test_claim_post_duplicate_key_loses_claim(self):
        import cqc_lem.utilities.db as db
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.IntegrityError(msg="dup", errno=1062)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert db.claim_post_for_comment(1, "key") is False

    def test_create_newsletter_edition_duplicate_slot_is_silent_zero(self):
        import cqc_lem.utilities.db as db
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.IntegrityError(msg="dup", errno=1062)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            assert db.create_newsletter_edition(1, "t", "s", "b",
                                                datetime(2026, 7, 10)) == 0


class TestPostFieldGetters:
    """Success paths of the small posts-table field getters (previously untested)."""

    def test_get_post_content(self):
        conn, cur = _conn(fetch_one={"content": "hello world"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_content
            assert get_post_content(9) == "hello world"
        assert cur.execute.call_args[0][1] == (9,)

    def test_get_post_content_missing_row(self):
        conn, _ = _conn(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_content
            assert get_post_content(9) is None

    def test_get_post_user_id(self):
        conn, _ = _conn(fetch_one={"user_id": 5})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_user_id
            assert get_post_user_id(9) == 5

    def test_get_post_video_url(self):
        conn, _ = _conn(fetch_one={"video_url": "https://x/v.mp4"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_video_url
            assert get_post_video_url(9) == "https://x/v.mp4"

    def test_get_post_buyer_stage(self):
        conn, _ = _conn(fetch_one={"buyer_stage": "awareness"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_buyer_stage
            assert get_post_buyer_stage(9) == "awareness"

    def test_get_post_type_returns_enum(self):
        conn, _ = _conn(fetch_one={"post_type": "carousel"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import PostType, get_post_type
            assert get_post_type(9) is PostType.CAROUSEL

    def test_get_post_type_invalid_value_returns_none(self):
        conn, _ = _conn(fetch_one={"post_type": "hologram"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_type
            assert get_post_type(9) is None

    def test_get_carousel_slides_parses_json_string(self):
        slides = ["https://x/1.png", "https://x/2.png"]
        conn, _ = _conn(fetch_one={"carousel_slides": json.dumps(slides)})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_carousel_slides
            assert get_carousel_slides(9) == slides

    def test_get_carousel_slides_bad_json_returns_empty(self):
        conn, _ = _conn(fetch_one={"carousel_slides": "{not json"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_carousel_slides
            assert get_carousel_slides(9) == []

    def test_get_carousel_slides_non_list_returns_empty(self):
        conn, _ = _conn(fetch_one={"carousel_slides": json.dumps({"a": 1})})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_carousel_slides
            assert get_carousel_slides(9) == []
