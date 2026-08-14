"""Unit tests for the top-5 skill snapshot DB helpers (issue #1075)."""

import json
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


class TestGetLastRecordedSkills:
    def test_no_row_reads_as_no_snapshot(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(1) == []

    def test_null_column_reads_as_no_snapshot(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=(None,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(1) == []

    def test_json_string_is_parsed(self, fake_cursor):
        conn, cursor = fake_cursor(fetch_one=(json.dumps(["ai strategy", "python"]),))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(42) == ["ai strategy", "python"]
        assert "last_recorded_skills" in cursor.execute.call_args[0][0]
        assert cursor.execute.call_args[0][1] == (42,)

    def test_already_parsed_list_passes_through(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=(["python"],))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(1) == ["python"]

    def test_unparseable_json_reads_as_no_snapshot(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=("{not json",))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(1) == []

    def test_non_list_json_reads_as_no_snapshot(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=(json.dumps({"a": 1}),))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(1) == []

    def test_db_error_reads_as_no_snapshot(self):
        with patch("cqc_lem.platform.db.connection.get_db_connection",
                   side_effect=mysql.connector.Error("boom")):
            from cqc_lem.utilities.db import get_last_recorded_skills
            assert get_last_recorded_skills(1) == []


class TestSetLastRecordedSkills:
    def test_writes_json_and_commits(self, fake_cursor):
        conn, cursor = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_last_recorded_skills
            assert set_last_recorded_skills(7, ["ai strategy"]) is True
        sql, params = cursor.execute.call_args[0]
        assert sql.startswith("UPDATE profiles SET last_recorded_skills")
        assert params == (json.dumps(["ai strategy"]), 7)
        conn.commit.assert_called_once()

    def test_none_is_stored_as_empty_list(self, fake_cursor):
        conn, cursor = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_last_recorded_skills
            assert set_last_recorded_skills(7, None) is True
        assert cursor.execute.call_args[0][1][0] == "[]"

    def test_missing_profile_row_returns_false(self, fake_cursor):
        conn, _ = fake_cursor(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import set_last_recorded_skills
            assert set_last_recorded_skills(7, ["x"]) is False

    def test_db_error_returns_false(self):
        with patch("cqc_lem.platform.db.connection.get_db_connection",
                   side_effect=mysql.connector.Error("boom")):
            from cqc_lem.utilities.db import set_last_recorded_skills
            assert set_last_recorded_skills(7, ["x"]) is False
