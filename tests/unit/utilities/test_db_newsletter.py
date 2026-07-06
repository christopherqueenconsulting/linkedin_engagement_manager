"""Unit tests for newsletter_settings DB helpers."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _mock_conn(fetch_row=None, fetch_all=None, rowcount=1):
    conn = MagicMock(); cur = MagicMock()
    cur.fetchone.return_value = fetch_row
    cur.fetchall.return_value = fetch_all or []
    cur.rowcount = rowcount
    conn.cursor.return_value = cur
    return conn, cur


class TestGetNewsletterSettings:
    def test_defaults_when_no_row(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["enabled"] is False and s["cadence"] == "weekly" and s["align_with_blog"] is True

    def test_coerces_bools(self):
        row = {"enabled": 1, "title": "T", "topic": None, "cadence": "monthly",
               "align_with_blog": 0, "newsletter_url": None, "last_published_at": None}
        conn, _ = _mock_conn(fetch_row=row)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["enabled"] is True and s["align_with_blog"] is False and s["cadence"] == "monthly"


class TestUpdateNewsletterSettings:
    def test_upserts(self):
        conn, cur = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_settings
            assert update_newsletter_settings(1, {"enabled": True, "title": "Weekly Wins", "cadence": "weekly"}) is True
        assert "ON DUPLICATE KEY UPDATE" in cur.execute.call_args[0][0]

    def test_upsert_includes_draft_config_columns(self):
        conn, cur = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_settings
            update_newsletter_settings(1, {"generate_lead_days": 14, "max_queued_drafts": 5})
        sql = cur.execute.call_args[0][0]
        assert "generate_lead_days" in sql and "max_queued_drafts" in sql


class TestNewsletterDue:
    def test_returns_due_user_ids(self):
        conn, cur = _mock_conn(fetch_all=[(1,), (5,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_due_user_ids
            import datetime
            assert get_newsletter_due_user_ids(datetime.datetime(2026, 7, 4)) == [1, 5]
        assert "enabled=1" in cur.execute.call_args[0][0]


class TestNewsletterSchedulingFields:
    def test_settings_include_publish_day_hour(self):
        row = {"enabled": 1, "title": "T", "topic": None, "cadence": "weekly",
               "align_with_blog": 1, "newsletter_url": None, "last_published_at": None,
               "publish_day": "3", "publish_hour": "14"}
        conn, _ = _mock_conn(fetch_row=row)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["publish_day"] == 3 and s["publish_hour"] == 14

    def test_defaults_publish_day_hour(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["publish_day"] == 1 and s["publish_hour"] == 9


class TestNewsletterDraftConfigFields:
    def test_settings_coerce_draft_config(self):
        row = {"enabled": 1, "title": "T", "topic": None, "cadence": "weekly",
               "align_with_blog": 1, "newsletter_url": None, "last_published_at": None,
               "publish_day": "1", "publish_hour": "9",
               "generate_lead_days": "14", "max_queued_drafts": "5"}
        conn, _ = _mock_conn(fetch_row=row)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["generate_lead_days"] == 14 and s["max_queued_drafts"] == 5

    def test_defaults_draft_config(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["generate_lead_days"] == 3 and s["max_queued_drafts"] == 1

    def test_selects_draft_config_columns(self):
        conn, cur = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            get_newsletter_settings(1)
        sql = cur.execute.call_args[0][0]
        assert "generate_lead_days" in sql and "max_queued_drafts" in sql


class TestEnabledNewsletterUsers:
    def test_returns_ids(self):
        conn, cur = _mock_conn(fetch_all=[(2,), (9,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_enabled_newsletter_user_ids
            assert get_enabled_newsletter_user_ids() == [2, 9]
        assert "enabled=1" in cur.execute.call_args[0][0]


class TestEditions:
    def test_create_returns_lastrowid(self):
        conn, cur = _mock_conn()
        cur.lastrowid = 77
        import datetime
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_newsletter_edition
            assert create_newsletter_edition(1, "T", "S", "B", datetime.datetime(2026, 7, 7, 13)) == 77
        assert "INSERT INTO newsletter_editions" in cur.execute.call_args[0][0]

    def test_get_pending(self):
        row = {"id": 4, "title": "T", "subtitle": "S", "body": "B", "status": "draft",
               "scheduled_for": None}
        conn, cur = _mock_conn(fetch_row=row)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_pending_newsletter_edition
            assert get_pending_newsletter_edition(1)["id"] == 4
        sql = cur.execute.call_args[0][0]
        assert "draft" in sql and "approved" in sql

    def test_create_returns_zero_on_duplicate_slot(self):
        import mysql.connector
        conn, cur = _mock_conn()
        cur.execute.side_effect = mysql.connector.IntegrityError("dup uq_user_slot")
        import datetime
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_newsletter_edition
            assert create_newsletter_edition(1, "T", "S", "B", datetime.datetime(2026, 7, 7, 13)) == 0

    def test_count_pending(self):
        conn, cur = _mock_conn(fetch_row=(3,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_pending_newsletter_editions
            assert count_pending_newsletter_editions(1) == 3
        sql = cur.execute.call_args[0][0]
        assert "COUNT(*)" in sql and "draft" in sql and "approved" in sql

    def test_get_pending_editions_plural_ordered(self):
        rows = [{"id": 1, "title": "A", "subtitle": None, "body": "B", "status": "draft", "scheduled_for": None},
                {"id": 2, "title": "C", "subtitle": None, "body": "D", "status": "approved", "scheduled_for": None}]
        conn, cur = _mock_conn(fetch_all=rows)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_pending_newsletter_editions
            out = get_pending_newsletter_editions(1)
        assert [e["id"] for e in out] == [1, 2]
        assert "ORDER BY scheduled_for ASC" in cur.execute.call_args[0][0]

    def test_get_latest_scheduled_for(self):
        import datetime
        dt = datetime.datetime(2026, 7, 21, 13)
        conn, cur = _mock_conn(fetch_row=(dt,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_latest_edition_scheduled_for
            assert get_latest_edition_scheduled_for(1) == dt
        assert "MAX(scheduled_for)" in cur.execute.call_args[0][0]

    def test_update_only_provided_fields(self):
        conn, cur = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_edition
            assert update_newsletter_edition(4, 1, status="approved") is True
        sql, params = cur.execute.call_args[0]
        assert "COALESCE" in sql
        # (title, subtitle, subject, format, hook_style, opening_line, blueprint, body, status, id, user_id)
        assert params == (None, None, None, None, None, None, None, None, "approved", 4, 1)

    def test_update_persists_subject(self):
        conn, cur = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_edition
            assert update_newsletter_edition(4, 1, subject="New Subject", status="draft") is True
        sql, params = cur.execute.call_args[0]
        assert "subject = COALESCE" in sql
        assert params == (None, None, "New Subject", None, None, None, None, None, "draft", 4, 1)

    def test_update_persists_shape_fields(self):
        import json
        conn, cur = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_edition
            assert update_newsletter_edition(
                4, 1, edition_format="case_study", hook_style="micro_story",
                opening_line="It was a Tuesday.", blueprint={"format": "case_study"}) is True
        sql, params = cur.execute.call_args[0]
        assert "`format` = COALESCE" in sql and "hook_style = COALESCE" in sql
        assert "opening_line = COALESCE" in sql and "blueprint = COALESCE" in sql
        assert "case_study" in params and "micro_story" in params and "It was a Tuesday." in params
        assert json.dumps({"format": "case_study"}) in params

    def test_create_persists_subject(self):
        conn, cur = _mock_conn()
        cur.lastrowid = 88
        import datetime
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_newsletter_edition
            assert create_newsletter_edition(1, "T", "S", "B", datetime.datetime(2026, 7, 7, 13),
                                             subject="Coherent Subject") == 88
        sql, params = cur.execute.call_args[0]
        assert "subject" in sql
        assert "Coherent Subject" in params

    def test_create_persists_shape_fields(self):
        import json
        conn, cur = _mock_conn()
        cur.lastrowid = 89
        import datetime
        bp = {"subject": "S", "format": "contrarian", "hook_style": "bold_claim", "cta_style": "debate"}
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_newsletter_edition
            assert create_newsletter_edition(
                1, "T", "S", "B", datetime.datetime(2026, 7, 7, 13), subject="S",
                edition_format="contrarian", hook_style="bold_claim",
                opening_line="Everyone is wrong about this.", blueprint=bp) == 89
        sql, params = cur.execute.call_args[0]
        assert "`format`" in sql and "hook_style" in sql and "opening_line" in sql and "blueprint" in sql
        assert "contrarian" in params and "bold_claim" in params
        assert "Everyone is wrong about this." in params
        assert json.dumps(bp) in params

    def test_create_shape_fields_default_null(self):
        conn, cur = _mock_conn()
        cur.lastrowid = 90
        import datetime
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_newsletter_edition
            assert create_newsletter_edition(1, "T", "S", "B", datetime.datetime(2026, 7, 7, 13)) == 90
        _, params = cur.execute.call_args[0]
        # blueprint None → stored NULL, not the string 'null'
        assert "null" not in [p for p in params if isinstance(p, str)]

    def test_blueprint_history(self):
        rows = [{"subject": "S1", "format": "case_study", "hook_style": "micro_story",
                 "opening_line": "It was a Tuesday."},
                {"subject": "S2", "format": "listicle", "hook_style": "question",
                 "opening_line": "What would you do?"}]
        conn, cur = _mock_conn(fetch_all=rows)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_newsletter_blueprint_history
            out = get_recent_newsletter_blueprint_history(1, limit=5)
        assert [h["format"] for h in out] == ["case_study", "listicle"]
        sql, params = cur.execute.call_args[0]
        assert "`format`" in sql and "hook_style" in sql and "opening_line" in sql
        assert "published" in sql and "skipped" in sql and "draft" in sql
        assert "ORDER BY id DESC" in sql
        assert params == (1, 5)

    def test_blueprint_history_empty_on_error(self):
        import mysql.connector
        conn, cur = _mock_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_newsletter_blueprint_history
            assert get_recent_newsletter_blueprint_history(1) == []

    def test_recent_subjects_dedup_history(self):
        conn, cur = _mock_conn(fetch_all=[("Subject A",), ("Subject B",)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_newsletter_subjects
            assert get_recent_newsletter_subjects(1, limit=5) == ["Subject A", "Subject B"]
        sql, params = cur.execute.call_args[0]
        # Pulls published + queued + skipped history and excludes blanks.
        assert "published" in sql and "skipped" in sql and "draft" in sql
        assert "subject IS NOT NULL" in sql
        assert params == (1, 5)

    def test_editions_due(self):
        rows = [{"id": 3, "user_id": 1, "title": "T", "subtitle": "S", "body": "B"}]
        conn, cur = _mock_conn(fetch_all=rows)
        import datetime
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_editions_due_to_publish
            due = get_editions_due_to_publish(datetime.datetime(2026, 7, 7, 13))
        assert due[0]["id"] == 3
        assert "scheduled_for <= %s" in cur.execute.call_args[0][0]

    def test_get_edition(self):
        row = {"id": 3, "user_id": 1, "title": "T", "subtitle": "S", "body": "B",
               "status": "draft", "scheduled_for": None, "published_url": None}
        conn, _ = _mock_conn(fetch_row=row)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_edition
            assert get_newsletter_edition(3)["user_id"] == 1

    def test_mark_published_rolls_cadence(self):
        conn, cur = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_edition_published
            assert mark_edition_published(3, "https://x/pulse/y") is True
        sqls = " ".join(c.args[0] for c in cur.execute.call_args_list)
        assert "status='published'" in sqls and "last_published_at=NOW()" in sqls

    def test_mark_failed(self):
        conn, cur = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_edition_failed
            assert mark_edition_failed(3) is True
        assert "status='failed'" in cur.execute.call_args[0][0]
