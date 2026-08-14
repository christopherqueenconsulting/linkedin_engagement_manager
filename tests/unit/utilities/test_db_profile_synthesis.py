"""Unit tests for the cached profile-synthesis DB helpers (get/set + stale selector)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestGetProfileSynthesis:
    def test_returns_none_when_no_row(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_profile_synthesis
            assert get_profile_synthesis(1) is None

    def test_returns_none_when_synthesis_null(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=(None, None))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_profile_synthesis
            assert get_profile_synthesis(1) is None

    def test_returns_text_and_timestamp(self, fake_cursor):
        from datetime import datetime
        ts = datetime(2026, 7, 1)
        conn, cursor = fake_cursor(fetch_one=("Durable voice brief", ts))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_profile_synthesis
            text, generated_at = get_profile_synthesis(42)
        assert text == "Durable voice brief" and generated_at == ts
        sql = cursor.execute.call_args[0][0]
        assert "synthesis" in sql and "synthesis_generated_at" in sql
        assert cursor.execute.call_args[0][1] == (42,)


class TestSetProfileSynthesis:
    def test_updates_and_stamps_now(self, fake_cursor):
        conn, cursor = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_profile_synthesis
            assert set_profile_synthesis(7, "brief text") is True
        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]
        assert "UPDATE profiles" in sql and "synthesis_generated_at = NOW()" in sql
        assert params == ("brief text", 7)
        conn.commit.assert_called_once()

    def test_returns_false_when_no_profile_row(self, fake_cursor):
        conn, _ = fake_cursor(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_profile_synthesis
            assert set_profile_synthesis(7, "brief") is False


class TestStaleSelector:
    def test_selects_missing_and_stale_user_ids(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_all=[(1,), (5,), (9,)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_ids_needing_profile_synthesis
            ids = get_user_ids_needing_profile_synthesis(stale_days=7)
        assert ids == [1, 5, 9]
        sql = cursor.execute.call_args[0][0]
        # Missing OR stale, only rows that actually have a profile.
        assert "synthesis IS NULL" in sql
        assert "INTERVAL" in sql and "DAY" in sql
        assert "user_id IS NOT NULL" in sql
        assert cursor.execute.call_args[0][1] == (7,)

    def test_empty_when_none_stale(self, fake_cursor):
        conn, _ = fake_cursor(fetch_all=[])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_ids_needing_profile_synthesis
            assert get_user_ids_needing_profile_synthesis() == []
