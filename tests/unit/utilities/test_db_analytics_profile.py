"""get_user_analytics_profile — the person facts the SPA identifies with (issue #646)."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


def _conn(fetch_one=None):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetch_one
    conn.cursor.return_value = cur
    return conn, cur


class TestGetUserAnalyticsProfile:
    def test_returns_plan_timezone_signup_and_the_survey_targeting_facts(self):
        row = {
            "subscription_tier": "premium",
            "subscription_status": "active",
            "timezone": "America/Chicago",
            "created_at": datetime(2026, 1, 2, 3, 4, 5),
            "onboarding_completed_at": datetime(2026, 2, 3, 4, 5, 6),
            "posts_approved": 12,
        }
        conn, cur = _conn(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_analytics_profile
            assert get_user_analytics_profile(7) == row
        assert cur.execute.call_args[0][1][-1] == 7

    def test_the_approval_tally_survives_a_post_moving_on(self):
        # An approved post becomes scheduled and then posted; counting status='approved' alone would
        # reset the CSAT survey's gate the moment automation ran.
        conn, cur = _conn(fetch_one={})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_analytics_profile
            get_user_analytics_profile(1)
        statuses = set(cur.execute.call_args[0][1][:-1])
        assert statuses == {"approved", "scheduled", "posted"}

    def test_onboarding_completion_comes_from_activation_not_signup(self):
        conn, cur = _conn(fetch_one={})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_analytics_profile
            get_user_analytics_profile(1)
        sql = " ".join(cur.execute.call_args[0][0].split()).lower()
        assert "o.activated_at as onboarding_completed_at" in sql
        assert "left join onboarding_state" in sql

    def test_selects_no_credentials(self):
        conn, cur = _conn(fetch_one={})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_analytics_profile
            get_user_analytics_profile(1)
        sql = cur.execute.call_args[0][0].lower()
        for secret in ("password", "access_token", "refresh_token", "linkedin"):
            assert secret not in sql

    def test_unknown_user_returns_empty_dict(self):
        conn, _ = _conn(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_analytics_profile
            assert get_user_analytics_profile(999) == {}

    def test_db_error_returns_empty_dict(self):
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_analytics_profile
            assert get_user_analytics_profile(1) == {}
