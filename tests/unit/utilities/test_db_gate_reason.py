"""Unit tests for the DB side of the pending-reason surface (issue #421): the gate_reason
round-trip, the self-exclusion the re-score path needs from the post history, and the clamping of
the user-tunable gate thresholds in the single-row engagement upsert.
"""

import json
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


_FINDING = {"gate": "authenticity", "label": "Authenticity", "score": 41, "threshold": 60,
            "demoted": True, "explanation": "scored 41", "remediation": "add specifics",
            "details": []}


class TestUpdateDbPostGateReason:
    def test_stores_the_findings_as_json(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_gate_reason
            assert update_db_post_gate_reason(7, [_FINDING]) is True
        sql, params = cur.execute.call_args[0]
        assert "UPDATE posts SET gate_reason" in sql
        assert json.loads(params[0]) == [_FINDING] and params[1] == 7

    @pytest.mark.parametrize("findings", [None, []])
    def test_no_findings_clears_the_column(self, findings, fake_cursor):
        # A post that passes on re-score must stop showing a stale reason.
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_gate_reason
            assert update_db_post_gate_reason(7, findings) is True
        assert cur.execute.call_args[0][1] == (None, 7)

    def test_false_on_db_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_db_post_gate_reason
            assert update_db_post_gate_reason(7, [_FINDING]) is False


class TestGetPostGateReason:
    def test_parses_the_stored_json(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=(json.dumps([_FINDING]),))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_gate_reason
            assert get_post_gate_reason(7) == [_FINDING]

    @pytest.mark.parametrize("row", [(None,), None])
    def test_empty_when_never_evaluated(self, row, fake_cursor):
        conn, _ = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_gate_reason
            assert get_post_gate_reason(7) == []

    def test_empty_on_db_error(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_post_gate_reason
            assert get_post_gate_reason(7) == []


class TestPostsQueryExposesTheVerdict:
    def test_get_posts_selects_the_quality_columns(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"total": 0}, fetch_all=[])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_posts
            get_posts(1)
        sql = cur.execute.call_args_list[-1][0][0]
        assert "authenticity_score" in sql and "gate_reason" in sql


class TestRecentPostTextsExclusion:
    def test_excludes_the_post_being_rescored(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[("older post",)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_texts
            assert get_recent_post_texts(1, limit=5, exclude_post_id=42) == ["older post"]
        sql, params = cur.execute.call_args[0]
        assert "id <> %s" in sql
        assert params == (1, 42, 5)

    def test_no_exclusion_keeps_the_original_query(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_post_texts
            get_recent_post_texts(1, limit=5)
        sql, params = cur.execute.call_args[0]
        assert "id <> %s" not in sql
        assert params == (1, 5)


class TestEngagementThresholdPersistence:
    def _saved(self, fake_cursor, prefs):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn), \
             patch("cqc_lem.platform.db.repositories.users.max_catchup_touches_allowed", return_value=5):
            from cqc_lem.utilities.db import _ENGAGEMENT_COLS, update_engagement_preferences
            assert update_engagement_preferences(1, prefs) is True
        values = cur.execute.call_args[0][1]
        return dict(zip(_ENGAGEMENT_COLS, values[1:]))

    def test_thresholds_persist(self, fake_cursor):
        saved = self._saved(fake_cursor, {"authenticity_score_min": 80, "post_similarity_max_pct": 40})
        assert saved["authenticity_score_min"] == 80
        assert saved["post_similarity_max_pct"] == 40

    def test_unset_thresholds_stay_null(self, fake_cursor):
        # NULL means "follow the deploy default" — the gates must behave exactly as before.
        saved = self._saved(fake_cursor, {})
        assert saved["authenticity_score_min"] is None
        assert saved["post_similarity_max_pct"] is None

    def test_out_of_range_thresholds_are_clamped_not_rejected(self, fake_cursor):
        # The whole engagement upsert is ONE row (the V52 lesson): a bad value must not roll back
        # every other section.
        saved = self._saved(fake_cursor, {"authenticity_score_min": 999, "post_similarity_max_pct": 0})
        assert saved["authenticity_score_min"] == 100
        assert saved["post_similarity_max_pct"] == 10

    def test_unparseable_threshold_falls_back_to_the_default(self, fake_cursor):
        saved = self._saved(fake_cursor, {"authenticity_score_min": "loose"})
        assert saved["authenticity_score_min"] is None
