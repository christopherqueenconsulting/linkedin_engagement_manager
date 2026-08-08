"""Unit tests for the story-bank DB helpers (issue #620)."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _mock_conn(fetch_all=None, fetch_one=None):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = fetch_all or []
    cursor.fetchone.return_value = fetch_one
    cursor.rowcount = 1
    conn.cursor.return_value = cursor
    return conn, cursor


class TestGetStoryBankEntries:
    def _row(self, **kw):
        row = {"id": 1, "kind": "anecdote", "title": "Onboarding", "body": "We cut it to 3 days.",
               "happened_at": None, "used_count": 2, "last_used_at": None, "active": 1}
        row.update(kw)
        return row

    def test_normalizes_active_and_counter(self):
        conn, _ = _mock_conn(fetch_all=[self._row(active=1, used_count=None)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_story_bank_entries
            rows = get_story_bank_entries(1)
        assert rows[0]["active"] is True
        assert rows[0]["used_count"] == 0

    def test_orders_least_used_first(self):
        conn, cursor = _mock_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_story_bank_entries
            get_story_bank_entries(1)
        sql = cursor.execute.call_args[0][0]
        assert "ORDER BY used_count ASC" in sql and "last_used_at ASC" in sql

    def test_active_only_filters_in_sql(self):
        conn, cursor = _mock_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_story_bank_entries
            get_story_bank_entries(1, active_only=True)
        assert "active=1" in cursor.execute.call_args[0][0]

    def test_db_error_returns_empty(self):
        import mysql.connector
        conn, cursor = _mock_conn()
        cursor.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_story_bank_entries
            assert get_story_bank_entries(1) == []


class TestCountStoryBankEntries:
    def test_counts_active_entries(self):
        conn, cursor = _mock_conn(fetch_one=(4,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_story_bank_entries
            assert count_story_bank_entries(1) == 4
        assert "active=1" in cursor.execute.call_args[0][0]

    def test_all_entries_when_active_only_is_off(self):
        conn, cursor = _mock_conn(fetch_one=(7,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_story_bank_entries
            assert count_story_bank_entries(1, active_only=False) == 7
        assert "active=1" not in cursor.execute.call_args[0][0]

    def test_db_error_counts_zero(self):
        import mysql.connector
        conn, cursor = _mock_conn()
        cursor.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_story_bank_entries
            assert count_story_bank_entries(1) == 0


class TestUpsertStoryBankEntries:
    def test_new_entry_is_inserted_with_a_defaulted_title(self):
        conn, cursor = _mock_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_story_bank_entries
            assert upsert_story_bank_entries(1, [{"body": "We cut onboarding to 3 days."}]) is True
        sql, rows = cursor.executemany.call_args[0]
        assert sql.startswith("INSERT INTO story_bank")
        assert rows[0][3] == "We cut onboarding to 3 days."
        assert rows[0][2] == "We cut onboarding to 3 days."   # title defaults from the body

    def test_unknown_kind_falls_back_to_anecdote(self):
        conn, cursor = _mock_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_story_bank_entries
            upsert_story_bank_entries(1, [{"body": "x", "kind": "gossip"}])
        assert cursor.executemany.call_args[0][1][0][1] == "anecdote"

    def test_existing_entry_is_updated_not_reinserted(self):
        conn, cursor = _mock_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_story_bank_entries
            upsert_story_bank_entries(1, [{"id": 9, "body": "edited"}])
        sql = cursor.executemany.call_args[0][0]
        assert sql.startswith("UPDATE story_bank")
        assert "used_count" not in sql and "last_used_at" not in sql

    def test_bodyless_rows_are_dropped(self):
        conn, cursor = _mock_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_story_bank_entries
            assert upsert_story_bank_entries(1, [{"title": "just a title", "body": "  "}]) is True
        cursor.executemany.assert_not_called()

    def test_long_title_is_truncated_to_the_column_width(self):
        conn, cursor = _mock_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_story_bank_entries
            upsert_story_bank_entries(1, [{"title": "t" * 400, "body": "x"}])
        assert len(cursor.executemany.call_args[0][1][0][2]) == 255

    def test_db_error_returns_false(self):
        import mysql.connector
        conn, cursor = _mock_conn()
        cursor.executemany.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_story_bank_entries
            assert upsert_story_bank_entries(1, [{"body": "x"}]) is False


class TestDeleteAndRecordUse:
    def test_delete_is_scoped_to_the_user(self):
        conn, cursor = _mock_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import delete_story_bank_entry
            assert delete_story_bank_entry(1, 9) is True
        assert cursor.execute.call_args[0][1] == (1, 9)

    def test_record_use_increments_and_stamps(self):
        conn, cursor = _mock_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_story_bank_use
            assert record_story_bank_use(1, 9) is True
        sql = cursor.execute.call_args[0][0]
        assert "used_count + 1" in sql and "last_used_at = NOW()" in sql

    def test_record_use_on_a_missing_row_is_false(self):
        conn, cursor = _mock_conn()
        cursor.rowcount = 0
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_story_bank_use
            assert record_story_bank_use(1, 404) is False

    def test_delete_db_error_returns_false(self):
        import mysql.connector
        conn, cursor = _mock_conn()
        cursor.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import delete_story_bank_entry
            assert delete_story_bank_entry(1, 9) is False
