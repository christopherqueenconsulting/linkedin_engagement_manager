"""Unit tests for lead-magnet DB helpers."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _conn(fetch_row=None, fetch_one=None):
    conn = MagicMock(); cur = MagicMock()
    cur.fetchone.return_value = fetch_row if fetch_row is not None else fetch_one
    cur.rowcount = 1
    conn.cursor.return_value = cur
    return conn, cur


class TestLeadMagnet:
    def test_defaults_when_no_row(self):
        conn, _ = _conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_lead_magnet_settings
            s = get_lead_magnet_settings(1)
        assert s["enabled"] is False and s["keyword"] is None

    def test_update_upserts(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_lead_magnet_settings
            assert update_lead_magnet_settings(1, {"enabled": True, "keyword": "GUIDE", "message": "here"}) is True
        assert "ON DUPLICATE KEY UPDATE" in cur.execute.call_args[0][0]

    def test_has_received_true_when_row(self):
        conn, _ = _conn(fetch_one=(1,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_received_lead_magnet
            assert has_received_lead_magnet(1, "https://x/in/jane") is True

    def test_has_received_false_when_none(self):
        conn, _ = _conn(fetch_one=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_received_lead_magnet
            assert has_received_lead_magnet(1, "https://x/in/new") is False

    def test_record_sent(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_lead_magnet_sent
            assert record_lead_magnet_sent(1, "https://x/in/jane", 9) is True
        assert "INSERT IGNORE INTO lead_magnet_sent" in cur.execute.call_args[0][0]
