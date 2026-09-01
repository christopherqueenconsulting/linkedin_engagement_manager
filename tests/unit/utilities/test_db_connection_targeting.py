"""Unit tests for the connection-targeting DB helpers (issue #486)."""

from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


class TestInsertWithProvenance:
    def test_stores_source_icp_and_reasons(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=11)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_connection_request
            got = insert_connection_request(1, "https://x/in/jane", message="hi",
                                            recipient_name="Jane", source="own_post",
                                            icp_score=72, reasons="engaged with your content")
        assert got == 11
        sql, params = cur.execute.call_args[0]
        assert "source, icp_score, reasons" in sql
        # recipient_email (issue #1836) rides along last, after the provenance columns.
        assert params[-4:] == ("own_post", 72, "engaged with your content", None)

    def test_blank_reasons_stored_as_null(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_connection_request
            insert_connection_request(1, "https://x/in/jane", reasons="")
        assert cur.execute.call_args[0][1][-2] is None

    def test_select_columns_include_provenance(self):
        from cqc_lem.utilities.db import _CONN_REQ_COLS
        assert {"source", "icp_score", "reasons"}.issubset(set(_CONN_REQ_COLS))


class TestCountOpenConnectionRequests:
    def test_counts_unsent_statuses_only(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(3,), lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_open_connection_requests
            assert count_open_connection_requests(1) == 3
        sql, params = cur.execute.call_args[0]
        assert "status IN ('pending','approved','sending')" in sql
        assert params == (1,)

    def test_no_rows_is_zero(self, fake_cursor):
        conn, _cur = fake_cursor(fetch_one=None, lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_open_connection_requests
            assert count_open_connection_requests(1) == 0

    def test_db_error_is_zero(self, fake_cursor):
        conn, _cur = fake_cursor(lastrowid=7, execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_open_connection_requests
            assert count_open_connection_requests(1) == 0


class TestRequestedPersonKeys:
    def test_keys_every_status_and_prefers_the_slug(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[("Jane Doe", "https://www.linkedin.com/in/jane-doe?x=1"),
                                    ("Nameless", None), (None, None)], lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_requested_person_keys
            keys = get_requested_person_keys(1)
        # No status filter: a canceled/failed target must not be re-sourced tomorrow.
        assert "status" not in cur.execute.call_args[0][0]
        assert keys == {"in:jane-doe", "name:nameless"}

    def test_db_error_is_empty(self, fake_cursor):
        conn, _cur = fake_cursor(lastrowid=7, execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_requested_person_keys
            assert get_requested_person_keys(1) == set()


class TestEngagerCandidates:
    def test_reads_recent_engagers_with_a_profile_url(self, fake_cursor):
        rows = [{"person_name": "Jane", "person_profile_url": "https://x/in/jane",
                 "occurred_at": None}]
        conn, cur = fake_cursor(fetch_all=rows, lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engager_candidates
            assert get_engager_candidates(1, days=30) == rows
        sql, params = cur.execute.call_args[0]
        assert "FROM post_engagers" in sql
        assert "engager_profile_url IS NOT NULL" in sql  # nobody to invite without a URL
        assert params == (1, 30)

    def test_db_error_is_empty(self, fake_cursor):
        conn, _cur = fake_cursor(lastrowid=7, execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engager_candidates
            assert get_engager_candidates(1) == []


class TestTargetingPreferences:
    def test_defaults_are_suggest_mode(self):
        from cqc_lem.utilities.db import _ENGAGEMENT_DEFAULTS
        assert _ENGAGEMENT_DEFAULTS["connection_targeting_mode"] == "suggest"
        assert _ENGAGEMENT_DEFAULTS["connection_target_authors"] == []
        assert _ENGAGEMENT_DEFAULTS["min_connection_icp_score"] == 55

    def test_authors_list_is_json_coerced_on_read(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        cur.fetchone.return_value = {"connection_target_authors": '["https://x/in/guru"]',
                                     "connection_targeting_mode": "auto_queue"}
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engagement_preferences
            prefs = get_engagement_preferences(1)
        assert prefs["connection_target_authors"] == ["https://x/in/guru"]

    @pytest.mark.parametrize("bad", ["", "on", None, "AUTO_QUEUE"])
    def test_invalid_mode_falls_back_to_suggest(self, bad, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_engagement_preferences
            update_engagement_preferences(1, {"connection_targeting_mode": bad})
        params = cur.execute.call_args[0][1]
        assert "suggest" in params

    @pytest.mark.parametrize("raw,expected", [(-5, 0), (140, 100), ("x", 55), (None, 55), (70, 70)])
    def test_min_icp_is_clamped(self, raw, expected, fake_cursor):
        # An out-of-range value must clamp, not roll back the whole single-row prefs upsert.
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import _ENGAGEMENT_COLS, update_engagement_preferences
            update_engagement_preferences(1, {"min_connection_icp_score": raw})
        params = cur.execute.call_args[0][1]
        assert params[_ENGAGEMENT_COLS.index("min_connection_icp_score") + 1] == expected

    def test_authors_persist_as_json(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import _ENGAGEMENT_COLS, update_engagement_preferences
            update_engagement_preferences(1, {"connection_target_authors": ["https://x/in/guru"]})
        params = cur.execute.call_args[0][1]
        assert params[_ENGAGEMENT_COLS.index("connection_target_authors") + 1] == '["https://x/in/guru"]'
