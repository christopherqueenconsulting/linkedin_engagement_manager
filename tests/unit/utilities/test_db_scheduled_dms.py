"""Unit tests for scheduled-DM DB helpers (issue #306)."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _conn(fetch_row=None, fetchall=None, lastrowid=7):
    conn = MagicMock(); cur = MagicMock()
    cur.fetchone.return_value = fetch_row
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = 1
    cur.lastrowid = lastrowid
    conn.cursor.return_value = cur
    return conn, cur


class TestScheduledDmDb:
    def test_insert_returns_id(self):
        from datetime import datetime
        conn, cur = _conn(lastrowid=42)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_scheduled_dm
            got = insert_scheduled_dm(1, "https://x/in/jane", "hi", datetime(2026, 8, 1, 9))
        assert got == 42
        assert "INSERT INTO scheduled_dms" in cur.execute.call_args[0][0]

    def test_get_due_filters_approved(self):
        conn, cur = _conn(fetchall=[(1, MagicMock(), 5)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_due_scheduled_dms
            rows = get_due_scheduled_dms(post_time_delta_minutes=20)
        assert len(rows) == 1
        sql = cur.execute.call_args[0][0]
        assert "status = 'approved'" in sql
        # No lower time bound: an approved DM deferred >24h past its slot (e.g. by the daily DM
        # cap) must stay eligible until sent/canceled — oldest first.
        assert "BETWEEN" not in sql
        assert "scheduled_time <= %s" in sql
        assert "ORDER BY scheduled_time ASC" in sql
        assert len(cur.execute.call_args[0][1]) == 1

    def test_get_orphaned_scheduled_dms(self):
        from datetime import datetime, timedelta, timezone
        conn, cur = _conn(fetchall=[(9, MagicMock(), 5)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_orphaned_scheduled_dms
            rows = get_orphaned_scheduled_dms(lookback_hours=2)
        assert rows == [(9, cur.fetchall.return_value[0][1], 5)]
        sql, params = cur.execute.call_args[0]
        assert "status = 'scheduled'" in sql and "scheduled_time <= %s" in sql
        # cutoff is ~2h in the past
        assert params[0] <= datetime.now(timezone.utc) - timedelta(hours=2) + timedelta(seconds=5)

    def test_get_orphaned_scheduled_dms_error_returns_empty(self):
        import mysql.connector
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="db down")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_orphaned_scheduled_dms
            assert get_orphaned_scheduled_dms() == []

    def test_list_returns_pagination_shape(self):
        conn, cur = _conn()
        cur.fetchone.return_value = {"c": 3}
        cur.fetchall.return_value = [{"id": 1, "scheduled_time": None, "status": "pending"}]
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_scheduled_dms
            out = get_scheduled_dms(1, status_filter="pending", page=1, page_size=25)
        assert out["total"] == 3 and out["page"] == 1 and len(out["dms"]) == 1

    def test_update_status(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ScheduledDmStatus, update_scheduled_dm_status
            assert update_scheduled_dm_status(7, ScheduledDmStatus.SENT) is True
        assert "UPDATE scheduled_dms SET status" in cur.execute.call_args[0][0]

    def test_partial_update_builds_only_provided_fields(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_scheduled_dm
            assert update_scheduled_dm(7, message="new body") is True
        sql = cur.execute.call_args[0][0]
        assert "message = %s" in sql and "recipient_profile_url" not in sql

    def test_update_noop_when_nothing_provided(self):
        from cqc_lem.utilities.db import update_scheduled_dm
        assert update_scheduled_dm(7) is False


class TestNurtureSourceDb:
    """The `source` column (issue #485) is what lets an auto-drafted nurture reply share the
    operator's approval queue without being confused for a DM they wrote.
    """

    def test_insert_records_the_source(self):
        from datetime import datetime
        conn, cur = _conn(lastrowid=11)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import SCHEDULED_DM_SOURCE_NURTURE, insert_scheduled_dm
            got = insert_scheduled_dm(1, "https://x/in/jane", "hi", datetime(2026, 8, 1, 9),
                                      source=SCHEDULED_DM_SOURCE_NURTURE)
        assert got == 11
        sql, params = cur.execute.call_args[0]
        assert "source" in sql
        assert "nurture" in params

    def test_insert_without_source_stores_null(self):
        from datetime import datetime
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_scheduled_dm
            insert_scheduled_dm(1, "https://x/in/jane", "hi", datetime(2026, 8, 1, 9))
        assert cur.execute.call_args[0][1][4] is None

    def test_has_open_scheduled_dm_true_when_a_draft_is_queued(self):
        conn, cur = _conn(fetch_row=(1,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import SCHEDULED_DM_SOURCE_NURTURE, has_open_scheduled_dm
            assert has_open_scheduled_dm(1, "https://x/in/jane",
                                         source=SCHEDULED_DM_SOURCE_NURTURE) is True
        sql, params = cur.execute.call_args[0]
        assert "source=%s" in sql
        # Only un-sent states block a new draft; a delivered DM must not freeze the thread forever.
        assert "pending" in params and "approved" in params and "scheduled" in params
        assert "sent" not in params and "canceled" not in params

    def test_has_open_scheduled_dm_false_when_none(self):
        conn, _cur = _conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_open_scheduled_dm
            assert has_open_scheduled_dm(1, "https://x/in/jane") is False

    def test_has_open_scheduled_dm_without_source_checks_every_queued_dm(self):
        conn, cur = _conn(fetch_row=None)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_open_scheduled_dm
            has_open_scheduled_dm(1, "https://x/in/jane")
        assert "source=%s" not in cur.execute.call_args[0][0]

    def test_has_open_scheduled_dm_fails_safe_to_true(self):
        import mysql.connector
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="db down")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_open_scheduled_dm
            assert has_open_scheduled_dm(1, "https://x/in/jane") is True

    def test_has_open_scheduled_dm_true_without_a_recipient(self):
        from cqc_lem.utilities.db import has_open_scheduled_dm
        assert has_open_scheduled_dm(1, "") is True

    def test_count_created_today_filters_by_source(self):
        conn, cur = _conn(fetch_row=(4,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import SCHEDULED_DM_SOURCE_NURTURE, count_scheduled_dms_created_today
            assert count_scheduled_dms_created_today(1, source=SCHEDULED_DM_SOURCE_NURTURE) == 4
        sql, params = cur.execute.call_args[0]
        assert "created_at >= CURDATE()" in sql and "source=%s" in sql
        assert params == (1, "nurture")

    def test_count_created_today_without_source(self):
        conn, cur = _conn(fetch_row=(0,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_scheduled_dms_created_today
            assert count_scheduled_dms_created_today(1) == 0
        assert "source=%s" not in cur.execute.call_args[0][0]

    def test_count_created_today_error_returns_zero(self):
        import mysql.connector
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error(msg="db down")
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_scheduled_dms_created_today
            assert count_scheduled_dms_created_today(1) == 0

    def test_nurture_default_template_exists(self):
        from cqc_lem.utilities.db import _DM_DEFAULT_TEMPLATES
        assert "{first_name}" in _DM_DEFAULT_TEMPLATES["nurture"]
