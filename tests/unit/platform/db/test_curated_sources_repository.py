"""The `curated_sources` repository: SQL shape, fail-soft readers, and the ceiling window read.

Patched at `platform.db.connection.get_db_connection` (the ONE definition, test_connection_seam).
"""

from datetime import datetime, timezone
from unittest.mock import patch

import mysql.connector
import pytest

from cqc_lem.platform.db.repositories import curated_sources as repo

pytestmark = pytest.mark.unit

_CONN = "cqc_lem.platform.db.connection.get_db_connection"
_ERR = mysql.connector.Error("boom")


def test_the_facade_re_exports_every_reader():
    from cqc_lem.utilities import db
    for name in ("insert_curated_source", "get_curated_sources", "get_curated_source",
                 "get_draftable_curated_sources", "update_curated_source_status",
                 "attach_curated_source_to_post", "post_is_curated", "get_post_curated_context",
                 "get_curated_neighbors", "get_curated_summaries", "curated_url_hash"):
        assert getattr(db, name) is getattr(repo, name)
        assert name in db.__all__


class TestInsert:
    def test_insert_ignore_with_hash_and_clipping(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1, lastrowid=12)
        item = {"platform": "rss", "url": " https://a.b/x ", "title": "T" * 600,
                "licence": None, "paywalled": True, "link_only": True,
                "published_at": datetime(2026, 10, 6, 10, tzinfo=timezone.utc)}
        with patch(_CONN, return_value=conn):
            assert repo.insert_curated_source(1, item, status="blocked",
                                              block_reason="paywall") == 12
        sql, params = cur.execute.call_args.args
        assert sql.startswith("INSERT IGNORE INTO curated_sources")
        assert params[2] == "https://a.b/x"
        assert params[3] == repo.curated_url_hash("https://a.b/x")
        assert params[6] == 1 and params[13] == 1
        assert len(params[9]) == 512
        assert params[12] == "unknown"
        assert params[14].tzinfo is None
        assert params[15:] == ("blocked", "paywall")
        conn.commit.assert_called_once()

    def test_duplicate_empty_and_error(self, fake_cursor):
        conn, _ = fake_cursor(rowcount=0)
        with patch(_CONN, return_value=conn):
            assert repo.insert_curated_source(1, {"url": "https://a.b"}) is None
        assert repo.insert_curated_source(1, {"url": ""}) is None
        conn, _ = fake_cursor(execute_error=_ERR)
        with patch(_CONN, return_value=conn):
            assert repo.insert_curated_source(1, {"url": "https://a.b"}) is None


class TestReads:
    ROW = {"id": 3, "link_only": 1, "paywalled": 0, "status": "new"}

    def test_list_and_filter(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[dict(self.ROW)])
        with patch(_CONN, return_value=conn):
            rows = repo.get_curated_sources(1, status="new", limit=5)
        assert rows == [{"id": 3, "link_only": True, "paywalled": False, "status": "new"}]
        sql, params = cur.execute.call_args.args
        assert "status = %s" in sql and params == [1, "new", 5]

    def test_a_read_error_is_none_not_empty(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=_ERR)
        with patch(_CONN, return_value=conn):
            assert repo.get_curated_sources(1) is None
            assert repo.get_draftable_curated_sources(1) == []
            assert repo.get_curated_source(3) is None
            assert repo.post_is_curated(3) is None
            assert repo.get_curated_neighbors(1, 3) is None
            assert repo.get_curated_summaries(1, [3]) == {}
            assert repo.update_curated_source_status(3, "new") is False
            assert repo.attach_curated_source_to_post(1, 3, "link") is False

    def test_one_scoped(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=dict(self.ROW))
        with patch(_CONN, return_value=conn):
            assert repo.get_curated_source(3, user_id=1)["link_only"] is True
        assert cur.execute.call_args.args[1] == [3, 1]

    def test_post_is_curated(self, fake_cursor):
        for row, expected in (((5,), True), ((None,), False), (None, False)):
            conn, _ = fake_cursor(fetch_one=row)
            with patch(_CONN, return_value=conn):
                assert repo.post_is_curated(1) is expected

    def test_summaries(self, fake_cursor):
        assert repo.get_curated_summaries(1, []) == {}
        conn, cur = fake_cursor(fetch_all=[dict(self.ROW)])
        with patch(_CONN, return_value=conn):
            assert list(repo.get_curated_summaries(1, [3, None])) == [3]
        assert cur.execute.call_args.args[1] == [1, 3]


class TestWrites:
    def test_status_with_reason_and_post(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch(_CONN, return_value=conn):
            assert repo.update_curated_source_status(3, "drafted", block_reason="x" * 300,
                                                     post_id=10)
        sql, params = cur.execute.call_args.args
        assert "status = %s, block_reason = %s, post_id = %s" in sql
        assert len(params[1]) == 255 and params[2:] == [10, 3]

    def test_attach(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch(_CONN, return_value=conn):
            assert repo.attach_curated_source_to_post(10, 3, "reshare")
        assert cur.execute.call_args.args[1] == (3, "reshare", 10)


class TestContext:
    def test_not_curated(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"post_id": 10, "curated_source_id": None})
        with patch(_CONN, return_value=conn):
            assert repo.get_post_curated_context(10) is None

    def test_curated_joins_its_source(self, fake_cursor):
        post = {"post_id": 10, "post_status": "approved", "approved_by": "user:1",
                "curated_source_id": 3}
        conn, _ = fake_cursor(fetch_one_side_effect=[post, {"id": 3, "link_only": 0,
                                                            "paywalled": 0}])
        with patch(_CONN, return_value=conn):
            context = repo.get_post_curated_context(10)
        assert context["approved_by"] == "user:1"
        assert context["source"]["id"] == 3

    def test_unreadable_is_marked(self, fake_cursor):
        conn, _ = fake_cursor(execute_error=_ERR)
        with patch(_CONN, return_value=conn):
            assert repo.get_post_curated_context(10) == {"unreadable": True}


class TestNeighbors:
    def test_before_is_oldest_first_and_after_nearest_first(self, fake_cursor):
        slot = datetime(2026, 10, 7, 9)
        conn, cur = fake_cursor(fetch_one=(slot,),
                                fetch_all_side_effect=[[(1,), (0,)], [(0,), (1,)]])
        with patch(_CONN, return_value=conn):
            before, after = repo.get_curated_neighbors(1, 10, reach=2)
        assert before == [False, True]      # DESC read reversed: nearest is LAST
        assert after == [False, True]
        assert "status <> 'rejected'" in cur.execute.call_args_list[1].args[0]

    def test_unknown_slot_is_none(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch(_CONN, return_value=conn):
            assert repo.get_curated_neighbors(1, 10) is None
