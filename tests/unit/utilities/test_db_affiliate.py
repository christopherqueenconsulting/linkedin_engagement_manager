"""Issue #737 — the affiliate reward ledger: cap enforcement, one-bonus-per-user, and an opt-out
that returns the user to their standard trial without touching days they EARNED.

The trial-day writes are the money path, so these drive the REAL db functions against a stateful
fake cursor rather than asserting on SQL strings — the invariant under test is what ends up in
`users.trial_ends_at` and `affiliate_rewards`, not the query that got it there.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"
_AFF = "cqc_lem.utilities.marketing.affiliate"

# A trial that started yesterday and is still LIVE — the ordinary case. A lapsed trial is its own
# test below, because the extension deliberately runs from today rather than from the past end.
STARTED = datetime.now().replace(microsecond=0) - timedelta(days=1)


class _Store:
    def __init__(self, status="trial", trial_days=14, rewards=None, early_adopter_end=None):
        self.user = {"subscription_status": status, "trial_started_at": STARTED,
                     "trial_ends_at": STARTED + timedelta(days=trial_days)}
        self.rewards = list(rewards or [])   # dicts: {kind, trial_days, referral_id}
        self.early_adopter_end = early_adopter_end

    def total(self):
        return sum(int(r["trial_days"]) for r in self.rewards)

    def by_kinds(self, kinds):
        return sum(int(r["trial_days"]) for r in self.rewards if r["kind"] in kinds)


class _Cursor:
    def __init__(self, store):
        self.store = store
        self._result = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "SUM(trial_days),0) AS total FROM affiliate_rewards" in s:
            self._result = {"total": self.store.total()}
        elif "SUM(trial_days),0) AS net FROM affiliate_rewards" in s:
            self._result = {"net": self.store.by_kinds(("enrollment", "revoked"))}
        elif "SUM(trial_days),0) AS days FROM affiliate_rewards" in s:
            self._result = {"days": self.store.by_kinds(("referral",))}
        elif "FROM early_adopter_grants" in s:
            self._result = ({"trial_ends_at": self.store.early_adopter_end}
                            if self.store.early_adopter_end else None)
        elif "FROM users WHERE id=" in s:
            self._result = dict(self.store.user)
        elif s.startswith("INSERT INTO affiliate_rewards"):
            user_id, referral_id, kind, days, reason, ends = params
            if referral_id is not None and any(r.get("referral_id") == referral_id
                                               for r in self.store.rewards):
                raise _dup_entry()
            self.store.rewards.append({"kind": kind, "trial_days": days,
                                       "referral_id": referral_id})
        elif s.startswith("UPDATE users SET trial_started_at="):
            self.store.user["trial_started_at"] = params[0]
            self.store.user["trial_ends_at"] = params[1]
            self.store.user["subscription_status"] = "trial"
        elif s.startswith("UPDATE users SET trial_ends_at="):
            self.store.user["trial_ends_at"] = params[0]
        else:                                    # pragma: no cover - unexpected query
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result

    def close(self):
        pass


def _dup_entry():
    import mysql.connector
    from mysql.connector import errorcode
    err = mysql.connector.Error("duplicate")
    err.errno = errorcode.ER_DUP_ENTRY
    return err


def _connection(store):
    conn = MagicMock()
    conn.cursor.return_value = _Cursor(store)
    return conn


def _run(store, fn, *args, cap=90, **kwargs):
    with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=_connection(store)), \
         patch(f"{_AFF}.AFFILIATE_MAX_REWARD_DAYS", cap), \
         patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True), \
         patch("cqc_lem.utilities.env_constants.FREE_TRIAL_DAYS", 14):
        return fn(*args, **kwargs)


class TestGrantAffiliateTrialDays:
    def test_grants_and_extends_the_trial(self):
        from cqc_lem.utilities.db import grant_affiliate_trial_days
        store = _Store()
        out = _run(store, grant_affiliate_trial_days, 1, 14, "referral", referral_id=5)
        assert out["granted"] is True and out["reason"] == "granted" and out["days"] == 14
        assert store.user["trial_ends_at"] == STARTED + timedelta(days=28)
        assert store.rewards == [{"kind": "referral", "trial_days": 14, "referral_id": 5}]

    def test_cap_grants_only_the_remainder(self):
        from cqc_lem.utilities.db import grant_affiliate_trial_days
        store = _Store(rewards=[{"kind": "referral", "trial_days": 85, "referral_id": 1}])
        out = _run(store, grant_affiliate_trial_days, 1, 14, "referral", referral_id=2)
        assert out["granted"] is True and out["days"] == 5
        assert store.user["trial_ends_at"] == STARTED + timedelta(days=19)

    def test_at_the_cap_nothing_is_granted(self):
        from cqc_lem.utilities.db import grant_affiliate_trial_days
        store = _Store(rewards=[{"kind": "referral", "trial_days": 90, "referral_id": 1}])
        out = _run(store, grant_affiliate_trial_days, 1, 14, "referral", referral_id=2)
        assert out["granted"] is False and out["reason"] == "capped"
        assert store.user["trial_ends_at"] == STARTED + timedelta(days=14)   # untouched

    def test_second_enrollment_bonus_is_a_no_op(self):
        from cqc_lem.utilities.db import grant_affiliate_trial_days
        store = _Store(rewards=[{"kind": "enrollment", "trial_days": 7, "referral_id": None}])
        out = _run(store, grant_affiliate_trial_days, 1, 7, "enrollment")
        assert out["reason"] == "already_granted" and out["days"] == 0
        assert len(store.rewards) == 1

    def test_a_paying_subscriber_is_not_paid_in_trial_days(self):
        from cqc_lem.utilities.db import grant_affiliate_trial_days
        store = _Store(status="active")
        out = _run(store, grant_affiliate_trial_days, 1, 14, "referral", referral_id=3)
        assert out["granted"] is False and out["reason"] == "not_on_trial"
        assert store.rewards == []

    def test_duplicate_referral_reward_rolls_back(self):
        from cqc_lem.utilities.db import grant_affiliate_trial_days
        store = _Store(rewards=[{"kind": "referral", "trial_days": 14, "referral_id": 5}])
        out = _run(store, grant_affiliate_trial_days, 1, 14, "referral", referral_id=5)
        assert out["reason"] == "already_granted"
        assert len(store.rewards) == 1

    def test_a_lapsed_trial_is_extended_from_today_not_from_the_past(self):
        from cqc_lem.utilities.db import grant_affiliate_trial_days
        store = _Store()
        store.user["trial_ends_at"] = datetime(2020, 1, 1)
        out = _run(store, grant_affiliate_trial_days, 1, 14, "referral", referral_id=9)
        assert out["granted"] is True
        assert store.user["trial_ends_at"] > datetime.now()

    def test_zero_day_reward_is_refused(self):
        from cqc_lem.utilities.db import grant_affiliate_trial_days
        store = _Store()
        assert _run(store, grant_affiliate_trial_days, 1, 0, "enrollment")["granted"] is False


class TestRevokeAffiliateEnrollmentBonus:
    def test_opt_out_returns_the_user_to_the_standard_trial(self):
        from cqc_lem.utilities.db import revoke_affiliate_enrollment_bonus
        store = _Store(trial_days=21, rewards=[{"kind": "enrollment", "trial_days": 7,
                                                "referral_id": None}])
        out = _run(store, revoke_affiliate_enrollment_bonus, 1)
        assert out["revoked"] is True and out["days"] == 7
        assert store.user["trial_ends_at"] == STARTED + timedelta(days=14)

    def test_earned_referral_days_survive_an_opt_out(self):
        """The enrollment bonus is contingent on status; days the member EARNED are not."""
        from cqc_lem.utilities.db import revoke_affiliate_enrollment_bonus
        store = _Store(trial_days=49, rewards=[
            {"kind": "enrollment", "trial_days": 7, "referral_id": None},
            {"kind": "referral", "trial_days": 28, "referral_id": 1},
        ])
        out = _run(store, revoke_affiliate_enrollment_bonus, 1)
        assert out["revoked"] is True
        assert store.user["trial_ends_at"] == STARTED + timedelta(days=42)   # 14 standard + 28 earned

    def test_an_early_adopter_grant_is_never_shortened(self):
        from cqc_lem.utilities.db import revoke_affiliate_enrollment_bonus
        store = _Store(trial_days=67,
                       rewards=[{"kind": "enrollment", "trial_days": 7, "referral_id": None}],
                       early_adopter_end=STARTED + timedelta(days=60))
        _run(store, revoke_affiliate_enrollment_bonus, 1)
        assert store.user["trial_ends_at"] == STARTED + timedelta(days=60)

    def test_nothing_standing_is_a_no_op(self):
        from cqc_lem.utilities.db import revoke_affiliate_enrollment_bonus
        store = _Store(rewards=[
            {"kind": "enrollment", "trial_days": 7, "referral_id": None},
            {"kind": "revoked", "trial_days": -7, "referral_id": None},
        ])
        out = _run(store, revoke_affiliate_enrollment_bonus, 1)
        assert out["revoked"] is False and out["reason"] == "nothing_to_revoke"

    def test_opt_out_then_back_in_cannot_mint_days(self):
        """The cap is measured on the ledger SUM, revocations included — so a round trip is free,
        not profitable.
        """
        from cqc_lem.utilities.db import grant_affiliate_trial_days, revoke_affiliate_enrollment_bonus
        store = _Store(rewards=[{"kind": "enrollment", "trial_days": 7, "referral_id": None}])
        _run(store, revoke_affiliate_enrollment_bonus, 1)
        _run(store, grant_affiliate_trial_days, 1, 7, "enrollment")
        assert store.total() == 7
