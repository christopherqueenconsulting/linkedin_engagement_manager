"""Unit tests for the top-5 skill snapshot DB helpers (issue #1075)."""

import json
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


def _mock_conn(fetch_row=None, rowcount=1):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetch_row
    cursor.rowcount = rowcount
    conn.cursor.return_value = cursor
    return conn, cursor


class TestGetLastRecordedSkills:
    def test_no_row_reads_as_no_snapshot(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(1) == []

    def test_null_column_reads_as_no_snapshot(self):
        conn, _ = _mock_conn(fetch_row=(None,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(1) == []

    def test_json_string_is_parsed(self):
        conn, cursor = _mock_conn(fetch_row=(json.dumps(["ai strategy", "python"]),))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(42) == ["ai strategy", "python"]
        assert "last_recorded_skills" in cursor.execute.call_args[0][0]
        assert cursor.execute.call_args[0][1] == (42,)

    def test_already_parsed_list_passes_through(self):
        conn, _ = _mock_conn(fetch_row=(["python"],))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(1) == ["python"]

    def test_unparseable_json_reads_as_no_snapshot(self):
        conn, _ = _mock_conn(fetch_row=("{not json",))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(1) == []

    def test_non_list_json_reads_as_no_snapshot(self):
        conn, _ = _mock_conn(fetch_row=(json.dumps({"a": 1}),))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(1) == []

    def test_db_error_reads_as_no_snapshot(self):
        with patch("cqc_lem.platform.db.connection.get_db_connection",
                   side_effect=mysql.connector.Error("boom")):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(1) == []


class TestSetLastRecordedSkills:
    def test_writes_json_and_commits(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_last_recorded_skills
            assert set_last_recorded_skills(7, ["ai strategy"]) is True
        sql, params = cursor.execute.call_args[0]
        assert sql.startswith("UPDATE profiles SET last_recorded_skills")
        assert params == (json.dumps(["ai strategy"]), 7)
        conn.commit.assert_called_once()

    def test_none_is_stored_as_empty_list(self):
        conn, cursor = _mock_conn(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_last_recorded_skills
            assert set_last_recorded_skills(7, None) is True
        assert cursor.execute.call_args[0][1][0] == "[]"

    def test_missing_profile_row_returns_false(self):
        conn, _ = _mock_conn(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_last_recorded_skills
            assert set_last_recorded_skills(7, ["x"]) is False

    def test_db_error_returns_false(self):
        with patch("cqc_lem.platform.db.connection.get_db_connection",
                   side_effect=mysql.connector.Error("boom")):
            from cqc_lem.utilities.db import set_last_recorded_skills
            assert set_last_recorded_skills(7, ["x"]) is False
