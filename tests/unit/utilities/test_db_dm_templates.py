"""Unit tests for DM-template DB helpers."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _mock_conn(fetch_row=None, fetch_all=None):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetch_row
    cursor.fetchall.return_value = fetch_all or []
    cursor.rowcount = 1
    conn.cursor.return_value = cursor
    return conn, cursor


class TestGetDmTemplate:
    def test_returns_db_row_when_present(self):
        conn, _ = _mock_conn(fetch_row={"template_text": "custom {first_name}", "delay_hours": 0, "step": 0})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dm_template
            t = get_dm_template(1, "connection_accepted", 0)
        assert t["template_text"] == "custom {first_name}"

    def test_default_fallback_for_step0(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dm_template
            t = get_dm_template(1, "connection_accepted", 0)
        assert t is not None and "appreciate you connecting" in t["template_text"]

    def test_none_for_higher_step_without_row(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dm_template
            assert get_dm_template(1, "connection_accepted", 1) is None


class TestUpsertDmTemplates:
    def test_upserts(self):
        conn, cursor = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_dm_templates
            ok = upsert_dm_templates(1, [
                {"event_type": "manual", "step": 0, "delay_hours": 0, "template_text": "hi", "is_active": True}])
        assert ok is True
        assert "ON DUPLICATE KEY UPDATE" in cursor.execute.call_args[0][0]


class TestGetDmTemplates:
    def test_lists_and_coerces_bool(self):
        conn, _ = _mock_conn(fetch_all=[{"event_type": "manual", "step": 0, "delay_hours": 0,
                                         "template_text": "hi", "is_active": 1}])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dm_templates
            rows = get_dm_templates(1)
        assert rows[0]["is_active"] is True
