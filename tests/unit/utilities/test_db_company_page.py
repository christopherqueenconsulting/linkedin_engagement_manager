"""Unit tests for update_company_linked_in_url_for_user."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestUpdateCompanyPage:
    def test_sets_url(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_company_linked_in_url_for_user
            assert update_company_linked_in_url_for_user(7, "https://www.linkedin.com/company/x/") is True
        conn.commit.assert_called_once()
        assert cur.execute.call_args[0][1] == ("https://www.linkedin.com/company/x/", 7)

    def test_empty_clears_to_none(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_company_linked_in_url_for_user
            update_company_linked_in_url_for_user(7, "")
        assert cur.execute.call_args[0][1] == (None, 7)
