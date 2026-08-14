"""Integration test for GET /api/user/linkedin-signin-status (issue #933).

Drives the real endpoint through the real Redis-backed store (only Redis itself is faked), so the
whole path the reporter could not see is exercised: the login flow records a device-approval ask,
the sign-in that follows clears it, and the Account page can read back "your approval landed".
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_M = "cqc_lem.api.main"
_STORE = "cqc_lem.utilities.linkedin.login_status"
_TOK = "session-token"
_UID = 42


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        return True


@pytest.fixture
def redis():
    fake = _FakeRedis()
    with patch(f"{_STORE}.shared_redis_client", return_value=fake):
        yield fake


def _get(api_client):
    with patch(f"{_M}.get_session_user_id", return_value=_UID):
        return api_client.get(f"/api/user/linkedin-signin-status?session_token={_TOK}")


class TestLinkedInSignInStatusEndpoint:
    def test_requires_a_session(self, api_client, redis):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = api_client.get("/api/user/linkedin-signin-status?session_token=bad")
        assert resp.status_code == 401

    def test_nothing_recorded_is_unknown_not_broken(self, api_client, redis):
        resp = _get(api_client)
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        # `unknown` must not read as a failure — it only means no sign-in is on record.
        assert detail["state"] == "unknown"
        assert detail["signed_in_at"] is None
        assert detail["approval_cleared_at"] is None

    def test_pending_approval_is_visible_while_the_user_is_being_asked(self, api_client, redis):
        from cqc_lem.utilities.linkedin.login_status import mark_approval_pending

        mark_approval_pending(_UID)
        detail = _get(api_client).json()["detail"]
        assert detail["state"] == "approval_pending"
        assert detail["approval_requested_at"]
        assert detail["approval_cleared_at"] is None

    def test_the_approval_the_user_already_made_shows_as_received(self, api_client, redis):
        from cqc_lem.utilities.linkedin.login_status import mark_approval_pending, mark_signed_in

        mark_approval_pending(_UID)
        # One login records the sign-in twice — when the approval clears, then at the cookie
        # persist. The user's tap must still be visible after the second write.
        mark_signed_in(_UID)
        mark_signed_in(_UID)
        detail = _get(api_client).json()["detail"]
        assert detail["state"] == "signed_in"
        assert detail["signed_in_at"]
        assert detail["approval_cleared_at"]

    def test_timed_out_approval_keeps_the_last_good_sign_in(self, api_client, redis):
        from cqc_lem.utilities.linkedin.login_status import (
            mark_approval_pending,
            mark_approval_timed_out,
            mark_signed_in,
        )

        mark_signed_in(_UID)
        mark_approval_pending(_UID)
        mark_approval_timed_out(_UID)
        detail = _get(api_client).json()["detail"]
        assert detail["state"] == "approval_timed_out"
        assert detail["signed_in_at"]

    def test_another_users_record_is_never_returned(self, api_client, redis):
        from cqc_lem.utilities.linkedin.login_status import mark_signed_in

        mark_signed_in(_UID + 1)
        assert _get(api_client).json()["detail"]["state"] == "unknown"

    def test_redis_down_reads_as_unknown(self, api_client):
        with patch(f"{_STORE}.shared_redis_client", return_value=None):
            detail = _get(api_client).json()["detail"]
        assert detail["state"] == "unknown"
