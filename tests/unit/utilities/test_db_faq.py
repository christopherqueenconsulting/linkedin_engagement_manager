"""Unit tests for the public FAQ DB helper (issue #506)."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"

_ROW = {"id": 3, "question": "Can I cancel anytime?", "answer": "Yes.", "cluster_id": None,
        "updated_at": datetime(2026, 7, 25, 12, 0)}


def _conn(fetchall=None):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = fetchall
    conn.cursor.return_value = cur
    return conn, cur


class TestGetPublishedFaqEntries:
    def test_returns_published_entries_in_display_order(self):
        conn, cur = _conn(fetchall=[_ROW])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_published_faq_entries
            assert get_published_faq_entries() == [_ROW]
        sql, params = cur.execute.call_args[0]
        # Drafts written by the auto-FAQ pass must never reach the public page.
        assert "WHERE status = %s" in sql
        assert "ORDER BY sort_order ASC, id ASC" in sql
        assert params == ("published", 50)
        conn.cursor.assert_called_once_with(dictionary=True)
        cur.close.assert_called_once()
        conn.close.assert_called_once()

    def test_limit_is_coerced_to_an_int(self):
        conn, cur = _conn(fetchall=[])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_published_faq_entries
            assert get_published_faq_entries(limit="5") == []
        assert cur.execute.call_args[0][1] == ("published", 5)

    def test_no_rows_returns_an_empty_list(self):
        conn, _cur = _conn(fetchall=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_published_faq_entries
            assert get_published_faq_entries() == []

    def test_db_errors_are_swallowed_so_the_public_page_still_renders(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.execute.side_effect = mysql.connector.Error("boom")
        conn.cursor.return_value = cur
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch(f"{_DB}.log_error") as log:
            from cqc_lem.utilities.db import get_published_faq_entries
            assert get_published_faq_entries() == []
        log.assert_called_once()
        conn.close.assert_called_once()

    def test_an_unreachable_database_still_returns_an_empty_list(self):
        """MySQL down means get_db_connection() itself raises — the landing page must not 500."""
        with patch("cqc_lem.platform.db.connection.get_db_connection", side_effect=mysql.connector.Error("no route")), \
             patch(f"{_DB}.log_error") as log:
            from cqc_lem.utilities.db import get_published_faq_entries
            assert get_published_faq_entries() == []
        log.assert_called_once()

    def test_a_failing_cursor_still_returns_an_empty_list(self):
        conn = MagicMock()
        conn.cursor.side_effect = mysql.connector.Error("cursor boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch(f"{_DB}.log_error") as log:
            from cqc_lem.utilities.db import get_published_faq_entries
            assert get_published_faq_entries() == []
        log.assert_called_once()
        conn.close.assert_called_once()
