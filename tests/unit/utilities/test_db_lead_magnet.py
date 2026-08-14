"""Unit tests for lead-magnet DB helpers."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestLeadMagnet:
    def test_defaults_when_no_row(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_lead_magnet_settings
            s = get_lead_magnet_settings(1)
        assert s["enabled"] is False and s["keyword"] is None

    def test_update_upserts(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_lead_magnet_settings
            assert update_lead_magnet_settings(1, {"enabled": True, "keyword": "GUIDE", "message": "here"}) is True
        assert "ON DUPLICATE KEY UPDATE" in cur.execute.call_args[0][0]

    def test_has_received_true_when_row(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_received_lead_magnet
            assert has_received_lead_magnet(1, "https://x/in/jane") is True

    def test_has_received_false_when_none(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_received_lead_magnet
            assert has_received_lead_magnet(1, "https://x/in/new") is False

    def test_record_sent(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_lead_magnet_sent
            assert record_lead_magnet_sent(1, "https://x/in/jane", 9) is True
        assert "INSERT IGNORE INTO lead_magnet_sent" in cur.execute.call_args[0][0]
