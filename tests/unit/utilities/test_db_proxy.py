"""Unit tests for per-user proxy DB helpers."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestGetUserProxy:
    def test_returns_url_when_set(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_one=("http://10.0.0.5:8080",))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_proxy
            assert get_user_proxy(7) == "http://10.0.0.5:8080"
        args = cursor.execute.call_args[0]
        assert "proxy_url" in args[0] and args[1] == (7,)

    def test_returns_none_when_null(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=(None,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_proxy
            assert get_user_proxy(7) is None

    def test_returns_none_when_no_row(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_proxy
            assert get_user_proxy(99) is None


class TestUpdateUserProxy:
    def test_sets_url(self, fake_cursor):
        conn, cursor = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_proxy
            assert update_user_proxy(7, "socks5://host:1080") is True
        conn.commit.assert_called_once()
        assert cursor.execute.call_args[0][1] == ("socks5://host:1080", 7)

    def test_empty_string_clears_to_none(self, fake_cursor):
        conn, cursor = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_proxy
            update_user_proxy(7, "")
        assert cursor.execute.call_args[0][1] == (None, 7)
