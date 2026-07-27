"""Integration test for the affiliate/ambassador program endpoints (issue #737).

Drives the real `GET /api/user/affiliate`, `POST /api/user/affiliate/status`,
`POST /api/user/affiliate/promo-consent` and `POST /api/user/affiliate/notice` handlers through the
real `db.py` SQL and the real policy core in `utilities/marketing/affiliate.py`, against a stateful
in-memory stand-in for the four tables involved. What that buys over mocking `db.py`: the acceptance
criteria are all about STATE — (A) and (B) moving independently, an opt-out that lands on the trial
end date immediately, a self-referral that pays nobody — and none of those are observable if the
storage layer is a mock that returns whatever the test says.
"""
from datetime import datetime, timedelta

import pytest
from unittest.mock import patch

import mysql.connector
from mysql.connector import errorcode

pytestmark = pytest.mark.integration

_M = "cqc_lem.api.main"
_AFF = "cqc_lem.utilities.marketing.affiliate"
_ATTR = "cqc_lem.utilities.marketing.attribution"

USER = 42
STARTED = datetime.now().replace(microsecond=0) - timedelta(days=1)
DISCLOSURE = "#ad — I get free trial time for referrals."


class _Store:
    def __init__(self):
        self.users = {
            USER: {"id": USER, "email": "user@example.test", "subscription_status": "trial",
                   "trial_started_at": STARTED, "trial_ends_at": STARTED + timedelta(days=14)},
            7: {"id": 7, "email": "friend@example.test", "subscription_status": "trial",
                "trial_started_at": STARTED, "trial_ends_at": STARTED + timedelta(days=14)},
        }
        self.enrollments: dict = {}
        self.referrals: list = []
        self.rewards: list = []
        self.company_pages: dict = {}

    def trial_days(self, user_id=USER) -> int:
        return round((self.users[user_id]["trial_ends_at"] - STARTED).total_seconds() / 86400)


class _Cursor:
    def __init__(self, store, dictionary=False):
        self.store = store
        self.dictionary = dictionary
        self._rows: list = []
        self.rowcount = 0
        self.lastrowid = None

    # -- helpers ------------------------------------------------------------------------------
    def _set(self, rows):
        self._rows = list(rows)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        self.rowcount = 0

        if s.startswith("SELECT user_id, status, referral_code"):
            row = self.store.enrollments.get(params[0])
            self._set([dict(row)] if row else [])
        elif s.startswith("INSERT IGNORE INTO affiliate_enrollments"):
            user_id, status, code, enrolled_at = params
            self.store.enrollments.setdefault(user_id, {
                "user_id": user_id, "status": status, "referral_code": code,
                "enrolled_at": enrolled_at, "opted_out_at": None, "notice_seen_at": None,
                "promo_content_opt_in": 0, "promo_consent_at": None,
                "promo_consent_version": None})
        elif s.startswith("UPDATE affiliate_enrollments SET status="):
            status, enrolled, now_enrolled, enrolled2, now_out, user_id = params
            row = self.store.enrollments[user_id]
            row["status"] = status
            if enrolled:
                row["enrolled_at"] = row.get("enrolled_at") or now_enrolled
            else:
                row["opted_out_at"] = now_out
        elif s.startswith("UPDATE affiliate_enrollments SET promo_content_opt_in="):
            opt_in, consent_at, version, user_id = params
            row = self.store.enrollments[user_id]
            row.update({"promo_content_opt_in": opt_in, "promo_consent_at": consent_at,
                        "promo_consent_version": version})
        elif s.startswith("UPDATE affiliate_enrollments SET notice_seen_at="):
            now, user_id = params
            row = self.store.enrollments[user_id]
            row["notice_seen_at"] = row.get("notice_seen_at") or now
        elif "kind, COALESCE(SUM(trial_days),0) AS days FROM affiliate_rewards" in s:
            totals: dict = {}
            for r in self.store.rewards:
                if r["user_id"] == params[0]:
                    totals[r["kind"]] = totals.get(r["kind"], 0) + r["trial_days"]
            self._set([{"kind": k, "days": v} for k, v in totals.items()])
        elif "COUNT(*) AS n FROM affiliate_referrals" in s:
            counts: dict = {}
            for r in self.store.referrals:
                if r["referrer_user_id"] == params[0]:
                    counts[r["status"]] = counts.get(r["status"], 0) + 1
            self._set([{"status": k, "n": v} for k, v in counts.items()])
        elif s.startswith("SELECT id, referrer_user_id, referred_user_id"):
            self._set([dict(r) for r in self.store.referrals
                       if r["referred_user_id"] == params[0]])
        elif s.startswith("INSERT INTO affiliate_referrals"):
            referrer, referred, code, status, reason = params
            if any(r["referred_user_id"] == referred for r in self.store.referrals):
                raise _dup()
            self.store.referrals.append({
                "id": len(self.store.referrals) + 1, "referrer_user_id": referrer,
                "referred_user_id": referred, "referral_code": code, "status": status,
                "reject_reason": reason, "created_at": datetime.now(), "converted_at": None})
            self.lastrowid = len(self.store.referrals)
        elif s.startswith("UPDATE affiliate_referrals SET status="):
            status, converted_at, referred, want = params
            for r in self.store.referrals:
                if r["referred_user_id"] == referred and r["status"] == want:
                    r.update({"status": status, "converted_at": converted_at})
                    self.rowcount = 1
        elif "SUM(trial_days),0) AS total FROM affiliate_rewards" in s:
            self._set([{"total": sum(r["trial_days"] for r in self.store.rewards
                                     if r["user_id"] == params[0])}])
        elif "SUM(trial_days),0) AS net FROM affiliate_rewards" in s:
            self._set([{"net": sum(r["trial_days"] for r in self.store.rewards
                                   if r["user_id"] == params[0]
                                   and r["kind"] in ("enrollment", "revoked"))}])
        elif "SUM(trial_days),0) AS days FROM affiliate_rewards" in s:
            self._set([{"days": sum(r["trial_days"] for r in self.store.rewards
                                    if r["user_id"] == params[0] and r["kind"] == "referral")}])
        elif s.startswith("INSERT INTO affiliate_rewards"):
            user_id, referral_id, kind, days, reason, ends = params
            if referral_id is not None and any(r["referral_id"] == referral_id
                                               for r in self.store.rewards):
                raise _dup()
            self.store.rewards.append({"user_id": user_id, "referral_id": referral_id,
                                       "kind": kind, "trial_days": days, "reason": reason})
        elif "FROM early_adopter_grants" in s:
            self._set([])
        elif s.startswith("SELECT subscription_status, trial_started_at, trial_ends_at FROM users"):
            self._set([dict(self.store.users[params[0]])])
        elif s.startswith("SELECT trial_started_at, trial_ends_at FROM users"):
            self._set([dict(self.store.users[params[0]])])
        elif s.startswith("UPDATE users SET trial_started_at="):
            self.store.users[params[2]].update({"trial_started_at": params[0],
                                                "trial_ends_at": params[1]})
        elif s.startswith("UPDATE users SET trial_ends_at="):
            self.store.users[params[1]]["trial_ends_at"] = params[0]
        elif "FROM users WHERE id" in s and "email" in s:
            user = self.store.users.get(params[0])
            self._set([{"email": user["email"]}] if user else [])
        elif "company_linked_in_url" in s:
            self._set([{"company_linked_in_url": self.store.company_pages.get(params[0])}])
        else:   # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        if not self._rows:
            return None
        row = self._rows[0]
        return row if self.dictionary else tuple(row.values())

    def fetchall(self):
        return [r if self.dictionary else tuple(r.values()) for r in self._rows]

    def close(self):
        pass


