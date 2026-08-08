"""Unit tests for per-user location database functions."""

from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"


class TestGetUserGeo:
    def test_returns_full_geo_dict(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = (
                30.3321, -81.6556, "America/New_York", "en-US", "Jacksonville", "US",
            )
            from cqc_lem.utilities.db import get_user_geo
            result = get_user_geo(1)
        assert result["latitude"] == pytest.approx(30.3321)
        assert result["longitude"] == pytest.approx(-81.6556)
        assert result["timezone"] == "America/New_York"
        assert result["locale"] == "en-US"
        assert result["city"] == "Jacksonville"
        assert result["country"] == "US"

    def test_returns_none_when_no_row(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = None
            from cqc_lem.utilities.db import get_user_geo
            assert get_user_geo(99) is None

    def test_handles_null_latlng(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].fetchone.return_value = (
                None, None, "UTC", None, None, None,
            )
            from cqc_lem.utilities.db import get_user_geo
            result = get_user_geo(2)
        assert result["latitude"] is None
        assert result["longitude"] is None
        assert result["timezone"] == "UTC"

    def test_returns_none_on_db_error(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("fail")
            from cqc_lem.utilities.db import get_user_geo
            assert get_user_geo(7) is None


class TestUpdateUserLocation:
    def test_update_without_timezone_omits_tz_column(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 1
            from cqc_lem.utilities.db import update_user_location
            result = update_user_location(3, 40.0, -73.0, city="NYC", country="US", locale="en-US")
        assert result is True
        sql = mock_database_connection["cursor"].execute.call_args[0][0]
        assert "timezone" not in sql.lower()
        assert "location_source" in sql.lower()
        mock_database_connection["connection"].commit.assert_called_once()

    def test_update_with_timezone_includes_tz_column(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].rowcount = 1
            from cqc_lem.utilities.db import update_user_location
            result = update_user_location(
                3, 40.0, -73.0, city="NYC", country="US",
                locale="en-US", timezone="America/New_York", source="ip_autocapture",
            )
        assert result is True
        args = mock_database_connection["cursor"].execute.call_args[0]
        assert "timezone" in args[0].lower()
        assert "America/New_York" in args[1]
        assert "ip_autocapture" in args[1]

    def test_returns_false_on_db_error(self, mock_database_connection):
        with patch(_GET_CONN, return_value=mock_database_connection["connection"]):
            mock_database_connection["cursor"].execute.side_effect = mysql.connector.Error("fail")
            from cqc_lem.utilities.db import update_user_location
            assert update_user_location(3, 1.0, 2.0) is False
