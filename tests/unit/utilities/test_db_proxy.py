"""Unit tests for per-user proxy DB helpers."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _mock_conn(fetch_row=None, rowcount=1):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetch_row
    cursor.rowcount = rowcount
    conn.cursor.return_value = cursor
    return conn, cursor


class TestGetUserProxy:
    def test_returns_url_when_set(self):
        conn, cursor = _mock_conn(fetch_row=("http://10.0.0.5:8080",))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_proxy
            assert get_user_proxy(7) == "http://10.0.0.5:8080"
        args = cursor.execute.call_args[0]
        assert "proxy_url" in args[0] and args[1] == (7,)

    def test_returns_none_when_null(self):
        conn, _ = _mock_conn(fetch_row=(None,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_proxy
            assert get_user_proxy(7) is None

    def test_returns_none_when_no_row(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_proxy
            assert get_user_proxy(99) is None


class TestUpdateUserProxy:
    def test_sets_url(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_proxy
            assert update_user_proxy(7, "socks5://host:1080") is True
        conn.commit.assert_called_once()
        assert cursor.execute.call_args[0][1] == ("socks5://host:1080", 7)

    def test_empty_string_clears_to_none(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_user_proxy
            update_user_proxy(7, "")
        assert cursor.execute.call_args[0][1] == (None, 7)
