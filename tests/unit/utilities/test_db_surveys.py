"""Unit tests for the survey DB helpers (issue #501)."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


def _erroring_conn():
    conn = MagicMock()
    cur = MagicMock()
    cur.execute.side_effect = mysql.connector.Error("boom")
    conn.cursor.return_value = cur
    return conn, cur


class TestLatestFeedbackAt:
    def test_returns_the_most_recent_answer_for_that_source(self, fake_cursor):
        when = datetime(2026, 7, 20, 9, 0)
        conn, cur = fake_cursor(fetch_one=(when,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import FeedbackSource, get_latest_feedback_at
            assert get_latest_feedback_at(4, FeedbackSource.NPS) == when
        sql, params = cur.execute.call_args[0]
        assert "MAX(created_at)" in sql
        assert params == (4, "nps")

    def test_none_when_never_answered_or_on_error(self, fake_cursor):
        conn, _cur = fake_cursor(fetch_one=(None,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import FeedbackSource, get_latest_feedback_at
            assert get_latest_feedback_at(4, FeedbackSource.REVIEW) is None

        conn, _cur = _erroring_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import FeedbackSource, get_latest_feedback_at
            assert get_latest_feedback_at(4, FeedbackSource.REVIEW) is None


class TestReviewGate:
    def test_true_only_when_a_review_row_exists(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_review_feedback
            assert has_review_feedback(4) is True
        assert cur.execute.call_args[0][1] == (4, "review")

        conn, _cur = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_review_feedback
            assert has_review_feedback(4) is False

    def test_fails_closed_on_a_db_error(self):
        conn, _cur = _erroring_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_review_feedback
            assert has_review_feedback(4) is False


class TestSurveyPromptLedger:
    def test_reads_key_to_sent_at(self, fake_cursor):
        sent = datetime(2026, 7, 24, 9, 45)
        conn, _cur = fake_cursor(fetch_all=[("nps_day3", sent)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_survey_prompts_sent
            assert get_survey_prompts_sent(4) == {"nps_day3": sent}

    def test_empty_dict_on_error(self):
        conn, _cur = _erroring_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_survey_prompts_sent
            assert get_survey_prompts_sent(4) == {}

    def test_record_is_one_shot_and_truncates_the_key(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_survey_prompt
            assert record_survey_prompt(4, "x" * 40) is True
        sql, params = cur.execute.call_args[0]
        assert "INSERT IGNORE INTO survey_prompts" in sql
        assert params == (4, "x" * 32)
        conn.commit.assert_called_once()

        conn, _cur = fake_cursor(rowcount=0)  # already asked
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_survey_prompt
            assert record_survey_prompt(4, "nps_day3") is False

    def test_record_survives_a_db_error(self):
        conn, _cur = _erroring_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_survey_prompt
            assert record_survey_prompt(4, "nps_day3") is False


class TestSurveyCandidates:
    def test_covers_paying_and_trialing_users_including_activated_ones(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[(1,), (2,)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_survey_candidate_user_ids
            assert get_survey_candidate_user_ids() == [1, 2]
        sql = " ".join(cur.execute.call_args[0][0].split())
        assert "subscription_status = 'active'" in sql
        assert "trial_ends_at > NOW()" in sql
        assert "activated_at" not in sql

    def test_empty_on_error(self):
        conn, _cur = _erroring_conn()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_survey_candidate_user_ids
            assert get_survey_candidate_user_ids() == []
