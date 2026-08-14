"""Unit tests for the public FAQ DB helper (issue #506)."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"
_FEEDBACK = "cqc_lem.platform.db.repositories.feedback"

_ROW = {"id": 3, "question": "Can I cancel anytime?", "answer": "Yes.", "cluster_id": None,
        "updated_at": datetime(2026, 7, 25, 12, 0)}


class TestGetPublishedFaqEntries:
    def test_returns_published_entries_in_display_order(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[_ROW])
        with patch(f"{_GET_CONN}", return_value=conn):
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

    def test_limit_is_coerced_to_an_int(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_published_faq_entries
            assert get_published_faq_entries(limit="5") == []
        assert cur.execute.call_args[0][1] == ("published", 5)

    def test_no_rows_returns_an_empty_list(self, fake_cursor):
        conn, _cur = fake_cursor(fetch_all=None)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_published_faq_entries
            assert get_published_faq_entries() == []

    def test_db_errors_are_swallowed_so_the_public_page_still_renders(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), \
             patch(f"{_FEEDBACK}.log_error") as log:
            from cqc_lem.utilities.db import get_published_faq_entries
            assert get_published_faq_entries() == []
        log.assert_called_once()
        conn.close.assert_called_once()

    def test_an_unreachable_database_still_returns_an_empty_list(self):
        """MySQL down means get_db_connection() itself raises — the landing page must not 500."""
        with patch(f"{_GET_CONN}", side_effect=mysql.connector.Error("no route")), \
             patch(f"{_FEEDBACK}.log_error") as log:
            from cqc_lem.utilities.db import get_published_faq_entries
            assert get_published_faq_entries() == []
        log.assert_called_once()

    def test_a_failing_cursor_still_returns_an_empty_list(self):
        conn = MagicMock()
        conn.cursor.side_effect = mysql.connector.Error("cursor boom")
        with patch(f"{_GET_CONN}", return_value=conn), \
             patch(f"{_FEEDBACK}.log_error") as log:
            from cqc_lem.utilities.db import get_published_faq_entries
            assert get_published_faq_entries() == []
        log.assert_called_once()
        conn.close.assert_called_once()
