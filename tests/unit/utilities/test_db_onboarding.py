"""Unit tests for the onboarding/activation DB helpers (issue #500)."""

from datetime import datetime
from unittest.mock import patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


class TestOnboardingState:
    def test_ensure_seeds_from_the_trial_start(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ensure_onboarding_state
            assert ensure_onboarding_state(4) is True
        sql, params = cur.execute.call_args[0]
        assert "INSERT IGNORE INTO onboarding_state" in sql
        assert "COALESCE(trial_started_at, NOW())" in sql
        assert params == (4,)
        conn.commit.assert_called_once()

    def test_get_state_returns_row_and_empty_dict_when_missing(self, fake_cursor):
        row = {"user_id": 4, "started_at": datetime(2026, 7, 20)}
        conn, cur = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_onboarding_state
            assert get_onboarding_state(4) == row
        assert "activated_at" in cur.execute.call_args[0][0]

        conn, _cur = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_onboarding_state
            assert get_onboarding_state(4) == {}

    def test_mark_step_only_writes_the_first_completion(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import OnboardingStep, mark_onboarding_step
            assert mark_onboarding_step(4, OnboardingStep.VOICE_SET) is True
        sql = cur.execute.call_args[0][0]
        assert "SET voice_set_at = NOW()" in sql
        assert "voice_set_at IS NULL" in sql  # idempotent — a second call updates nothing

        conn, _cur = fake_cursor(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import OnboardingStep, mark_onboarding_step
            assert mark_onboarding_step(4, OnboardingStep.VOICE_SET) is False

    def test_state_errors_are_swallowed(self, fake_cursor):
        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import (
                OnboardingStep,
                ensure_onboarding_state,
                get_onboarding_state,
                mark_onboarding_step,
            )
            assert ensure_onboarding_state(4) is False
            assert get_onboarding_state(4) == {}
            assert mark_onboarding_step(4, OnboardingStep.ACTIVATED) is False


class TestOnboardingNudges:
    def test_sent_ledger_maps_key_to_timestamp(self, fake_cursor):
        sent_at = datetime(2026, 7, 24, 9, 0)
        conn, _cur = fake_cursor(fetch_all=[("connect_linkedin", sent_at)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_onboarding_nudges_sent
            assert get_onboarding_nudges_sent(4) == {"connect_linkedin": sent_at}

    def test_record_is_one_shot_and_truncates_the_key(self, fake_cursor):
        conn, cur = fake_cursor(rowcount=1)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_onboarding_nudge
            assert record_onboarding_nudge(4, "k" * 40) is True
        sql, params = cur.execute.call_args[0]
        assert "INSERT IGNORE INTO onboarding_nudges" in sql
        assert len(params[1]) == 32  # onboarding_nudges.nudge_key VARCHAR(32)

        conn, _cur = fake_cursor(rowcount=0)  # already sent → PK swallows it
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_onboarding_nudge
            assert record_onboarding_nudge(4, "connect_linkedin") is False

    def test_nudge_errors_are_swallowed(self, fake_cursor):
        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_onboarding_nudges_sent, record_onboarding_nudge
            assert get_onboarding_nudges_sent(4) == {}
            assert record_onboarding_nudge(4, "connect_linkedin") is False


class TestOnboardingCandidates:
    def test_selects_paying_or_trialing_users_who_have_not_activated(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[(1,), (5,)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_onboarding_candidate_user_ids
            assert get_onboarding_candidate_user_ids() == [1, 5]
        sql = " ".join(cur.execute.call_args[0][0].split())
        assert "o.activated_at IS NULL" in sql
        assert "subscription_status = 'trial'" in sql
        # Must NOT require a live LinkedIn connection — that's the step most stalled users lack.
        assert "linkedin_connection_status" not in sql

    def test_error_returns_empty_list(self, fake_cursor):
        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_onboarding_candidate_user_ids
            assert get_onboarding_candidate_user_ids() == []


class TestStepEvidenceQueries:
    def test_has_engagement_preferences(self, fake_cursor):
        conn, _cur = fake_cursor(fetch_one=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_engagement_preferences
            assert has_engagement_preferences(4) is True

        conn, _cur = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_engagement_preferences
            assert has_engagement_preferences(4) is False

    def test_has_post_with_status_expands_placeholders(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import PostStatus, has_post_with_status
            assert has_post_with_status(4, (PostStatus.APPROVED, PostStatus.POSTED)) is True
        sql, params = cur.execute.call_args[0]
        assert "status IN (%s, %s)" in sql
        assert params == (4, "approved", "posted")

    def test_has_post_with_status_short_circuits_on_no_statuses(self):
        with patch("cqc_lem.platform.db.connection.get_db_connection") as get_conn:
            from cqc_lem.utilities.db import has_post_with_status
            assert has_post_with_status(4, ()) is False
        get_conn.assert_not_called()

    def test_has_automated_engagement_counts_only_successful_actions(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import has_automated_engagement
            assert has_automated_engagement(4) is True
        sql, params = cur.execute.call_args[0]
        assert "result = %s" in sql
        assert params[1] == "success"
        assert set(params[2:]) == {"comment", "reply", "dm", "followup"}

    def test_evidence_errors_are_swallowed(self, fake_cursor):
        conn, _cur = fake_cursor(execute_error=mysql.connector.Error("boom"))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import (
                PostStatus,
                has_automated_engagement,
                has_engagement_preferences,
                has_post_with_status,
            )
            assert has_engagement_preferences(4) is False
            assert has_post_with_status(4, (PostStatus.POSTED,)) is False
            assert has_automated_engagement(4) is False
