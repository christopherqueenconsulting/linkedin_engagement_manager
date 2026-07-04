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


class TestNewsletterDue:
    def test_returns_due_user_ids(self):
        conn, cur = _mock_conn(fetch_all=[(1,), (5,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_newsletter_due_user_ids
            import datetime
            assert get_newsletter_due_user_ids(datetime.datetime(2026, 7, 4)) == [1, 5]
        assert "enabled=1" in cur.execute.call_args[0][0]
