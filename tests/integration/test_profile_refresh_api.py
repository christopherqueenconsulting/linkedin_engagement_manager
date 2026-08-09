"""On-demand profile re-scrape, end to end (issue #1076).

Unit tests mock the claim and the dispatch, so they prove the handler and not the loop. These prove
the two halves only real infrastructure can answer:

- **Redis** — the real limiter behind the real endpoint. Press the button twice and exactly one
  Chrome session is asked for; the GET then reports the same window the second press hit, which is
  what keeps the SPA's disabled state honest across a reload.
- **MySQL** — the task's DB half. A re-scrape is only useful if the voice brief every generation
  prompt reads is re-distilled from it and PERSISTED, so the round trip is asserted against the
  `profiles` row rather than against `set_profile_synthesis` having been called.

Only the two external boundaries are faked: the Redis handle, and the Chrome session + LLM call the
task would otherwise make.
"""

from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from cqc_lem.utilities import db

pytestmark = pytest.mark.integration

_M = "cqc_lem.api.main"
_USER = "cqc_lem.api.routers.user"
_LIMITER = "cqc_lem.utilities.profile_refresh"
_AUTOMATION = "cqc_lem.app.run_automation"
_TOK = "session-token"
_EMAIL = "profile-refresh-1076@example.test"


class _FakeRedis:
    """Enough of Redis for a fixed-window counter, shared by the endpoint and the peek."""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def ttl(self, key):
        return self.ttls.get(key, -2)

    def get(self, key):
        value = self.counts.get(key)
        return None if value is None else str(value).encode()


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from cqc_lem.api.main import app
    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc


@pytest.fixture
def redis():
    fake = _FakeRedis()
    with patch(f"{_LIMITER}.shared_redis_client", return_value=fake):
        yield fake


class TestTheWindowBoundsTheButton:
    def test_two_presses_ask_for_exactly_one_chrome_session(self, client, redis):
        with patch(f"{_M}.get_session_user_id", return_value=4242), \
             patch(f"{_USER}.update_stale_profile") as task:
            first = client.post("/api/user/linkedin-profile/refresh", json={"session_token": _TOK})
            second = client.post("/api/user/linkedin-profile/refresh", json={"session_token": _TOK})

        assert first.status_code == 202 and second.status_code == 202
        assert first.json()["detail"]["queued"] is True
        # A second press the same day is a person pressing a button twice — an expected no-op, not
        # a 429 the SPA would render as a failure.
        assert second.json()["detail"]["queued"] is False
        assert second.json()["detail"]["reason"] == "already_refreshed_today"
        assert task.apply_async.call_count == 1
        assert task.apply_async.call_args.kwargs["kwargs"] == {
            "user_id": 4242, "force_refresh": True,
        }

    def test_the_profile_get_reports_the_window_the_press_spent(self, client, redis):
        with patch(f"{_M}.get_session_user_id", return_value=4243), \
             patch(f"{_USER}.update_stale_profile"), \
             patch(f"{_USER}.get_linkedin_profile_url_by_user_id", return_value=None):
            before = client.get(f"/api/user/linkedin-profile?session_token={_TOK}")
            client.post("/api/user/linkedin-profile/refresh", json={"session_token": _TOK})
            after = client.get(f"/api/user/linkedin-profile?session_token={_TOK}")

        assert before.json()["detail"]["refresh_available_in_seconds"] == 0
        assert after.json()["detail"]["refresh_available_in_seconds"] > 0

    def test_one_user_pressing_the_button_never_bounds_another(self, client, redis):
        with patch(f"{_USER}.update_stale_profile") as task:
            with patch(f"{_M}.get_session_user_id", return_value=4244):
                client.post("/api/user/linkedin-profile/refresh", json={"session_token": _TOK})
                client.post("/api/user/linkedin-profile/refresh", json={"session_token": _TOK})
            with patch(f"{_M}.get_session_user_id", return_value=4245):
                resp = client.post("/api/user/linkedin-profile/refresh",
                                   json={"session_token": _TOK})
        assert resp.json()["detail"]["queued"] is True
        assert task.apply_async.call_count == 2


def _schema_available() -> bool:
    """Is a migrated MySQL schema reachable?

    A reachable server is not enough — this needs the migrated `profiles` table. An un-migrated DB
    (a bare local server) skips instead of erroring; CI provisions it before the suite runs.
    """
    try:
        config = db._get_mysql_config()
        connection = mysql.connector.connect(connect_timeout=3, **config)
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW COLUMNS FROM profiles LIKE 'synthesis'")
        present = bool(cursor.fetchone())
        cursor.close()
        return present
    except Exception:  # noqa: BLE001
        return False
    finally:
        connection.close()


def _exec(sql: str, params=()):
    connection = db.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        connection.commit()
    finally:
        cursor.close()
        connection.close()


@pytest.fixture
def user_with_profile():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the profile-refresh integration test")
    from cqc_lem.utilities.linkedin.profile import LinkedInProfile

    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))
    _exec("DELETE FROM profiles WHERE email=%s", (_EMAIL,))
    db.add_user(_EMAIL, "x")
    uid = db.get_user_id(_EMAIL)
    profile = LinkedInProfile(full_name="Jane Doe", email=_EMAIL,
                              profile_url="https://www.linkedin.com/in/profile-refresh-1076/")
    db.add_linkedin_profile(profile, user_id=uid)
    db.set_profile_synthesis(uid, "- Stale brief, written before the profile was edited")
    yield uid, profile
    _exec("DELETE FROM profiles WHERE user_id=%s", (uid,))
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))


class TestTheTaskRoundTrip:
    def test_a_forced_refresh_persists_the_new_voice_brief(self, user_with_profile):
        uid, profile = user_with_profile
        from cqc_lem.app.run_automation import update_stale_profile

        fresh = "- Applied AI engineer\n- Speaks to LLM, RAG and agent systems"
        with patch(f"{_AUTOMATION}.get_current_profile",
                   return_value=(MagicMock(), MagicMock(), _EMAIL, profile)) as get_profile, \
             patch(f"{_AUTOMATION}.synthesize_profile", return_value=fresh), \
             patch(f"{_AUTOMATION}.quit_gracefully"):
            result = update_stale_profile.run(user_id=uid, force_refresh=True)

        assert result == "Profile Updated Successfully"
        assert get_profile.call_args.kwargs["force_refresh"] is True
        stored, generated_at = db.get_profile_synthesis(uid)
        assert stored == fresh
        assert generated_at is not None

    def test_a_failed_scrape_leaves_the_previous_brief_standing(self, user_with_profile):
        """A failed scrape must not wipe the voice LEM is already writing in.

        The brief the generators read is the only copy, and an empty one falls back to the raw
        profile JSON.
        """
        uid, _profile = user_with_profile
        from cqc_lem.app.run_automation import update_stale_profile

        with patch(f"{_AUTOMATION}.get_current_profile", side_effect=RuntimeError("429")), \
             patch(f"{_AUTOMATION}.quit_gracefully"):
            result = update_stale_profile.run(user_id=uid, force_refresh=True)

        assert "Failed to update profile" in result
        stored, _generated_at = db.get_profile_synthesis(uid)
        assert stored == "- Stale brief, written before the profile was edited"
