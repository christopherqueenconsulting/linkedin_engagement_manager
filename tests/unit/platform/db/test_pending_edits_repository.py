"""`update_pending_row` and the post-id insert, at the SQL they send (issue #2684, MCP W1).

The handler tests prove which seats reach these; this file proves what the SQL itself guarantees:
the pending predicate is IN the UPDATE (so a status that moves between the check and the write
still refuses the write), the row is locked first, and the answer never rests on `rowcount`.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"
_POSTS = "cqc_lem.platform.db.repositories.posts"


def _statements(cursor: Any) -> list[tuple[str, tuple]]:
    return [(c.args[0], tuple(c.args[1])) for c in cursor.execute.call_args_list]


class TestThePendingPredicateIsInTheSql:
    @pytest.mark.parametrize("kind,table,pending", [
        ("scheduled_dm", "scheduled_dms", ("pending",)),
        ("connection_request", "connection_requests", ("pending",)),
        ("lead_signal", "lead_signals", ("new",)),
        ("catchup_touch", "catchup_touches", ("pending",)),
        ("outreach_target", "outreach_funnel_targets", ("pending",)),
        ("post", "posts", ("pending",)),
        ("curated_source", "curated_sources", ("new", "drafted")),
    ])
    def test_a_pending_row_is_locked_then_updated_under_the_same_predicate(
            self, fake_cursor: Any, kind: str, table: str, pending: tuple) -> None:
        from cqc_lem.utilities.db import PendingEdit, update_pending_row

        conn, cur = fake_cursor(fetch_one=(pending[-1],), rowcount=1)
        column = {"lead_signal": "draft_response", "post": "content",
                  "outreach_target": "draft_text", "curated_source": "status"}.get(kind, "message")
        with patch(_GET_CONN, return_value=conn):
            assert update_pending_row(kind, 11, 22, {column: "x"}) is PendingEdit.UPDATED
        (select, select_params), (update, update_params) = _statements(cur)
        assert select == f"SELECT status FROM {table} WHERE id = %s AND user_id = %s FOR UPDATE"
        assert select_params == (11, 22)
        placeholders = ", ".join(["%s"] * len(pending))
        assert update == (f"UPDATE {table} SET {column} = %s "
                          f"WHERE id = %s AND user_id = %s AND status IN ({placeholders})")
        assert update_params == ("x", 11, 22, *pending)
        conn.commit.assert_called_once()

    @pytest.mark.parametrize("status", ["approved", "sent", "failed", "canceled", "scheduled"])
    def test_a_row_that_left_pending_is_never_written(self, fake_cursor: Any, status: str) -> None:
        from cqc_lem.utilities.db import PendingEdit, update_pending_row

        conn, cur = fake_cursor(fetch_one=(status,))
        with patch(_GET_CONN, return_value=conn):
            assert update_pending_row("scheduled_dm", 1, 2, {"message": "m"}) is PendingEdit.NOT_PENDING
        assert len(_statements(cur)) == 1, "only the locking read ran"

    def test_a_missing_or_foreign_row_is_not_pending(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import PendingEdit, update_pending_row

        conn, cur = fake_cursor(fetch_one=None)
        with patch(_GET_CONN, return_value=conn):
            assert update_pending_row("lead_signal", 1, 2, {"draft_response": "d"}) \
                is PendingEdit.NOT_PENDING
        assert len(_statements(cur)) == 1

    def test_a_lead_signals_pending_word_is_new_not_pending(self, fake_cursor: Any) -> None:
        """`LeadSignalStatus` has no 'pending'; reading it as one would refuse every draft edit."""
        from cqc_lem.utilities.db import PendingEdit, update_pending_row

        conn, _cur = fake_cursor(fetch_one=("pending",))
        with patch(_GET_CONN, return_value=conn):
            assert update_pending_row("lead_signal", 1, 2, {"draft_response": "d"}) \
                is PendingEdit.NOT_PENDING

    def test_an_unchanged_resave_is_still_updated(self, fake_cursor: Any) -> None:
        """MySQL counts CHANGED rows; an identical re-save has rowcount 0 and is still a success."""
        from cqc_lem.utilities.db import PendingEdit, update_pending_row

        conn, _cur = fake_cursor(fetch_one=("pending",), rowcount=0)
        with patch(_GET_CONN, return_value=conn):
            assert update_pending_row("catchup_touch", 1, 2, {"message": "same"}) \
                is PendingEdit.UPDATED

    def test_a_database_error_is_failed_and_logged(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import PendingEdit, update_pending_row

        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("down"))
        with patch(_GET_CONN, return_value=conn), \
             patch("cqc_lem.platform.db.repositories.pending_edits.log_error") as err:
            assert update_pending_row("post", 1, 2, {"content": "c"}) is PendingEdit.FAILED
        err.assert_called_once()
        assert err.call_args.kwargs["user_id"] == 2

    def test_values_are_bound_the_way_the_connector_needs(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import PostStatus, PostType, update_pending_row

        conn, cur = fake_cursor(fetch_one=("pending",))
        when = datetime(2099, 1, 1, 9, 30, tzinfo=timezone.utc)
        with patch(_GET_CONN, return_value=conn):
            update_pending_row("post", 3, 4, {"post_type": PostType.TEXT, "scheduled_time": when,
                                              "status": PostStatus.PENDING, "video_url": None})
        _update, params = _statements(cur)[1]
        # Columns are written in sorted order; enums by value, datetimes naive UTC, None as NULL.
        assert params == ("text", datetime(2099, 1, 1, 9, 30), "pending", None, 3, 4, "pending")

    @pytest.mark.parametrize("kind,fields", [
        ("nope", {"message": "m"}),
        ("scheduled_dm", {}),
        ("scheduled_dm", {"user_id": 9}),
        ("post", {"approved_by": "user:1"}),
        ("connection_request", {"status = 'approved', message": "x"}),
    ])
    def test_anything_off_the_whitelist_is_refused_before_any_sql(
            self, fake_cursor: Any, kind: str, fields: dict) -> None:
        from cqc_lem.utilities.db import update_pending_row

        conn, cur = fake_cursor()
        with patch(_GET_CONN, return_value=conn), pytest.raises(ValueError):
            update_pending_row(kind, 1, 2, fields)
        cur.execute.assert_not_called()


class TestInsertPostReturningId:
    def test_it_answers_the_new_rows_id(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import PostType, insert_post_returning_id

        conn, _cur = fake_cursor(rowcount=1, lastrowid=4242)
        with patch(_GET_CONN, return_value=conn), patch(f"{_POSTS}.get_user_id", return_value=3):
            assert insert_post_returning_id("a@b.c", "body", datetime(2099, 1, 1),
                                            PostType.TEXT) == 4242

    def test_an_unknown_email_or_an_error_is_none(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import PostType, insert_post_returning_id

        with patch(f"{_POSTS}.get_user_id", return_value=None):
            assert insert_post_returning_id("a@b.c", "body", datetime(2099, 1, 1),
                                            PostType.TEXT) is None
        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("down"))
        with patch(_GET_CONN, return_value=conn), patch(f"{_POSTS}.get_user_id", return_value=3):
            assert insert_post_returning_id("a@b.c", "body", datetime(2099, 1, 1),
                                            PostType.TEXT) is None

    def test_insert_post_keeps_its_bool_contract(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import PostType, insert_post

        conn, _cur = fake_cursor(rowcount=1, lastrowid=7)
        with patch(_GET_CONN, return_value=conn), patch(f"{_POSTS}.get_user_id", return_value=3):
            assert insert_post("a@b.c", "body", datetime(2099, 1, 1), PostType.TEXT) is True
        conn, _cur = fake_cursor(rowcount=0)
        with patch(_GET_CONN, return_value=conn), patch(f"{_POSTS}.get_user_id", return_value=3):
            assert insert_post("a@b.c", "body", datetime(2099, 1, 1), PostType.TEXT) is False


class TestTheAssetBackfillNeverPicksATextPost:
    """A machine-made draft is a PENDING text post; the backfill must not spend media on it.

    The real-database proof is `tests/integration/test_queue_only_edits_db.py`; this pins the query
    shape in the fast lane so a widening fails here first.
    """

    def test_only_media_post_types_are_selected(self, fake_cursor: Any) -> None:
        from cqc_lem.utilities.db import get_unposted_posts_missing_assets

        conn, cur = fake_cursor(fetch_all=[])
        with patch(_GET_CONN, return_value=conn):
            get_unposted_posts_missing_assets()
        sql = cur.execute.call_args.args[0]
        assert "post_type = 'video'" in sql
        assert "post_type IN ('carousel', 'document')" in sql
        assert "'text'" not in sql
