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
    def test_upserts(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_dm_templates
            ok = upsert_dm_templates(1, [
                {"event_type": "manual", "step": 0, "delay_hours": 0, "template_text": "hi", "is_active": True}])
        assert ok is True
        assert "ON DUPLICATE KEY UPDATE" in cursor.execute.call_args[0][0]


class TestGetDmTemplates:
    def test_lists_and_coerces_bool(self, fake_cursor):
        conn, _ = fake_cursor(fetch_all=[{"event_type": "manual", "step": 0, "delay_hours": 0,
                                         "template_text": "hi", "is_active": 1}])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dm_templates
            rows = get_dm_templates(1)
        assert rows[0]["is_active"] is True
