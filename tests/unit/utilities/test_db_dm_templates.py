"""Unit tests for DM-template DB helpers."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestGetDmTemplate:
    def test_returns_db_row_when_present(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"template_text": "custom {first_name}", "delay_hours": 0, "step": 0})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dm_template
            t = get_dm_template(1, "connection_accepted", 0)
        assert t["template_text"] == "custom {first_name}"

    def test_default_fallback_for_step0(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dm_template
            t = get_dm_template(1, "connection_accepted", 0)
        assert t is not None and "appreciate you connecting" in t["template_text"]

    def test_none_for_higher_step_without_row(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dm_template
            assert get_dm_template(1, "connection_accepted", 1) is None


class TestUpsertDmTemplates:
    def _run(self, fake_cursor, templates):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_dm_templates
            ok = upsert_dm_templates(1, templates)
        return ok, [call[0] for call in cursor.execute.call_args_list]

    def test_upserts(self, fake_cursor):
        ok, calls = self._run(fake_cursor, [
            {"event_type": "manual", "step": 0, "delay_hours": 0, "template_text": "hi", "is_active": True}])
        assert ok is True
        assert "ON DUPLICATE KEY UPDATE" in calls[0][0]

    def test_deletes_the_steps_the_payload_left_out(self, fake_cursor):
        """Issue #1575: a removed follow-up step must stop being sent, so the posted set is the WHOLE set."""
        ok, calls = self._run(fake_cursor, [
            {"event_type": "manual", "step": 0, "delay_hours": 0, "template_text": "hi", "is_active": True},
            {"event_type": "funnel", "step": 1, "delay_hours": 24, "template_text": "again", "is_active": True}])
        assert ok is True
        sql, params = calls[-1]
        assert sql.startswith("DELETE FROM dm_templates WHERE user_id=%s AND (event_type, step) NOT IN")
        assert sql.count("(%s,%s)") == 2
        assert params == (1, "manual", 0, "funnel", 1)

    def test_empty_payload_clears_every_template(self, fake_cursor):
        ok, calls = self._run(fake_cursor, [])
        assert ok is True
        assert calls == [("DELETE FROM dm_templates WHERE user_id=%s", (1,))]

    def test_db_error_leaves_the_set_untouched(self, fake_cursor):
        import mysql.connector
        conn, cursor = fake_cursor(execute_error=mysql.connector.Error("nope"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_dm_templates
            ok = upsert_dm_templates(1, [
                {"event_type": "manual", "step": 0, "delay_hours": 0, "template_text": "hi"}])
        assert ok is False
        conn.commit.assert_not_called()


class TestGetDmTemplates:
    def test_lists_and_coerces_bool(self, fake_cursor):
        conn, _ = fake_cursor(fetch_all=[{"event_type": "manual", "step": 0, "delay_hours": 0,
                                         "template_text": "hi", "is_active": 1}])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dm_templates
            rows = get_dm_templates(1)
        assert rows[0]["is_active"] is True
