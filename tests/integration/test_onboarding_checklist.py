"""Integration test for the activation checklist endpoint (issue #500).

Drives the real GET /api/user/onboarding handler through the real onboarding module and the real
db.py SQL into a stateful fake cursor that models `onboarding_state` — so a step the user just
finished provably lands as a persisted row (and emits its funnel event exactly once) without needing
a live MySQL container.
"""
from datetime import datetime, timedelta

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.integration

_DB = "cqc_lem.utilities.db"

_STEP_COLUMNS = ("linkedin_connected_at", "voice_set_at", "first_post_approved_at",
                 "caps_enabled_at", "activated_at")


class _FakeCursor:
    """Minimal MySQL-ish cursor over an in-memory `onboarding_state` / `onboarding_nudges` store."""

    def __init__(self, store, dictionary=False):
        self.store = store
        self.dictionary = dictionary
        self._result = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._result = []
        self.rowcount = 0
        if s.startswith("INSERT IGNORE INTO onboarding_state"):
            if self.store["state"] is None:
                self.store["state"] = {"user_id": params[0], "started_at": self.store["started_at"],
                                       **{c: None for c in _STEP_COLUMNS}}
                self.rowcount = 1
        elif s.startswith("SELECT user_id, started_at"):
            state = self.store["state"]
            self._result = [dict(state)] if state else []
        elif s.startswith("UPDATE onboarding_state SET"):
            column = s.split("SET ")[1].split(" =")[0]
            state = self.store["state"]
            if state and state.get(column) is None:
                state[column] = datetime.now()
                self.rowcount = 1
        elif s.startswith("SELECT nudge_key, sent_at"):
            self._result = list(self.store["nudges"].items())
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


def _get(client, store, *, session, voice, approved, posted, engaged, trial_ends_at=None):
    """Call the endpoint with the checklist EVIDENCE stubbed but state persistence real."""
    prefs = {"tone": "friendly" if voice else None, "comment_style": None, "focus_topics": [],
             "include_topics": [], "max_comments_per_day": 20, "max_dms_per_day": 20}

    def _has_post(_user_id, statuses):
        from cqc_lem.utilities.db import PostStatus
        return posted if tuple(statuses) == (PostStatus.POSTED,) else approved

    with patch(f"{_DB}.get_db_connection", side_effect=lambda *a, **k: _FakeConn(store)), \
         patch("cqc_lem.api.main.get_session_user_id", return_value=4), \
         patch("cqc_lem.utilities.onboarding.has_linkedin_session", return_value=session), \
         patch("cqc_lem.utilities.onboarding.get_engagement_preferences", return_value=prefs), \
         patch("cqc_lem.utilities.onboarding.has_engagement_preferences", return_value=True), \
         patch("cqc_lem.utilities.onboarding.has_post_with_status", side_effect=_has_post), \
         patch("cqc_lem.utilities.onboarding.has_automated_engagement", return_value=engaged), \
         patch("cqc_lem.utilities.onboarding.get_user_subscription_info",
               return_value={"trial_ends_at": trial_ends_at}), \
         patch("cqc_lem.utilities.observability.track_onboarding_step") as track:
        response = client.get("/api/user/onboarding?session_token=sess-abc")
    return response, track


class TestOnboardingChecklistEndpoint:
    def test_steps_persist_once_and_the_next_nudge_surfaces(self, client):
        store = {"state": None, "nudges": {}, "started_at": datetime.now() - timedelta(days=3)}

        response, track = _get(client, store, session=True, voice=False, approved=False,
                               posted=False, engaged=False)

        assert response.status_code == 200
        detail = response.json()["detail"]
        assert detail["activated"] is False
        by_key = {s["key"]: s for s in detail["steps"]}
        assert by_key["linkedin_connected"]["ok"] is True
        assert by_key["voice_set"]["ok"] is False
        # Connected 3 days in with no voice yet → the voice nudge is the next best thing to say.
        assert detail["nudge"]["key"] == "set_voice"

        # Persisted, and one funnel event per completed step (session + saved caps), none for the
        # steps still outstanding.
        assert store["state"]["linkedin_connected_at"] is not None
        assert store["state"]["caps_enabled_at"] is not None
        assert store["state"]["voice_set_at"] is None
        assert {c.args[1] for c in track.call_args_list} == {"linkedin_connected", "caps_enabled"}

        # A second read re-derives the same truth but must not re-emit the event.
        response2, track2 = _get(client, store, session=True, voice=False, approved=False,
                                 posted=False, engaged=False)
        assert response2.json()["detail"]["steps"][0]["ok"] is True
        assert track2.call_count == 0

    def test_activation_clears_the_checklist_and_stops_nudging(self, client):
        store = {"state": None, "nudges": {}, "started_at": datetime.now() - timedelta(days=6)}

        response, _track = _get(client, store, session=True, voice=True, approved=True,
                                posted=True, engaged=True,
                                trial_ends_at=datetime.now() + timedelta(days=1))

        detail = response.json()["detail"]
        assert detail["activated"] is True
        assert all(s["ok"] for s in detail["steps"])
        assert detail["nudge"] is None  # activated users are never nudged, trial ending or not
        assert store["state"]["activated_at"] is not None
