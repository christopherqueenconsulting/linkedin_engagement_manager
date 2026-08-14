"""Issue #742 — the app-level credential store behind DB-over-env token precedence."""

from datetime import datetime, timezone
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


class TestGetAppCredential:
    def test_returns_the_stored_value(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"value": "1//0gabc"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_app_credential
            assert get_app_credential("youtube_refresh_token") == "1//0gabc"
        assert cur.execute.call_args[0][1] == ("youtube_refresh_token",)

    def test_missing_row_is_none_so_the_caller_falls_back_to_env(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_app_credential
            assert get_app_credential("youtube_refresh_token") is None

    def test_a_stored_empty_string_reads_as_unset(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"value": ""})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_app_credential
            assert get_app_credential("youtube_refresh_token") is None

    def test_a_db_error_is_none_not_an_exception(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=None)
        cur.execute.side_effect = mysql.connector.Error("db down")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_app_credential
            assert get_app_credential("youtube_refresh_token") is None
        conn.close.assert_called_once()


class TestSetAppCredential:
    def test_upserts_and_commits(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_app_credential
            assert set_app_credential("youtube_refresh_token", "tok", note="re-minted") is True
        assert "ON DUPLICATE KEY UPDATE" in cur.execute.call_args[0][0]
        assert cur.execute.call_args[0][1] == ("youtube_refresh_token", "tok", "re-minted")
        conn.commit.assert_called_once()

    def test_a_db_error_reports_failure_rather_than_claiming_a_write(self, fake_cursor):
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("db down")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_app_credential
            assert set_app_credential("youtube_refresh_token", "tok") is False


class TestGetAppCredentialUpdatedAt:
    def test_returns_the_write_timestamp(self, fake_cursor):
        stamp = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        conn, _ = fake_cursor(fetch_one={"updated_at": stamp})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_app_credential_updated_at
            assert get_app_credential_updated_at("youtube_refresh_token") == stamp

    def test_never_written_is_none(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_app_credential_updated_at
            assert get_app_credential_updated_at("youtube_refresh_token") is None
