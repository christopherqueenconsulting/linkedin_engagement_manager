"""Unit tests for update_company_linked_in_url_for_user."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _conn(rowcount=1):
    conn = MagicMock()
    cur = MagicMock()
    cur.rowcount = rowcount
    conn.cursor.return_value = cur
    return conn, cur


class TestUpdateCompanyPage:
    def test_sets_url(self):
        conn, cur = _conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_company_linked_in_url_for_user
            assert update_company_linked_in_url_for_user(7, "https://www.linkedin.com/company/x/") is True
        conn.commit.assert_called_once()
        assert cur.execute.call_args[0][1] == ("https://www.linkedin.com/company/x/", 7)

    def test_empty_clears_to_none(self):
        conn, cur = _conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_company_linked_in_url_for_user
            update_company_linked_in_url_for_user(7, "")
        assert cur.execute.call_args[0][1] == (None, 7)
