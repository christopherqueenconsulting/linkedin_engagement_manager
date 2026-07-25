"""Integration test for NPS + review capture (issue #501).

Drives the real POST /api/survey/nps, POST /api/survey/review and GET /api/user/survey handlers
through the real `utilities/surveys.py` policy and the real db.py SQL into a stateful fake cursor
that models `feedback` + `survey_prompts` + `onboarding_state` — so the rows provably land with the
right source, the ask is recorded once, and a review provably flips the extended-trial gate
(`has_review_feedback`, issue #499) without needing a live MySQL container.
"""
import json
from datetime import datetime, timedelta

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.integration

_DB = "cqc_lem.utilities.db"


class _FakeCursor:
    """Minimal MySQL-ish cursor over an in-memory feedback / survey_prompts / onboarding store."""

    def __init__(self, store, dictionary=False):
        self.store = store
        self.dictionary = dictionary
        self._result = []
        self.rowcount = 0
        self.lastrowid = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._result = []
        self.rowcount = 0
        if s.startswith("INSERT INTO feedback"):
            row = {"id": len(self.store["feedback"]) + 1, "user_id": params[0], "source": params[1],
                   "type_hint": params[2], "body": params[3],
                   "context_json": json.loads(params[4]) if params[4] else None,
                   "sentiment": params[5], "created_at": datetime.now()}
            self.store["feedback"].append(row)
            self.lastrowid = row["id"]
            self.rowcount = 1
        elif s.startswith("SELECT MAX(created_at) FROM feedback"):
            answers = [r["created_at"] for r in self.store["feedback"]
                       if r["user_id"] == params[0] and r["source"] == params[1]]
            self._result = [(max(answers) if answers else None,)]
        elif s.startswith("SELECT 1 FROM feedback"):
            self._result = [(1,)] if any(r["user_id"] == params[0] and r["source"] == params[1]
                                         for r in self.store["feedback"]) else []
        elif s.startswith("INSERT IGNORE INTO survey_prompts"):
            if params[1] not in self.store["prompts"]:
                self.store["prompts"][params[1]] = datetime.now()
                self.rowcount = 1
        elif s.startswith("SELECT survey_key, sent_at"):
            self._result = list(self.store["prompts"].items())
        elif s.startswith("SELECT user_id, started_at"):
            self._result = [dict(self.store["onboarding"])] if self.store["onboarding"] else []
        elif s.startswith("SELECT subscription_status"):
            self._result = [dict(self.store["subscription"])]
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self, *a, **k):
        return _FakeCursor(self.store, dictionary=k.get("dictionary", False))

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from cqc_lem.api.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def store():
    return {
        "feedback": [],
        "prompts": {},
        "onboarding": {"user_id": 4, "started_at": datetime.now() - timedelta(days=10),
                       "linkedin_connected_at": None, "voice_set_at": None,
                       "first_post_approved_at": None, "caps_enabled_at": None,
                       "activated_at": datetime.now() - timedelta(days=5)},
        "subscription": {"subscription_status": "trial",
                         "trial_ends_at": datetime.now() + timedelta(days=2)},
    }


def _call(client, store, method, path, **kwargs):
    with patch(f"{_DB}.get_db_connection", side_effect=lambda *a, **k: _FakeConn(store)), \
         patch("cqc_lem.api.main.get_session_user_id", return_value=4), \
         patch("cqc_lem.utilities.observability.posthog"):
        return getattr(client, method)(path, **kwargs)


class TestSurveyCapture:
    def test_nps_lands_as_a_feedback_row_and_invites_a_promoter_to_review(self, client, store):
        response = _call(client, store, "post", "/api/survey/nps", json={
            "session_token": "sess-abc", "score": 10, "why": "It sounds exactly like me",
            "survey_key": "nps_day3", "context": {"route": "/"}})

        assert response.status_code == 200
        detail = response.json()["detail"]
        assert detail["bucket"] == "promoter"
        assert detail["review_invite"] is True  # promoter with no review on file yet

        row = store["feedback"][0]
        assert row["source"] == "nps"
        assert row["body"] == "It sounds exactly like me"
        assert row["sentiment"] == "promoter"
        assert row["context_json"] == {"route": "/", "score": 10, "survey_key": "nps_day3"}
        assert row["id"] == detail["feedback_id"]
        # The ask that produced the answer is closed out, so nothing re-asks it.
        assert "nps_day3" in store["prompts"]

    def test_review_lands_with_source_review_and_unlocks_the_trial_extension(self, client, store):
        from cqc_lem.utilities.db import has_review_feedback

        with patch(f"{_DB}.get_db_connection", side_effect=lambda *a, **k: _FakeConn(store)):
            assert has_review_feedback(4) is False  # gate closed before the review

        response = _call(client, store, "post", "/api/survey/review", json={
            "session_token": "sess-abc", "rating": 5, "improvement": "Fewer clicks to approve",
            "testimonial": "Saves me an hour a day", "consent_testimonial": True,
            "survey_key": "review_trial_end"})

        assert response.status_code == 200
        assert response.json()["detail"]["trial_extension_unlocked"] is True

        row = store["feedback"][0]
        assert row["source"] == "review"
        assert row["sentiment"] == "positive"
        assert row["context_json"]["rating"] == 5
        assert row["context_json"]["consent_testimonial"] is True
        assert "Saves me an hour a day" in row["body"]
        assert "Fewer clicks to approve" in row["body"]

        with patch(f"{_DB}.get_db_connection", side_effect=lambda *a, **k: _FakeConn(store)):
            assert has_review_feedback(4) is True  # gate now open for POST /trial/extend (#499)

    def test_the_due_survey_is_the_review_offer_and_it_clears_once_answered(self, client, store):
        # Trial ends in 2 days with no review on file → the review offer outranks the NPS ask.
        response = _call(client, store, "get", "/api/user/survey?session_token=sess-abc")
        detail = response.json()["detail"]
        assert detail["survey"]["key"] == "review_trial_end"
        assert detail["survey"]["source"] == "review"
        assert detail["review_submitted"] is False

        _call(client, store, "post", "/api/survey/review",
              json={"session_token": "sess-abc", "rating": 4, "survey_key": "review_trial_end"})

        detail = _call(client, store, "get",
                       "/api/user/survey?session_token=sess-abc").json()["detail"]
        assert detail["review_submitted"] is True
        assert detail["survey"] is None  # just answered — the daily cooldown holds the next ask

        # A day later the cooldown has lapsed: the NPS this user (activated 5 days ago) still owes
        # is next — never a review re-ask.
        store["prompts"]["review_trial_end"] = datetime.now() - timedelta(days=1, hours=1)
        detail = _call(client, store, "get",
                       "/api/user/survey?session_token=sess-abc").json()["detail"]
        assert detail["survey"]["key"] == "nps_day3"

    def test_a_dismissed_survey_is_never_shown_again(self, client, store):
        dismissed = _call(client, store, "post", "/api/survey/dismiss",
                          json={"session_token": "sess-abc", "survey_key": "review_trial_end"})
        assert dismissed.json()["detail"]["dismissed"] is True

        detail = _call(client, store, "get",
                       "/api/user/survey?session_token=sess-abc").json()["detail"]
        # The review ask is spent; the cooldown holds the next ask back for a day.
        assert detail["survey"] is None
