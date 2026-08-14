"""Unit tests for outreach-funnel DB helpers (issue #399)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestOutreachFunnelDb:
    def test_insert_returns_id(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=42)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_outreach_target
            got = insert_outreach_target(1, "https://x/in/jane", target_name="Jane",
                                         context_url="https://x/post/1", draft_text="great post")
        assert got == 42
        assert "INSERT INTO outreach_funnel_targets" in cur.execute.call_args[0][0]

    def test_insert_error_returns_none(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="dupe")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_outreach_target
            assert insert_outreach_target(1, "u") is None

    def test_get_target_and_user_id(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"id": 3, "user_id": 9}, lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_outreach_target, get_outreach_target_user_id
            assert get_outreach_target(3)["user_id"] == 9
            assert get_outreach_target_user_id(3) == 9

    def test_get_target_user_id_none_when_missing(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=None, lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_outreach_target_user_id
            assert get_outreach_target_user_id(3) is None

    def test_get_by_url(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"id": 3}, lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_outreach_target_by_url
            assert get_outreach_target_by_url(1, "https://x/in/jane")["id"] == 3
        sql = cur.execute.call_args[0][0]
        assert "target_profile_url = %s" in sql

    def test_list_returns_pagination_shape(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        cur.fetchone.return_value = {"c": 2}
        cur.fetchall.return_value = [{"id": 1, "stage": "comment", "status": "pending",
                                      "created_at": None, "updated_at": None}]
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_outreach_targets
            out = get_outreach_targets(1, status_filter="pending", stage_filter="comment", page=1, page_size=25)
        assert out["total"] == 2 and out["page"] == 1 and len(out["targets"]) == 1
        # both filters applied
        list_sql = cur.execute.call_args_list[-1][0][0]
        assert "status = %s" in list_sql and "stage = %s" in list_sql

    def test_list_error_returns_empty(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor(lastrowid=7)
        cur.execute.side_effect = mysql.connector.Error(msg="down")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_outreach_targets
            out = get_outreach_targets(1)
        assert out == {"targets": [], "total": 0, "page": 1, "page_size": 25}

    def test_get_approved_filters(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[{"id": 1}], lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_approved_outreach_targets
            rows = get_approved_outreach_targets(5)
        assert len(rows) == 1
        sql = cur.execute.call_args[0][0]
        assert "status = 'approved'" in sql and "stage <> 'completed'" in sql

    def test_users_with_approved(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[(1,), (4,)], lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_users_with_approved_outreach
            assert get_users_with_approved_outreach() == [1, 4]

    def test_update_status(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import OutreachStatus, update_outreach_target_status
            assert update_outreach_target_status(7, OutreachStatus.CANCELED) is True
        assert "UPDATE outreach_funnel_targets SET status" in cur.execute.call_args[0][0]

    def test_partial_update_builds_only_provided_fields(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import OutreachStage, OutreachStatus, update_outreach_target
            assert update_outreach_target(7, draft_text="new", stage=OutreachStage.DM,
                                          status=OutreachStatus.PENDING) is True
        sql = cur.execute.call_args[0][0]
        assert "draft_text = %s" in sql and "stage = %s" in sql and "status = %s" in sql
        assert "target_profile_url" not in sql

    def test_update_empty_draft_still_updates(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_outreach_target
            # empty string is a real value (clears a stale draft) — must NOT be treated as "skip".
            assert update_outreach_target(7, draft_text="") is True
        assert "draft_text = %s" in cur.execute.call_args[0][0]

    def test_update_noop_when_nothing_provided(self):
        from cqc_lem.utilities.db import update_outreach_target
        assert update_outreach_target(7) is False
