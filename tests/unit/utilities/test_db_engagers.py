"""Unit tests for reciprocity engager tracking."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _mock_conn(fetch_all=None):
    conn = MagicMock(); cur = MagicMock()
    cur.fetchall.return_value = fetch_all or []
    conn.cursor.return_value = cur
    return conn, cur


class TestUpsertEngager:
    def test_upserts(self):
        conn, cur = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_engager
            assert upsert_engager(1, "Jane Doe", "https://x/in/jane") is True
        sql = cur.execute.call_args[0][0]
        assert "INSERT INTO post_engagers" in sql and "ON DUPLICATE KEY UPDATE" in sql

    def test_blank_name_noop(self):
        with patch(f"{_DB}.get_db_connection") as gc:
            from cqc_lem.utilities.db import upsert_engager
            assert upsert_engager(1, "   ") is False
        gc.assert_not_called()


class TestGetRecentEngagers:
    def test_returns_lowercased_names(self):
        conn, _ = _mock_conn(fetch_all=[("jane doe",), ("bob smith",)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_engagers
            assert get_recent_engagers(1) == {"jane doe", "bob smith"}

    def test_empty_when_table_missing(self):
        import mysql.connector
        conn = MagicMock(); cur = MagicMock()
        cur.execute.side_effect = mysql.connector.Error("no such table")
        conn.cursor.return_value = cur
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_engagers
            assert get_recent_engagers(1) == set()
