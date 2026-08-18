"""Unit tests for timezone-related database functions."""

from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_CONNECT = "cqc_lem.platform.db.connection.mysql.connector.connect"
_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"


# ---------------------------------------------------------------------------
# get_db_connection — must set time_zone='+00:00'
# ---------------------------------------------------------------------------

class TestGetDbConnectionTimezone:
    def test_connect_called_with_utc_time_zone(self):
        with patch(_CONNECT) as mock_connect:
            mock_connect.return_value = MagicMock()
            from cqc_lem.utilities.db import get_db_connection
            get_db_connection()

        mock_connect.assert_called_once()
        kwargs = mock_connect.call_args[1]
        assert kwargs.get("time_zone") == "+00:00", (
            "MySQL connection must specify time_zone='+00:00' to prevent session-timezone "
            "from distorting TIMESTAMP reads"
        )


# ---------------------------------------------------------------------------
# get_user_timezone
# ---------------------------------------------------------------------------

class TestGetUserTimezone:
    def test_returns_stored_timezone(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = ("America/New_York",)
            from cqc_lem.utilities.db import get_user_timezone
            result = get_user_timezone(1)
        assert result == "America/New_York"

    def test_returns_default_when_row_is_none(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = None
            from cqc_lem.utilities.db import get_user_timezone
            result = get_user_timezone(99)
        assert result == "America/New_York"

    def test_returns_default_when_field_is_empty(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = ("",)
            from cqc_lem.utilities.db import get_user_timezone
            result = get_user_timezone(5)
        assert result == "America/New_York"

    def test_returns_default_on_db_error(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("fail")
            from cqc_lem.utilities.db import get_user_timezone
            result = get_user_timezone(7)
        assert result == "America/New_York"


# ---------------------------------------------------------------------------
# get_linkedin_profile_url_by_user_id
# ---------------------------------------------------------------------------

class TestGetLinkedInProfileUrlByUserId:
    def test_returns_stored_profile_url(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = (
                "https://www.linkedin.com/in/christopherqueen/",
            )
            from cqc_lem.utilities.db import get_linkedin_profile_url_by_user_id
            result = get_linkedin_profile_url_by_user_id(1)
        assert result == "https://www.linkedin.com/in/christopherqueen/"

    def test_returns_none_when_no_profile(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = None
            from cqc_lem.utilities.db import get_linkedin_profile_url_by_user_id
            result = get_linkedin_profile_url_by_user_id(99)
        assert result is None

    def test_returns_none_on_db_error(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("fail")
            from cqc_lem.utilities.db import get_linkedin_profile_url_by_user_id
            result = get_linkedin_profile_url_by_user_id(7)
        assert result is None


# ---------------------------------------------------------------------------
# update_user_timezone
# ---------------------------------------------------------------------------

class TestUpdateUserTimezone:
    def test_executes_update_with_correct_values(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 1
            from cqc_lem.utilities.db import update_user_timezone
            result = update_user_timezone(3, "Europe/London")

        assert result is True
        args = mock_database_connection["cursor"].execute.call_args[0]
        assert "UPDATE" in args[0].upper()
        assert "Europe/London" in args[1]
        assert 3 in args[1]
        mock_database_connection["connection"].commit.assert_called_once()

    def test_returns_false_on_db_error(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("fail")
            from cqc_lem.utilities.db import update_user_timezone
            result = update_user_timezone(3, "Europe/London")
        assert result is False
