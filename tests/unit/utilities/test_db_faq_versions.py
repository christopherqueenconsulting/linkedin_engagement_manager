"""Unit tests for the auto-FAQ DB helpers — issue #507 (entries, versions, candidate queue)."""

from datetime import datetime
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit

_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"
_FEEDBACK = "cqc_lem.platform.db.repositories.feedback"

_ENTRY = {"id": 7, "question": "Does it work with Sales Navigator?", "answer": "Not yet.",
          "cluster_id": 42, "status": "draft", "sort_order": 1000,
          "created_at": datetime(2026, 7, 25, 9, 0), "updated_at": datetime(2026, 7, 25, 9, 0)}
_VERSION = {"id": 3, "faq_entry_id": 7, "question": _ENTRY["question"], "answer": "Not yet.",
            "status": "published", "source": "auto", "created_at": datetime(2026, 7, 25, 9, 0)}


class TestGetFaqEntries:
    def test_returns_drafts_and_published_in_display_order(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[_ENTRY], lastrowid=None)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_faq_entries
            assert get_faq_entries() == [_ENTRY]
        sql, params = cur.execute.call_args[0]
        assert "ORDER BY sort_order ASC, id ASC" in sql
        assert params == ("published", "draft", 200)
        conn.close.assert_called_once()

    def test_unknown_statuses_are_dropped_and_an_empty_set_short_circuits(self):
        with patch(f"{_GET_CONN}") as connect:
            from cqc_lem.utilities.db import get_faq_entries
            assert get_faq_entries(statuses=("nonsense",)) == []
        connect.assert_not_called()

    def test_db_error_returns_an_empty_list(self, fake_cursor):
        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error") as log:
            from cqc_lem.utilities.db import get_faq_entries
            assert get_faq_entries() == []
        log.assert_called_once()


class TestGetFaqEntryByCluster:
    def test_returns_the_entry_for_a_cluster(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=_ENTRY, fetch_all=None, lastrowid=None)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_faq_entry_by_cluster
            assert get_faq_entry_by_cluster("42") == _ENTRY
        assert cur.execute.call_args[0][1] == (42,)

    def test_no_cluster_id_never_queries(self):
        with patch(f"{_GET_CONN}") as connect:
            from cqc_lem.utilities.db import get_faq_entry_by_cluster
            assert get_faq_entry_by_cluster(None) is None
        connect.assert_not_called()

    def test_db_error_returns_none(self, fake_cursor):
        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error"):
            from cqc_lem.utilities.db import get_faq_entry_by_cluster
            assert get_faq_entry_by_cluster(42) is None


class TestUpsertFaqEntry:
    def test_insert_returns_the_new_id_and_keeps_the_existing_one_on_conflict(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=None, lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import FaqStatus, upsert_faq_entry
            assert upsert_faq_entry("Q?", "A.", cluster_id=42, status=FaqStatus.PUBLISHED,
                                    sort_order=1000) == 7
        sql, params = cur.execute.call_args[0]
        # LAST_INSERT_ID(id) is what makes lastrowid the EXISTING row on a duplicate question.
        assert "ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)" in sql
        assert "sort_order" not in sql.split("ON DUPLICATE KEY UPDATE")[1]
        assert params == ("Q?", "A.", 42, "published", 1000)
        conn.commit.assert_called_once()

    def test_empty_question_or_answer_is_never_written(self):
        with patch(f"{_GET_CONN}") as connect:
            from cqc_lem.utilities.db import upsert_faq_entry
            assert upsert_faq_entry("  ", "A.") is None
            assert upsert_faq_entry("Q?", "") is None
        connect.assert_not_called()

    def test_db_error_returns_none(self, fake_cursor):
        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error") as log:
            from cqc_lem.utilities.db import upsert_faq_entry
            assert upsert_faq_entry("Q?", "A.") is None
        log.assert_called_once()


class TestFaqEntryVersions:
    def test_recording_a_version_appends_history(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=None, lastrowid=3)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import FaqStatus, record_faq_entry_version
            assert record_faq_entry_version(7, "Q?", "A.", status=FaqStatus.PUBLISHED) == 3
        sql, params = cur.execute.call_args[0]
        assert sql.startswith("INSERT INTO faq_entry_versions")
        assert params == (7, "Q?", "A.", "published", "auto")

    def test_a_version_without_an_answer_is_not_recorded(self):
        with patch(f"{_GET_CONN}") as connect:
            from cqc_lem.utilities.db import record_faq_entry_version
            assert record_faq_entry_version(7, "Q?", "   ") is None
            assert record_faq_entry_version(None, "Q?", "A.") is None
        connect.assert_not_called()

    def test_history_is_newest_first(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[_VERSION], lastrowid=None)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_faq_entry_versions
            assert get_faq_entry_versions(7) == [_VERSION]
        sql, params = cur.execute.call_args[0]
        assert "ORDER BY id DESC" in sql
        assert params == (7, 20)

    def test_history_for_no_entry_is_empty(self):
        with patch(f"{_GET_CONN}") as connect:
            from cqc_lem.utilities.db import get_faq_entry_versions
            assert get_faq_entry_versions(None) == []
        connect.assert_not_called()

    def test_history_db_error_returns_an_empty_list(self, fake_cursor):
        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error"):
            from cqc_lem.utilities.db import get_faq_entry_versions
            assert get_faq_entry_versions(7) == []


class TestApplyFaqEntryVersion:
    def test_reapplies_the_stored_copy_and_status(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=_VERSION, fetch_all=None, lastrowid=None)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import apply_faq_entry_version
            assert apply_faq_entry_version(7, 3) == _VERSION
        update_sql, update_params = cur.execute.call_args_list[1][0]
        assert update_sql.startswith("UPDATE faq_entries SET")
        assert update_params == (_VERSION["question"], _VERSION["answer"], "published", 7)
        conn.commit.assert_called_once()

    def test_a_version_from_another_entry_changes_nothing(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=None, fetch_all=None, lastrowid=None)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import apply_faq_entry_version
            assert apply_faq_entry_version(7, 999) is None
        assert cur.execute.call_count == 1
        conn.commit.assert_not_called()

    def test_missing_ids_never_query(self):
        with patch(f"{_GET_CONN}") as connect:
            from cqc_lem.utilities.db import apply_faq_entry_version
            assert apply_faq_entry_version(None, 3) is None
            assert apply_faq_entry_version(7, None) is None
        connect.assert_not_called()

    def test_db_error_returns_none(self, fake_cursor):
        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error") as log:
            from cqc_lem.utilities.db import apply_faq_entry_version
            assert apply_faq_entry_version(7, 3) is None
        log.assert_called_once()


class TestGetFaqCandidateFeedback:
    def test_only_triaged_unclustered_rows_are_offered_to_the_faq_pass(self, fake_cursor):
        row = {"id": 1, "body": "How do I pause automation?"}
        conn, cur = fake_cursor(fetch_all=[row], lastrowid=None)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_faq_candidate_feedback
            assert get_faq_candidate_feedback(limit="10") == [row]
        sql, params = cur.execute.call_args[0]
        # `new` rows belong to the auto-filer — the FAQ pass must never claim one.
        assert "status=%s AND cluster_id IS NULL" in sql
        assert "ORDER BY created_at ASC, id ASC" in sql
        assert params == ("triaged", 10)

    def test_db_error_returns_an_empty_list(self, fake_cursor):
        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error") as log:
            from cqc_lem.utilities.db import get_faq_candidate_feedback
            assert get_faq_candidate_feedback() == []
        log.assert_called_once()
