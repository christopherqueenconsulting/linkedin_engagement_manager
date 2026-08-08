"""Unit tests for the early-adopter extended-trial DB layer (issue #499)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"
_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"
_BILLING = "cqc_lem.platform.db.repositories.billing"
_FEEDBACK = "cqc_lem.platform.db.repositories.feedback"
_ENV = "cqc_lem.utilities.env_constants"

STARTED = datetime(2026, 7, 1, 12, 0, 0)


class _Cursor:
    """Stateful stand-in for the `early_adopter_*` + `users` rows the extension touches.

    The slot counter is modeled faithfully — `used < capacity` is evaluated inside the UPDATE and
    surfaced as rowcount — so the exhaustion tests exercise the same conditional the DB does.
    """

    def __init__(self, *, grant=None, user=None, used=None):
        self.grant = grant
        self.user = user
        self.used = dict(used or {"P0": 0, "P1": 0})
        self.inserted_grants: list = []
        self.user_updates: list = []
        self.rowcount = 0
        self._result = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.rowcount = 0
        if s.startswith("SELECT cohort, trial_days, trial_ends_at FROM early_adopter_grants"):
            self._result = self.grant
        elif s.startswith("SELECT subscription_status, trial_started_at, trial_ends_at FROM users"):
            self._result = self.user
        elif s.startswith("UPDATE early_adopter_slots"):
            cohort, capacity = params
            if self.used.get(cohort, 0) < capacity:
                self.used[cohort] = self.used.get(cohort, 0) + 1
                self.rowcount = 1
        elif s.startswith("INSERT INTO early_adopter_grants"):
            self.inserted_grants.append(params)
            self.rowcount = 1
        elif s.startswith("UPDATE users SET trial_started_at"):
            self.user_updates.append((s, params))
            self.rowcount = 1
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result

    def close(self):
        pass


class _Conn:
    def __init__(self, cursor):
        self._cur = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self, *a, **k):
        return self._cur

    def start_transaction(self):
        pass

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _user(status="trial", started=STARTED, ends=None):
    if ends is None and started is not None:
        ends = started + timedelta(days=14)
    return {"subscription_status": status, "trial_started_at": started, "trial_ends_at": ends}


def _extend(cursor, user_id=5, feedback_id=99, p0=25, p1=100, days=60):
    conn = _Conn(cursor)
    with patch(f"{_GET_CONN}", return_value=conn), \
            patch(f"{_ENV}.EARLY_ADOPTER_P0_SLOTS", p0), \
            patch(f"{_ENV}.EARLY_ADOPTER_P1_SLOTS", p1), \
            patch(f"{_ENV}.EARLY_ADOPTER_TRIAL_DAYS", days):
        from cqc_lem.utilities.db import extend_trial_for_user
        return extend_trial_for_user(user_id, feedback_id=feedback_id), conn


class TestGetLatestReviewFeedbackId:
    def test_returns_most_recent_review_id(self):
        cur = MagicMock()
        cur.fetchone.return_value = (17,)
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_latest_review_feedback_id
            assert get_latest_review_feedback_id(5) == 17
        sql, params = cur.execute.call_args[0]
        assert "source=%s" in sql and params == (5, "review")

    def test_no_review_returns_none(self):
        cur = MagicMock()
        cur.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_latest_review_feedback_id
            assert get_latest_review_feedback_id(5) is None

    def test_db_error_returns_none_and_closes(self):
        import mysql.connector
        cur = MagicMock()
        cur.execute.side_effect = mysql.connector.Error("boom")
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error"):
            from cqc_lem.utilities.db import get_latest_review_feedback_id
            assert get_latest_review_feedback_id(5) is None
        cur.close.assert_called_once()
        conn.close.assert_called_once()


class TestExtendTrialForUser:
    def test_grants_p0_slot_and_extends_to_started_plus_days(self):
        cur = _Cursor(user=_user())
        result, conn = _extend(cur)

        assert result["granted"] is True
        assert result["reason"] == "granted"
        assert result["cohort"] == "P0"
        assert result["trial_days"] == 60
        assert result["trial_ends_at"] == STARTED + timedelta(days=60)
        assert cur.used["P0"] == 1 and cur.used["P1"] == 0
        # grant row records who unlocked it and with which review
        user_id, cohort, trial_days, feedback_id, ends = cur.inserted_grants[0]
        assert (user_id, cohort, trial_days, feedback_id) == (5, "P0", 60, 99)
        assert ends == STARTED + timedelta(days=60)
        assert conn.committed is True

    def test_user_row_is_put_back_on_trial(self):
        cur = _Cursor(user=_user(status="inactive"))
        result, _ = _extend(cur)
        assert result["granted"] is True
        sql, params = cur.user_updates[0]
        assert "subscription_status='trial'" in sql
        assert params == (STARTED, STARTED + timedelta(days=60), 5)

    def test_p0_full_falls_through_to_p1(self):
        cur = _Cursor(user=_user(), used={"P0": 25, "P1": 0})
        result, _ = _extend(cur)
        assert result["cohort"] == "P1"
        assert cur.used["P1"] == 1

    def test_all_slots_exhausted_falls_back_to_standard_trial(self):
        cur = _Cursor(user=_user(), used={"P0": 25, "P1": 100})
        result, conn = _extend(cur)
        assert result["granted"] is False
        assert result["reason"] == "slots_exhausted"
        assert result["trial_days"] == 14  # FREE_TRIAL_DAYS default
        assert cur.inserted_grants == [] and cur.user_updates == []
        assert conn.rolled_back is True and conn.committed is False

    def test_zero_capacity_cohort_is_skipped(self):
        cur = _Cursor(user=_user())
        result, _ = _extend(cur, p0=0)
        assert result["cohort"] == "P1"
        assert cur.used["P0"] == 0

    def test_existing_grant_is_idempotent_and_consumes_no_slot(self):
        cur = _Cursor(grant={"cohort": "P0", "trial_days": 60,
                             "trial_ends_at": STARTED + timedelta(days=60)},
                      user=_user())
        result, conn = _extend(cur)
        assert result == {"granted": True, "reason": "already_granted", "cohort": "P0",
                          "trial_days": 60, "trial_ends_at": STARTED + timedelta(days=60)}
        assert cur.used == {"P0": 0, "P1": 0}
        assert conn.committed is False

    def test_unknown_user_is_rejected(self):
        cur = _Cursor(user=None)
        result, conn = _extend(cur)
        assert (result["granted"], result["reason"]) == (False, "user_not_found")
        assert cur.used["P0"] == 0 and conn.rolled_back is True

    @pytest.mark.parametrize("status", ["active", "past_due", "cancelled"])
    def test_non_trial_subscription_is_rejected(self, status):
        cur = _Cursor(user=_user(status=status))
        result, _ = _extend(cur)
        assert (result["granted"], result["reason"]) == (False, "not_on_trial")
        assert cur.used["P0"] == 0

    def test_never_shortens_an_already_longer_trial(self):
        longer = STARTED + timedelta(days=90)
        cur = _Cursor(user=_user(ends=longer))
        result, _ = _extend(cur)
        assert result["trial_ends_at"] == longer

    def test_missing_trial_start_uses_now(self):
        cur = _Cursor(user=_user(started=None, ends=None))
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        result, _ = _extend(cur)
        assert result["granted"] is True
        assert result["trial_ends_at"] >= before + timedelta(days=59)

    def test_aware_timestamps_are_compared_without_error(self):
        from datetime import timezone as _tz
        cur = _Cursor(user={"subscription_status": "trial",
                            "trial_started_at": STARTED.replace(tzinfo=_tz.utc),
                            "trial_ends_at": (STARTED + timedelta(days=120)).replace(tzinfo=_tz.utc)})
        result, _ = _extend(cur)
        assert result["trial_ends_at"] == STARTED + timedelta(days=120)

    def test_duplicate_grant_race_returns_the_winning_grant(self):
        import mysql.connector
        from mysql.connector import errorcode

        err = mysql.connector.Error("dup")
        err.errno = errorcode.ER_DUP_ENTRY
        cur = _Cursor(user=_user())
        original_execute = cur.execute

        def _boom(sql, params=None):
            if " ".join(sql.split()).startswith("INSERT INTO early_adopter_grants"):
                raise err
            original_execute(sql, params)

        cur.execute = _boom
        winner = {"cohort": "P0", "trial_days": 60, "trial_ends_at": STARTED + timedelta(days=60)}
        conn = _Conn(cur)
        with patch(f"{_GET_CONN}", return_value=conn), \
                patch(f"{_DB}.get_early_adopter_grant", return_value=winner):
            from cqc_lem.utilities.db import extend_trial_for_user
            result = extend_trial_for_user(5, feedback_id=1)
        assert (result["granted"], result["reason"]) == (True, "already_granted")
        assert conn.rolled_back is True

    def test_db_error_rolls_back_and_reports_error(self):
        import mysql.connector
        cur = _Cursor(user=_user())
        cur.execute = MagicMock(side_effect=mysql.connector.Error("boom"))
        conn = _Conn(cur)
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_DB}.log_error"):
            from cqc_lem.utilities.db import extend_trial_for_user
            result = extend_trial_for_user(5)
        assert (result["granted"], result["reason"]) == (False, "error")
        assert conn.rolled_back is True and conn.committed is False


class TestGrantAndSlotReads:
    def test_get_grant_returns_row(self):
        cur = MagicMock()
        cur.fetchone.return_value = {"user_id": 5, "cohort": "P0"}
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_early_adopter_grant
            assert get_early_adopter_grant(5)["cohort"] == "P0"

    def test_get_grant_db_error_returns_none(self):
        import mysql.connector
        cur = MagicMock()
        cur.execute.side_effect = mysql.connector.Error("boom")
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_BILLING}.log_error"):
            from cqc_lem.utilities.db import get_early_adopter_grant
            assert get_early_adopter_grant(5) is None

    def test_slot_usage_maps_cohort_to_used(self):
        cur = MagicMock()
        cur.fetchall.return_value = [("P0", 3), ("P1", 0)]
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_early_adopter_slot_usage
            assert get_early_adopter_slot_usage() == {"P0": 3, "P1": 0}

    def test_slot_usage_db_error_returns_empty(self):
        import mysql.connector
        cur = MagicMock()
        cur.execute.side_effect = mysql.connector.Error("boom")
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_BILLING}.log_error"):
            from cqc_lem.utilities.db import get_early_adopter_slot_usage
            assert get_early_adopter_slot_usage() == {}
