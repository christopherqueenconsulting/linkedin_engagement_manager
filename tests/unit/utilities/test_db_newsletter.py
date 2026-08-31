"""Unit tests for newsletter_settings DB helpers."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestGetNewsletterSettings:
    def test_defaults_when_no_row(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["enabled"] is False and s["cadence"] == "weekly" and s["align_with_blog"] is True

    def test_coerces_bools(self, fake_cursor):
        row = {"enabled": 1, "title": "T", "topic": None, "cadence": "monthly",
               "align_with_blog": 0, "newsletter_url": None, "last_published_at": None}
        conn, _ = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["enabled"] is True and s["align_with_blog"] is False and s["cadence"] == "monthly"


class TestUpdateNewsletterSettings:
    def test_upserts(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_settings
            assert update_newsletter_settings(1, {"enabled": True, "title": "Weekly Wins", "cadence": "weekly"}) is True
        assert "ON DUPLICATE KEY UPDATE" in cur.execute.call_args[0][0]

    def test_upsert_includes_draft_config_columns(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_settings
            update_newsletter_settings(1, {"generate_lead_days": 14, "max_queued_drafts": 5})
        sql = cur.execute.call_args[0][0]
        assert "generate_lead_days" in sql and "max_queued_drafts" in sql

    def test_the_save_never_writes_a_column_the_publish_run_owns(self):
        """A written column the request cannot set is a column every save blanks (issue #1538).

        The settings PUT writes the WHOLE row from a request that carries neither `newsletter_url`
        nor `last_published_at`, and `newsletter_url` is what the CTA loop (#624), the
        subscriber-count scrape (#400) and post alignment (#967) all read.
        """
        from cqc_lem.platform.db.repositories.newsletter import (
            _NEWSLETTER_COLS,
            _NEWSLETTER_DEFAULTS,
        )

        assert "newsletter_url" not in _NEWSLETTER_COLS
        assert "last_published_at" not in _NEWSLETTER_COLS
        # The READ shape stays wider than the written one — both are still on the wire.
        assert set(_NEWSLETTER_COLS) < set(_NEWSLETTER_DEFAULTS)

    def test_saving_settings_leaves_the_stored_newsletter_url_alone(self, fake_cursor):
        """The regression itself: neither the INSERT columns nor the UPDATE clause may name it."""
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_settings
            update_newsletter_settings(1, {"enabled": True, "title": "Weekly Wins"})
        sql, values = cur.execute.call_args[0]
        assert "newsletter_url" not in sql
        # …while the columns the request DOES carry are still written, so this is not a dead save.
        from cqc_lem.platform.db.repositories.newsletter import _NEWSLETTER_COLS
        assert values[1 + _NEWSLETTER_COLS.index("title")] == "Weekly Wins"

    def test_the_written_columns_are_exactly_what_the_request_model_can_set(self):
        """Anti-drift, so the same bug cannot arrive one save later.

        A column added to the written list that `NewsletterSettingsRequest` cannot carry blanks
        itself on the next save.
        """
        from cqc_lem.api.routers.user import NewsletterSettingsRequest
        from cqc_lem.platform.db.repositories.newsletter import _NEWSLETTER_COLS

        settable = set(NewsletterSettingsRequest.model_fields) - {"session_token"}
        assert set(_NEWSLETTER_COLS) == settable


class TestNewsletterDue:
    def test_returns_due_user_ids(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[(1,), (5,)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            import datetime

            from cqc_lem.utilities.db import get_newsletter_due_user_ids
            assert get_newsletter_due_user_ids(datetime.datetime(2026, 7, 4)) == [1, 5]
        assert "enabled=1" in cur.execute.call_args[0][0]


class TestNewsletterSchedulingFields:
    def test_settings_include_publish_day_hour(self, fake_cursor):
        row = {"enabled": 1, "title": "T", "topic": None, "cadence": "weekly",
               "align_with_blog": 1, "newsletter_url": None, "last_published_at": None,
               "publish_day": "3", "publish_hour": "14"}
        conn, _ = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["publish_day"] == 3 and s["publish_hour"] == 14

    def test_defaults_publish_day_hour(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["publish_day"] == 1 and s["publish_hour"] == 9


class TestNewsletterDraftConfigFields:
    def test_settings_coerce_draft_config(self, fake_cursor):
        row = {"enabled": 1, "title": "T", "topic": None, "cadence": "weekly",
               "align_with_blog": 1, "newsletter_url": None, "last_published_at": None,
               "publish_day": "1", "publish_hour": "9",
               "generate_lead_days": "14", "max_queued_drafts": "5"}
        conn, _ = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["generate_lead_days"] == 14 and s["max_queued_drafts"] == 5

    def test_defaults_draft_config(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["generate_lead_days"] == 3 and s["max_queued_drafts"] == 1

    def test_selects_draft_config_columns(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            get_newsletter_settings(1)
        sql = cur.execute.call_args[0][0]
        assert "generate_lead_days" in sql and "max_queued_drafts" in sql


class TestNewsletterInviteFields:
    def test_settings_coerce_invite_fields(self, fake_cursor):
        row = {"enabled": 1, "title": "T", "topic": None, "cadence": "weekly",
               "align_with_blog": 1, "newsletter_url": None, "last_published_at": None,
               "publish_day": "1", "publish_hour": "9", "generate_lead_days": "3",
               "max_queued_drafts": "1", "invite_connections_enabled": 1, "max_invites_per_run": "80"}
        conn, _ = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["invite_connections_enabled"] is True and s["max_invites_per_run"] == 80

    def test_defaults_invite_fields(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            s = get_newsletter_settings(1)
        assert s["invite_connections_enabled"] is False and s["max_invites_per_run"] == 50

    def test_selects_invite_columns(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            get_newsletter_settings(1)
        sql = cur.execute.call_args[0][0]
        assert "invite_connections_enabled" in sql and "max_invites_per_run" in sql

    def test_upsert_includes_invite_columns(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_settings
            update_newsletter_settings(1, {"invite_connections_enabled": True, "max_invites_per_run": 30})
        sql, params = cur.execute.call_args[0]
        assert "invite_connections_enabled" in sql and "max_invites_per_run" in sql
        assert 1 in params and 30 in params  # bool coerced to 1


class TestSubscriberStats:
    def test_record_stat_inserts(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_newsletter_subscriber_stat
            assert record_newsletter_subscriber_stat(1, subscriber_count=123, invites_sent=5) is True
        sql, params = cur.execute.call_args[0]
        assert "INSERT INTO newsletter_subscriber_stats" in sql
        assert params == (1, 123, 5)

    def test_record_stat_defaults(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_newsletter_subscriber_stat
            assert record_newsletter_subscriber_stat(1) is True
        _, params = cur.execute.call_args[0]
        assert params == (1, None, 0)

    def test_record_stat_false_on_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_newsletter_subscriber_stat
            assert record_newsletter_subscriber_stat(1, 5) is False

    def test_get_stats_orders_desc(self, fake_cursor):
        import datetime
        rows = [{"subscriber_count": 130, "invites_sent": 0, "captured_at": datetime.datetime(2026, 7, 20)},
                {"subscriber_count": 120, "invites_sent": 5, "captured_at": datetime.datetime(2026, 7, 13)}]
        conn, cur = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_subscriber_stats
            out = get_newsletter_subscriber_stats(1, limit=10)
        assert [r["subscriber_count"] for r in out] == [130, 120]
        sql, params = cur.execute.call_args[0]
        assert "ORDER BY captured_at DESC" in sql and params == (1, 10)

    def test_get_stats_empty_on_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_subscriber_stats
            assert get_newsletter_subscriber_stats(1) == []

    def test_latest_count_returns_int(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(142,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_latest_newsletter_subscriber_count
            assert get_latest_newsletter_subscriber_count(1) == 142
        sql = cur.execute.call_args[0][0]
        assert "subscriber_count IS NOT NULL" in sql

    def test_latest_count_none_when_absent(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_latest_newsletter_subscriber_count
            assert get_latest_newsletter_subscriber_count(1) is None


class TestEnabledNewsletterUsers:
    def test_returns_ids(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[(2,), (9,)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_enabled_newsletter_user_ids
            assert get_enabled_newsletter_user_ids() == [2, 9]
        assert "enabled=1" in cur.execute.call_args[0][0]


class TestEditions:
    def test_create_returns_lastrowid(self, fake_cursor):
        conn, cur = fake_cursor()
        cur.lastrowid = 77
        import datetime
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_newsletter_edition
            assert create_newsletter_edition(1, "T", "S", "B", datetime.datetime(2026, 7, 7, 13)) == 77
        assert "INSERT INTO newsletter_editions" in cur.execute.call_args[0][0]

    def test_get_pending(self, fake_cursor):
        row = {"id": 4, "title": "T", "subtitle": "S", "body": "B", "status": "draft",
               "scheduled_for": None}
        conn, cur = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_pending_newsletter_edition
            assert get_pending_newsletter_edition(1)["id"] == 4
        sql = cur.execute.call_args[0][0]
        assert "draft" in sql and "approved" in sql

    def test_create_returns_zero_on_duplicate_slot(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.IntegrityError("dup uq_user_slot")
        import datetime
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_newsletter_edition
            assert create_newsletter_edition(1, "T", "S", "B", datetime.datetime(2026, 7, 7, 13)) == 0

    def test_count_pending(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(3,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_pending_newsletter_editions
            assert count_pending_newsletter_editions(1) == 3
        sql = cur.execute.call_args[0][0]
        assert "COUNT(*)" in sql and "draft" in sql and "approved" in sql

    def test_get_pending_editions_plural_ordered(self, fake_cursor):
        rows = [{"id": 1, "title": "A", "subtitle": None, "body": "B", "status": "draft", "scheduled_for": None},
                {"id": 2, "title": "C", "subtitle": None, "body": "D", "status": "approved", "scheduled_for": None}]
        conn, cur = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_pending_newsletter_editions
            out = get_pending_newsletter_editions(1)
        assert [e["id"] for e in out] == [1, 2]
        assert "ORDER BY scheduled_for ASC" in cur.execute.call_args[0][0]

    def test_get_latest_scheduled_for(self, fake_cursor):
        import datetime
        dt = datetime.datetime(2026, 7, 21, 13)
        conn, cur = fake_cursor(fetch_one=(dt,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_latest_edition_scheduled_for
            assert get_latest_edition_scheduled_for(1) == dt
        assert "MAX(scheduled_for)" in cur.execute.call_args[0][0]

    def test_update_only_provided_fields(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_edition
            assert update_newsletter_edition(4, 1, status="approved") is True
        sql, params = cur.execute.call_args[0]
        assert "COALESCE" in sql
        # (title, subtitle, subject, format, hook_style, opening_line, blueprint, body, status, scheduled_for, id, user_id)
        assert params == (None, None, None, None, None, None, None, None, "approved", None, 4, 1)

    def test_update_persists_subject(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_edition
            assert update_newsletter_edition(4, 1, subject="New Subject", status="draft") is True
        sql, params = cur.execute.call_args[0]
        assert "subject = COALESCE" in sql
        assert params == (None, None, "New Subject", None, None, None, None, None, "draft", None, 4, 1)

    def test_update_persists_shape_fields(self, fake_cursor):
        import json
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_edition
            assert update_newsletter_edition(
                4, 1, edition_format="case_study", hook_style="micro_story",
                opening_line="It was a Tuesday.", blueprint={"format": "case_study"}) is True
        sql, params = cur.execute.call_args[0]
        assert "`format` = COALESCE" in sql and "hook_style = COALESCE" in sql
        assert "opening_line = COALESCE" in sql and "blueprint = COALESCE" in sql
        assert "case_study" in params and "micro_story" in params and "It was a Tuesday." in params
        assert json.dumps({"format": "case_study"}) in params

    def test_update_persists_scheduled_for(self, fake_cursor):
        from datetime import datetime, timezone
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_newsletter_edition
            # tz-aware input is normalized to naive UTC before storage.
            assert update_newsletter_edition(
                4, 1, status="approved",
                scheduled_for=datetime(2026, 7, 10, 19, 0, 0, tzinfo=timezone.utc)) is True
        sql, params = cur.execute.call_args[0]
        assert "scheduled_for = COALESCE" in sql
        assert datetime(2026, 7, 10, 19, 0, 0) in params

    def test_create_persists_subject(self, fake_cursor):
        conn, cur = fake_cursor()
        cur.lastrowid = 88
        import datetime
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_newsletter_edition
            assert create_newsletter_edition(1, "T", "S", "B", datetime.datetime(2026, 7, 7, 13),
                                             subject="Coherent Subject") == 88
        sql, params = cur.execute.call_args[0]
        assert "subject" in sql
        assert "Coherent Subject" in params

    def test_create_persists_shape_fields(self, fake_cursor):
        import json
        conn, cur = fake_cursor()
        cur.lastrowid = 89
        import datetime
        bp = {"subject": "S", "format": "contrarian", "hook_style": "bold_claim", "cta_style": "debate"}
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
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

    def test_create_shape_fields_default_null(self, fake_cursor):
        conn, cur = fake_cursor()
        cur.lastrowid = 90
        import datetime
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import create_newsletter_edition
            assert create_newsletter_edition(1, "T", "S", "B", datetime.datetime(2026, 7, 7, 13)) == 90
        _, params = cur.execute.call_args[0]
        # blueprint None → stored NULL, not the string 'null'
        assert "null" not in [p for p in params if isinstance(p, str)]

    def test_blueprint_history(self, fake_cursor):
        rows = [{"subject": "S1", "format": "case_study", "hook_style": "micro_story",
                 "opening_line": "It was a Tuesday."},
                {"subject": "S2", "format": "listicle", "hook_style": "question",
                 "opening_line": "What would you do?"}]
        conn, cur = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_newsletter_blueprint_history
            out = get_recent_newsletter_blueprint_history(1, limit=5)
        assert [h["format"] for h in out] == ["case_study", "listicle"]
        sql, params = cur.execute.call_args[0]
        assert "`format`" in sql and "hook_style" in sql and "opening_line" in sql
        assert "published" in sql and "skipped" in sql and "draft" in sql
        assert "ORDER BY id DESC" in sql
        assert params == (1, 5)

    def test_blueprint_history_empty_on_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_newsletter_blueprint_history
            assert get_recent_newsletter_blueprint_history(1) == []

    def test_recent_subjects_dedup_history(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[("Subject A",), ("Subject B",)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_newsletter_subjects
            assert get_recent_newsletter_subjects(1, limit=5) == ["Subject A", "Subject B"]
        sql, params = cur.execute.call_args[0]
        # Pulls published + queued + skipped history and excludes blanks.
        assert "published" in sql and "skipped" in sql and "draft" in sql
        assert "subject IS NOT NULL" in sql
        assert params == (1, 5)

    def test_recent_bodies_are_the_similarity_history(self, fake_cursor):
        # #1284: the nightly quality pass had no body reader for this surface, so every edition's
        # self-similarity was recorded as unmeasured.
        conn, cur = fake_cursor(fetch_all=[("Edition two body",), ("Edition one body",)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_newsletter_bodies
            assert get_recent_newsletter_bodies(1, limit=5) == ["Edition two body",
                                                               "Edition one body"]
        sql, params = cur.execute.call_args[0]
        assert "body IS NOT NULL" in sql and "body <> ''" in sql
        # Queued drafts count as history too — a queued edition is already written.
        assert "published" in sql and "draft" in sql and "approved" in sql
        assert "ORDER BY id DESC" in sql
        assert params == (1, 5)

    def test_recent_titles_read_the_same_editions_as_the_bodies(self, fake_cursor):
        # #1433: the sampler measures body and title similarity over ONE corpus, so the two readers
        # must share the status filter and the ordering — otherwise the two distributions describe
        # different sets of editions.
        conn, cur = fake_cursor(fetch_all=[("Edition two title",), ("Edition one title",)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_newsletter_titles
            assert get_recent_newsletter_titles(1, limit=5) == ["Edition two title",
                                                               "Edition one title"]
        sql, params = cur.execute.call_args[0]
        assert "title IS NOT NULL" in sql and "title <> ''" in sql
        assert "published" in sql and "draft" in sql and "approved" in sql
        assert "ORDER BY id DESC" in sql
        assert params == (1, 5)

    def test_recent_titles_empty_on_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_newsletter_titles
            assert get_recent_newsletter_titles(1) == []

    def test_recent_bodies_empty_on_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_newsletter_bodies
            assert get_recent_newsletter_bodies(1) == []

    def test_editions_due(self, fake_cursor):
        rows = [{"id": 3, "user_id": 1, "title": "T", "subtitle": "S", "body": "B"}]
        conn, cur = fake_cursor(fetch_all=rows)
        import datetime
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_editions_due_to_publish
            due = get_editions_due_to_publish(datetime.datetime(2026, 7, 7, 13))
        assert due[0]["id"] == 3
        assert "scheduled_for <= %s" in cur.execute.call_args[0][0]

    def test_editions_due_gates_drafts_on_the_opt_in(self, fake_cursor):
        """Issue #1135 — 'approved' always publishes; 'draft' only for an opted-in user."""
        conn, cur = fake_cursor(fetch_all=[])
        import datetime
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_editions_due_to_publish
            get_editions_due_to_publish(datetime.datetime(2026, 7, 7, 13))
        sql = " ".join(cur.execute.call_args[0][0].split())
        assert "e.status = 'approved'" in sql
        assert "e.status = 'draft' AND COALESCE(s.auto_publish_newsletters, 0) = 1" in sql
        # LEFT, so a user with no settings row is read as opted OUT rather than losing their
        # APPROVED editions to an inner join.
        assert "LEFT JOIN newsletter_settings s ON s.user_id = e.user_id" in sql

    def test_pending_cover_sweep_selects_the_edition_status(self, fake_cursor):
        """Issue #1135 — the reminder's wording turns on whether the BODY reaches that slot."""
        conn, cur = fake_cursor(fetch_all=[])
        import datetime
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_editions_with_pending_cover
            get_editions_with_pending_cover(datetime.datetime(2026, 7, 7),
                                            datetime.datetime(2026, 7, 9))
        sql = " ".join(cur.execute.call_args[0][0].split())
        assert "SELECT id, user_id, title, status, scheduled_for" in sql

    def test_get_edition(self, fake_cursor):
        row = {"id": 3, "user_id": 1, "title": "T", "subtitle": "S", "body": "B",
               "status": "draft", "scheduled_for": None, "published_url": None}
        conn, _ = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_edition
            assert get_newsletter_edition(3)["user_id"] == 1

    def test_mark_published_rolls_cadence(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_edition_published
            assert mark_edition_published(3, "https://x/pulse/y") is True
        sqls = " ".join(c.args[0] for c in cur.execute.call_args_list)
        assert "status='published'" in sqls and "last_published_at=NOW()" in sqls

    def test_mark_failed(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_edition_failed
            assert mark_edition_failed(3) is True
        assert "status='failed'" in cur.execute.call_args[0][0]

    def test_latest_published_edition_url(self, fake_cursor):
        """Issue #1770: the sweep's `newsletter_edition` target resolver reads this."""
        conn, cur = fake_cursor(fetch_one={"published_url": "https://x/pulse/y"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_latest_published_newsletter_edition_url
            assert get_latest_published_newsletter_edition_url(1) == "https://x/pulse/y"
        sql = " ".join(cur.execute.call_args[0][0].split())
        assert "status='published'" in sql and "ORDER BY published_at DESC" in sql

    def test_latest_published_edition_url_is_none_with_nothing_published(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_latest_published_newsletter_edition_url
            assert get_latest_published_newsletter_edition_url(1) is None


class TestNewsletterCoverSettings:
    """Issue #893: the cover opt-in rides the same settings row."""

    def test_default_is_off(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            assert get_newsletter_settings(1)["cover_image_auto"] is False

    def test_coerces_the_stored_flag_to_a_bool(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"enabled": 1, "align_with_blog": 1,
                                        "invite_connections_enabled": 0, "cover_image_auto": 1})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            assert get_newsletter_settings(1)["cover_image_auto"] is True

    def test_upsert_writes_the_flag_as_an_int(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import _NEWSLETTER_COLS, update_newsletter_settings
            update_newsletter_settings(1, {"cover_image_auto": True})
        sql, values = cur.execute.call_args[0]
        assert "cover_image_auto" in sql
        assert values[1 + _NEWSLETTER_COLS.index("cover_image_auto")] == 1


class TestAutoPublishSetting:
    """Issue #1135: the autonomous-publish opt-in rides the same settings row."""

    def test_a_user_with_no_settings_row_requires_approval(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            assert get_newsletter_settings(1)["auto_publish_newsletters"] is False

    def test_coerces_the_stored_flag_to_a_bool(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"enabled": 1, "align_with_blog": 1,
                                         "invite_connections_enabled": 0, "cover_image_auto": 0,
                                         "auto_publish_newsletters": 1})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            assert get_newsletter_settings(1)["auto_publish_newsletters"] is True

    def test_the_read_selects_the_column(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_settings
            get_newsletter_settings(1)
        assert "auto_publish_newsletters" in cur.execute.call_args[0][0]

    def test_upsert_writes_the_flag_as_an_int(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import _NEWSLETTER_COLS, update_newsletter_settings
            update_newsletter_settings(1, {"auto_publish_newsletters": True})
        sql, values = cur.execute.call_args[0]
        assert "auto_publish_newsletters" in sql
        assert values[1 + _NEWSLETTER_COLS.index("auto_publish_newsletters")] == 1


class TestEditionCoverImage:
    def test_set_writes_path_source_and_status_together(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_edition_cover_image
            assert set_edition_cover_image(9, 3, "images/newsletter_covers/3/a.png",
                                           "ai", "pending_review") is True
        sql, values = cur.execute.call_args[0]
        assert "cover_image_path" in sql and "cover_image_source" in sql and "cover_image_status" in sql
        assert "user_id=%s" in sql, "a cover write must be scoped to its owner"
        assert values == ("images/newsletter_covers/3/a.png", "ai", "pending_review", 9, 3)

    def test_set_status_only_touches_an_edition_that_has_a_cover(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_edition_cover_status
            assert set_edition_cover_status(9, 3, "approved") is True
        sql, values = cur.execute.call_args[0]
        assert "cover_image_path IS NOT NULL" in sql
        assert values == ("approved", 9, 3)

    def test_clear_nulls_every_cover_column(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import clear_edition_cover_image
            assert clear_edition_cover_image(9, 3) is True
        sql, values = cur.execute.call_args[0]
        assert sql.count("NULL") == 3
        assert values == (9, 3)

    def test_db_error_is_false_not_an_exception(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import (
                clear_edition_cover_image,
                set_edition_cover_image,
                set_edition_cover_status,
            )
            assert set_edition_cover_image(9, 3, "p", "ai", "pending_review") is False
            assert set_edition_cover_status(9, 3, "approved") is False
            assert clear_edition_cover_image(9, 3) is False

    def test_reads_select_the_cover_columns(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"id": 9}, fetch_all=[{"id": 9}])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_edition, get_pending_newsletter_editions
            get_newsletter_edition(9)
            single_sql = cur.execute.call_args[0][0]
            get_pending_newsletter_editions(3)
            queue_sql = cur.execute.call_args[0][0]
        for sql in (single_sql, queue_sql):
            assert "cover_image_path" in sql and "cover_image_status" in sql
