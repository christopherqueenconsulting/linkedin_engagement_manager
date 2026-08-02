"""Unit tests for the LinkedIn sign-in status store (issue #933)."""

import json

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.linkedin.login_status"


class FakeRedis:
    """Minimal in-memory stand-in — just the get/set the store uses."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex


@pytest.fixture
def fake_redis():
    client = FakeRedis()
    with patch(f"{_MOD}.shared_redis_client", return_value=client):
        yield client


@pytest.fixture
def no_redis():
    with patch(f"{_MOD}.shared_redis_client", return_value=None):
        yield


class TestTtl:
    def test_defaults_when_unset(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_LOGIN_STATUS_TTL_SECONDS", raising=False)
        monkeypatch.delenv("LINKEDIN_LOGIN_STATUS_PENDING_TTL_SECONDS", raising=False)
        from cqc_lem.utilities.linkedin.login_status import _pending_ttl_seconds, _ttl_seconds
        assert _ttl_seconds() == 30 * 24 * 60 * 60
        assert _pending_ttl_seconds() == 900

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_LOGIN_STATUS_TTL_SECONDS", "60")
        monkeypatch.setenv("LINKEDIN_LOGIN_STATUS_PENDING_TTL_SECONDS", "30")
        from cqc_lem.utilities.linkedin.login_status import _pending_ttl_seconds, _ttl_seconds
        assert _ttl_seconds() == 60
        assert _pending_ttl_seconds() == 30

    def test_bad_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_LOGIN_STATUS_TTL_SECONDS", "soon")
        monkeypatch.setenv("LINKEDIN_LOGIN_STATUS_PENDING_TTL_SECONDS", "later")
        from cqc_lem.utilities.linkedin.login_status import _pending_ttl_seconds, _ttl_seconds
        assert _ttl_seconds() == 30 * 24 * 60 * 60
        assert _pending_ttl_seconds() == 900


class TestLifecycle:
    def test_pending_then_signed_in_records_the_approval_landed(self, fake_redis):
        from cqc_lem.utilities.linkedin.login_status import (
            LinkedInLoginState, get_login_status, mark_approval_pending, mark_signed_in)

        mark_approval_pending(7)
        pending = get_login_status(7)
        assert pending["state"] == LinkedInLoginState.APPROVAL_PENDING
        assert pending["approval_requested_at"]

        mark_signed_in(7)
        done = get_login_status(7)
        assert done["state"] == LinkedInLoginState.SIGNED_IN
        assert done["signed_in_at"]
        # The whole point of the issue: the tap the user already made is visibly received.
        assert done["approval_cleared_at"]
        assert done["approval_requested_at"] == pending["approval_requested_at"]

    def test_the_cookie_persist_does_not_erase_the_approval_it_follows(self, fake_redis):
        """One login records the sign-in twice — when the approval clears and again when the
        cookies persist. The second write must not wipe the fact the user's tap landed."""
        from cqc_lem.utilities.linkedin.login_status import (
            get_login_status, mark_approval_pending, mark_signed_in)

        mark_approval_pending(7)
        mark_signed_in(7)
        cleared = get_login_status(7)["approval_cleared_at"]
        mark_signed_in(7)

        status = get_login_status(7)
        assert status["approval_cleared_at"] == cleared
        assert status["approval_requested_at"]

    def test_a_later_sign_in_does_not_re_claim_an_old_approval(self, fake_redis):
        """Only the approval THIS login cleared counts — a routine sign-in weeks later must not
        keep telling the user their device approval came through."""
        from cqc_lem.utilities.linkedin.login_status import (
            _key, get_login_status, mark_signed_in)

        fake_redis.store[_key(7)] = json.dumps({
            "state": "signed_in",
            "signed_in_at": "2026-07-01T00:00:00+00:00",
            "approval_requested_at": "2026-07-01T00:00:00+00:00",
            "approval_cleared_at": "2026-07-01T00:00:00+00:00",
        })

        mark_signed_in(7)
        status = get_login_status(7)
        assert status["approval_cleared_at"] is None
        assert status["approval_requested_at"] is None

    def test_unparseable_timestamp_is_treated_as_old(self, fake_redis):
        from cqc_lem.utilities.linkedin.login_status import _is_recent

        assert _is_recent("not-a-time", 900) is False
        assert _is_recent(None, 900) is False

    def test_sign_in_without_a_challenge_claims_no_approval(self, fake_redis):
        from cqc_lem.utilities.linkedin.login_status import get_login_status, mark_signed_in

        mark_signed_in(7)
        status = get_login_status(7)
        assert status["approval_cleared_at"] is None
        assert status["approval_requested_at"] is None

    def test_timeout_keeps_the_last_good_sign_in(self, fake_redis):
        from cqc_lem.utilities.linkedin.login_status import (
            LinkedInLoginState, get_login_status, mark_approval_pending,
            mark_approval_timed_out, mark_signed_in)

        mark_signed_in(7)
        first = get_login_status(7)["signed_in_at"]
        mark_approval_pending(7)
        mark_approval_timed_out(7)

        status = get_login_status(7)
        assert status["state"] == LinkedInLoginState.APPROVAL_TIMED_OUT
        # "You approved on the 2nd, we're asking again now" beats "we have never signed in".
        assert status["signed_in_at"] == first
        assert status["approval_requested_at"]

    def test_pending_expires_sooner_than_a_settled_record(self, fake_redis, monkeypatch):
        monkeypatch.delenv("LINKEDIN_LOGIN_STATUS_TTL_SECONDS", raising=False)
        monkeypatch.delenv("LINKEDIN_LOGIN_STATUS_PENDING_TTL_SECONDS", raising=False)
        from cqc_lem.utilities.linkedin.login_status import (
            _key, mark_approval_pending, mark_signed_in)

        mark_approval_pending(7)
        pending_ttl = fake_redis.ttls[_key(7)]
        mark_signed_in(7)
        assert pending_ttl == 900
        assert fake_redis.ttls[_key(7)] == 30 * 24 * 60 * 60

    def test_keys_are_per_user(self, fake_redis):
        from cqc_lem.utilities.linkedin.login_status import get_login_status, mark_signed_in

        mark_signed_in(7)
        assert get_login_status(8) is None


class TestFailsOpen:
    def test_no_redis_is_a_silent_no_op(self, no_redis):
        from cqc_lem.utilities.linkedin.login_status import (
            get_login_status, mark_approval_pending, mark_approval_timed_out, mark_signed_in)

        mark_approval_pending(7)
        mark_approval_timed_out(7)
        mark_signed_in(7)
        assert get_login_status(7) is None

    def test_read_error_reads_as_nothing_recorded(self, fake_redis):
        from cqc_lem.utilities.linkedin.login_status import get_login_status

        with patch.object(fake_redis, "get", side_effect=RuntimeError("down")):
            assert get_login_status(7) is None

    def test_write_error_never_raises(self, fake_redis):
        from cqc_lem.utilities.linkedin.login_status import mark_signed_in

        with patch.object(fake_redis, "set", side_effect=RuntimeError("down")):
            mark_signed_in(7)  # must not raise into the login path

    def test_corrupt_payload_reads_as_nothing_recorded(self, fake_redis):
        from cqc_lem.utilities.linkedin.login_status import _key, get_login_status

        fake_redis.store[_key(7)] = "{not json"
        assert get_login_status(7) is None

    def test_non_dict_payload_reads_as_nothing_recorded(self, fake_redis):
        from cqc_lem.utilities.linkedin.login_status import _key, get_login_status

        fake_redis.store[_key(7)] = json.dumps(["signed_in"])
        assert get_login_status(7) is None
