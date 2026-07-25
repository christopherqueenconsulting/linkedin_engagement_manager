"""Integration test for the early-adopter extended trial (#499).

Drives the real POST /api/trial/extend handler through the real db.extend_trial_for_user into a
stateful fake that models `users`, `feedback`, `early_adopter_slots` and `early_adopter_grants` —
including the row lock behind the conditional slot UPDATE — so the acceptance criteria (grant only
with a review, atomic slot counter, extension mirrored into Stripe on conversion) are proven
end-to-end without a live MySQL container.
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import patch

import mysql.connector
from mysql.connector import errorcode

pytestmark = pytest.mark.integration

_DB = "cqc_lem.utilities.db"
_MAIN = "cqc_lem.api.main"
_ENV = "cqc_lem.utilities.env_constants"

STARTED = datetime(2026, 7, 1, 12, 0, 0)


class _Store:
    """In-memory stand-in for the four tables the extension touches."""

    def __init__(self, user_ids, reviewers):
        self.users = {uid: {"subscription_status": "trial", "trial_started_at": STARTED,
                            "trial_ends_at": STARTED + timedelta(days=14)} for uid in user_ids}
        self.reviews = {uid: 100 + uid for uid in reviewers}
        self.slots = {"P0": 0, "P1": 0}
        self.grants: dict = {}
        # MySQL serializes the conditional slot UPDATE on the cohort row; this is that row lock.
        self.lock = threading.Lock()


class _Cursor:
    def __init__(self, store, conn):
        self.store = store
        self.conn = conn
        self._result = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.rowcount = 0
        if s.startswith("SELECT id FROM feedback"):
            user_id, source = params
            assert source == "review"
            fid = self.store.reviews.get(user_id)
            self._result = (fid,) if fid else None
        elif s.startswith("SELECT cohort, trial_days, trial_ends_at FROM early_adopter_grants"):
            self._result = self.store.grants.get(params[0])
        elif s.startswith("SELECT id, user_id, cohort"):
            self._result = self.store.grants.get(params[0])
        elif s.startswith("SELECT subscription_status, trial_started_at, trial_ends_at FROM users"):
            self._result = self.store.users.get(params[0])
        elif s.startswith("UPDATE early_adopter_slots"):
            cohort, capacity = params
            with self.store.lock:
                if self.store.slots.get(cohort, 0) < capacity:
                    self.store.slots[cohort] += 1
                    self.conn.claimed.append(cohort)
                    self.rowcount = 1
        elif s.startswith("INSERT INTO early_adopter_grants"):
            user_id, cohort, trial_days, feedback_id, ends = params
            if user_id in self.store.grants:
                err = mysql.connector.Error("duplicate")
                err.errno = errorcode.ER_DUP_ENTRY
                raise err
            self.conn.pending_grant = (user_id, {"id": len(self.store.grants) + 1,
                                                 "user_id": user_id, "cohort": cohort,
                                                 "trial_days": trial_days,
                                                 "feedback_id": feedback_id,
                                                 "trial_ends_at": ends})
            self.rowcount = 1
        elif s.startswith("UPDATE users SET trial_started_at"):
            started, ends, user_id = params
            self.conn.pending_user = (user_id, started, ends)
            self.rowcount = 1
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result

    def fetchall(self):
        return []

    def close(self):
        pass


class _Conn:
    def __init__(self, store):
        self.store = store
        self.claimed: list = []
        self.pending_grant = None
        self.pending_user = None

    def cursor(self, *a, **k):
        return _Cursor(self.store, self)

    def start_transaction(self):
        pass

    def commit(self):
        if self.pending_grant:
            user_id, row = self.pending_grant
            self.store.grants[user_id] = row
        if self.pending_user:
            user_id, started, ends = self.pending_user
            self.store.users[user_id].update({"trial_started_at": started, "trial_ends_at": ends,
                                              "subscription_status": "trial"})
        self._reset()

    def rollback(self):
        # A rolled-back transaction gives its claimed slot back — otherwise a losing racer would
        # permanently burn a seat in the cohort.
        with self.store.lock:
            for cohort in self.claimed:
                self.store.slots[cohort] -= 1
        self._reset()

    def _reset(self):
        self.claimed = []
        self.pending_grant = None
        self.pending_user = None

    def close(self):
        pass


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from cqc_lem.api.main import app
    return TestClient(app, raise_server_exceptions=False)


def _extend(client, store, user_id, p0=25, p1=100):
    with patch(f"{_DB}.get_db_connection", side_effect=lambda: _Conn(store)), \
            patch(f"{_MAIN}.get_session_user_id", return_value=user_id), \
            patch(f"{_ENV}.EARLY_ADOPTER_TRIAL_ENABLED", True), \
            patch(f"{_ENV}.EARLY_ADOPTER_TRIAL_DAYS", 60), \
            patch(f"{_ENV}.EARLY_ADOPTER_P0_SLOTS", p0), \
            patch(f"{_ENV}.EARLY_ADOPTER_P1_SLOTS", p1):
        return client.post("/api/trial/extend", json={"session_token": f"tok{user_id}"})


def test_review_gate_blocks_the_extension(client):
    store = _Store(user_ids=[1], reviewers=[])
    resp = _extend(client, store, 1)

    assert resp.status_code == 200
    detail = resp.json()["detail"]
    assert detail["granted"] is False and detail["reason"] == "review_required"
    assert store.grants == {} and store.slots == {"P0": 0, "P1": 0}
    # trial untouched: still the standard 14 days
    assert store.users[1]["trial_ends_at"] == STARTED + timedelta(days=14)


def test_review_unlocks_60_days_and_is_idempotent(client):
    store = _Store(user_ids=[1], reviewers=[1])

    detail = _extend(client, store, 1).json()["detail"]
    assert detail["granted"] is True
    assert detail["cohort"] == "P0" and detail["trial_days"] == 60
    assert store.users[1]["trial_ends_at"] == STARTED + timedelta(days=60)
    assert store.grants[1]["feedback_id"] == 101  # the review row that unlocked it
    assert store.slots["P0"] == 1

    again = _extend(client, store, 1).json()["detail"]
    assert again["granted"] is True and again["reason"] == "already_granted"
    assert store.slots["P0"] == 1  # no second slot consumed


def test_cohort_counter_is_atomic_under_concurrency(client):
    """8 simultaneous claims against a 2-seat cohort must produce exactly 2 grants."""
    user_ids = list(range(1, 9))
    store = _Store(user_ids=user_ids, reviewers=user_ids)
    results: list = []
    barrier = threading.Barrier(len(user_ids))

    from cqc_lem.api.main import trial_extend_endpoint, TrialExtendRequest

    def _claim(user_id: int):
        barrier.wait()
        with patch(f"{_MAIN}.get_session_user_id", return_value=user_id):
            results.append(trial_extend_endpoint(TrialExtendRequest(session_token=f"tok{user_id}")))

    with patch(f"{_DB}.get_db_connection", side_effect=lambda: _Conn(store)), \
            patch(f"{_ENV}.EARLY_ADOPTER_TRIAL_ENABLED", True), \
            patch(f"{_ENV}.EARLY_ADOPTER_TRIAL_DAYS", 60), \
            patch(f"{_ENV}.EARLY_ADOPTER_P0_SLOTS", 2), \
            patch(f"{_ENV}.EARLY_ADOPTER_P1_SLOTS", 0):
        threads = [threading.Thread(target=_claim, args=(uid,)) for uid in user_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    granted = [r for r in results if r.detail["granted"]]
    assert len(granted) == 2
    assert store.slots["P0"] == 2
    assert len(store.grants) == 2
    assert all(r.detail["reason"] == "slots_exhausted" for r in results if not r.detail["granted"])


def test_conversion_mirrors_the_remaining_trial_into_stripe(client):
    store = _Store(user_ids=[1], reviewers=[1])
    # Trial started today so the grant still has ~60 days left to carry into Stripe.
    store.users[1]["trial_started_at"] = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)
    store.users[1]["trial_ends_at"] = None
    assert _extend(client, store, 1).json()["detail"]["granted"] is True

    with patch(f"{_DB}.get_db_connection", side_effect=lambda: _Conn(store)), \
            patch(f"{_MAIN}.get_session_user_id", return_value=1), \
            patch(f"{_ENV}.EARLY_ADOPTER_COUPON_ID", "EARLYBIRD"), \
            patch(f"{_MAIN}.get_user_subscription_info",
                  return_value={"stripe_customer_id": "cus_1", "stripe_subscription_id": None,
                                "subscription_status": "trial"}), \
            patch("cqc_lem.utilities.stripe_util.create_checkout_session",
                  return_value="https://checkout.stripe.com/x") as create:
        resp = client.post("/api/billing/create-checkout-session",
                           json={"session_token": "tok1", "tier": "starter",
                                 "success_url": "https://e.com/s", "cancel_url": "https://e.com/c"})

    assert resp.status_code == 200
    assert create.call_args.kwargs["trial_period_days"] == 50  # 60-day grant, 10 days used
    assert create.call_args.kwargs["discounts"] == [{"coupon": "EARLYBIRD"}]