def _dup():
    err = mysql.connector.Error("duplicate")
    err.errno = errorcode.ER_DUP_ENTRY
    return err


class _Connection:
    def __init__(self, store):
        self.store = store

    def cursor(self, dictionary=False, **kw):
        return _Cursor(self.store, dictionary=dictionary)

    def start_transaction(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def store():
    return _Store()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from cqc_lem.api.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def env(store):
    with patch(f"{_M}.get_session_user_id", return_value=USER), \
         patch("cqc_lem.utilities.db.get_db_connection", return_value=_Connection(store)), \
         patch("cqc_lem.utilities.observability.posthog"), \
         patch(f"{_ATTR}.PUBLIC_BASE_URL", "https://app.lem.test"), \
         patch(f"{_ATTR}.BRAND_SIGNUP_URL", "https://app.lem.test/signup"), \
         patch(f"{_ATTR}.MARKETING_OWNED_DOMAINS", ""), \
         patch(f"{_AFF}.AFFILIATE_PROGRAM_ENABLED", True), \
         patch(f"{_AFF}.AFFILIATE_DEFAULT_ENROLLED", True), \
         patch(f"{_AFF}.AFFILIATE_REQUIRE_COMPANY_PAGE", False), \
         patch(f"{_AFF}.AFFILIATE_ENROLLMENT_BONUS_DAYS", 7), \
         patch(f"{_AFF}.AFFILIATE_REFERRAL_BONUS_DAYS", 14), \
         patch(f"{_AFF}.AFFILIATE_MAX_REWARD_DAYS", 90), \
         patch(f"{_AFF}.AFFILIATE_DISCLOSURE_TEXT", DISCLOSURE), \
         patch("cqc_lem.utilities.env_constants.FREE_TRIAL_DAYS", 14):
        yield


def _get(client):
    r = client.get("/api/user/affiliate?session_token=t")
    assert r.status_code == 200
    return r.json()["detail"]


def _post(client, path, **body):
    r = client.post(f"/api/user/affiliate{path}", json={"session_token": "t", **body})
    return r


def test_user_is_enrolled_by_default_with_a_link_and_the_bonus(client, env, store):
    detail = _get(client)
    assert detail["enrolled"] is True and detail["status"] == "enrolled"
    assert detail["referral_url"].endswith("ref=42")
    assert detail["days_earned"] == 7
    assert store.trial_days() == 21          # 14 standard + 7 enrollment bonus
    # (B) is untouched by (A) being on.
    assert detail["promo_content_opt_in"] is False
    assert detail["promo_content_allowed"] is False


def test_enrollment_is_idempotent_across_page_loads(client, env, store):
    _get(client)
    _get(client)
    assert store.trial_days() == 21
    assert len([r for r in store.rewards if r["kind"] == "enrollment"]) == 1


def test_opt_out_is_immediate_and_returns_the_standard_trial(client, env, store):
    _get(client)
    r = _post(client, "/status", enrolled=False)
    assert r.status_code == 200
    detail = r.json()["detail"]
    assert detail["enrolled"] is False and detail["referral_url"] == ""
    assert store.trial_days() == 14
    # The response carries the resulting trial end, so the UI can say what the user now has.
    assert detail["trial_ends_at"] is not None


def test_opting_back_in_regrants_the_bonus_once(client, env, store):
    _get(client)
    _post(client, "/status", enrolled=False)
    _post(client, "/status", enrolled=True)
    assert store.trial_days() == 21
    _post(client, "/status", enrolled=True)     # a second opt-in must not pay again
    assert store.trial_days() == 21


def test_promo_consent_requires_an_explicit_acknowledgement(client, env, store):
    _get(client)
    r = client.post("/api/user/affiliate/promo-consent",
                    json={"session_token": "t", "enabled": True, "consent_acknowledged": False})
    assert r.status_code == 422
    assert store.enrollments[USER]["promo_content_opt_in"] == 0


def test_promo_consent_records_a_timestamp_and_can_be_withdrawn(client, env, store):
    _get(client)
    detail = _post(client, "/promo-consent", enabled=True, consent_acknowledged=True).json()["detail"]
    assert detail["promo_content_opt_in"] is True
    assert detail["promo_consent_at"] is not None
    assert detail["promo_content_allowed"] is True

    # Withdrawing needs no acknowledgement, and clears the consent record with it.
    detail = _post(client, "/promo-consent", enabled=False).json()["detail"]
    assert detail["promo_content_opt_in"] is False
    assert detail["promo_consent_at"] is None
    assert detail["promo_content_allowed"] is False


def test_leaving_the_program_withdraws_promo_consent_too(client, env, store):
    _get(client)
    _post(client, "/promo-consent", enabled=True, consent_acknowledged=True)
    detail = _post(client, "/status", enrolled=False).json()["detail"]
    assert detail["promo_content_opt_in"] is False
    assert detail["promo_content_allowed"] is False


def test_promo_consent_cannot_be_enabled_by_a_non_member(client, env, store):
    _get(client)
    _post(client, "/status", enrolled=False)
    r = _post(client, "/promo-consent", enabled=True, consent_acknowledged=True)
    assert r.status_code == 422


def test_notice_acknowledgement_is_recorded_once(client, env, store):
    _get(client)
    detail = _post(client, "/notice").json()["detail"]
    first = detail["notice_seen_at"]
    assert first is not None
    assert _post(client, "/notice").json()["detail"]["notice_seen_at"] == first


def test_endpoints_reject_an_invalid_session(client, env):
    with patch(f"{_M}.get_session_user_id", return_value=None):
        assert client.get("/api/user/affiliate?session_token=x").status_code == 401
        assert _post(client, "/status", enrolled=False).status_code == 401
        assert _post(client, "/notice").status_code == 401


def test_self_referral_pays_nobody(client, env, store):
    """The whole referral path, end to end: attribute at signup → activate → reward."""
    from cqc_lem.utilities.marketing.affiliate import attribute_referral, convert_referral
    _get(client)
    before = store.trial_days()
    attribute_referral(USER, {"ref": str(USER)})
    assert store.referrals[0]["status"] == "rejected"
    assert store.referrals[0]["reject_reason"] == "self_referral"
    # A rejected referral never converts, so it can never pay.
    assert convert_referral(USER) is None
    assert store.trial_days() == before


def test_a_real_referral_pays_only_on_activation(client, env, store):
    from cqc_lem.utilities.marketing.affiliate import attribute_referral, convert_referral
    _get(client)
    attribute_referral(7, {"ref": str(USER)})
    assert store.referrals[0]["status"] == "pending"
    assert store.trial_days() == 21           # signup alone earns the referrer nothing

    result = convert_referral(7)
    assert result["granted"] is True and result["days"] == 14
    assert store.trial_days() == 35
    assert store.referrals[0]["status"] == "converted"

    # A replayed activation cannot pay twice.
    assert convert_referral(7) is None
    assert store.trial_days() == 35


def test_duplicate_referral_attribution_is_rejected(client, env, store):
    from cqc_lem.utilities.marketing.affiliate import attribute_referral
    _get(client)
    attribute_referral(7, {"ref": str(USER)})
    second = attribute_referral(7, {"ref": str(USER)})
    assert second["reason"] == "duplicate"
    assert len(store.referrals) == 1
