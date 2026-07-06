"""Unit tests for the cached profile-synthesis DB helpers (get/set + stale selector)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _mock_conn(fetch_row=None, fetch_all=None, rowcount=1):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetch_row
    cursor.fetchall.return_value = fetch_all if fetch_all is not None else []
    cursor.rowcount = rowcount
    conn.cursor.return_value = cursor
    return conn, cursor


class TestGetProfileSynthesis:
    def test_returns_none_when_no_row(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_profile_synthesis
            assert get_profile_synthesis(1) is None

    def test_returns_none_when_synthesis_null(self):
        conn, _ = _mock_conn(fetch_row=(None, None))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_profile_synthesis
            assert get_profile_synthesis(1) is None

    def test_returns_text_and_timestamp(self):
        from datetime import datetime
        ts = datetime(2026, 7, 1)
        conn, cursor = _mock_conn(fetch_row=("Durable voice brief", ts))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_profile_synthesis
            text, generated_at = get_profile_synthesis(42)
        assert text == "Durable voice brief" and generated_at == ts
        sql = cursor.execute.call_args[0][0]
        assert "synthesis" in sql and "synthesis_generated_at" in sql
        assert cursor.execute.call_args[0][1] == (42,)


class TestSetProfileSynthesis:
    def test_updates_and_stamps_now(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_profile_synthesis
            assert set_profile_synthesis(7, "brief text") is True
        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]
        assert "UPDATE profiles" in sql and "synthesis_generated_at = NOW()" in sql
        assert params == ("brief text", 7)
        conn.commit.assert_called_once()

    def test_returns_false_when_no_profile_row(self):
        conn, _ = _mock_conn(rowcount=0)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_profile_synthesis
            assert set_profile_synthesis(7, "brief") is False


class TestStaleSelector:
    def test_selects_missing_and_stale_user_ids(self):
        conn, cursor = _mock_conn(fetch_all=[(1,), (5,), (9,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_ids_needing_profile_synthesis
            ids = get_user_ids_needing_profile_synthesis(stale_days=7)
        assert ids == [1, 5, 9]
        sql = cursor.execute.call_args[0][0]
        # Missing OR stale, only rows that actually have a profile.
        assert "synthesis IS NULL" in sql
        assert "INTERVAL" in sql and "DAY" in sql
        assert "user_id IS NOT NULL" in sql
        assert cursor.execute.call_args[0][1] == (7,)

    def test_empty_when_none_stale(self):
        conn, _ = _mock_conn(fetch_all=[])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_ids_needing_profile_synthesis
            assert get_user_ids_needing_profile_synthesis() == []
