"""Unit tests for `utilities/linkedin/session_health.py` — the `/health/deep` session reading (#2356).

Every reader the module consults (`get_login_status`, the challenge cooldown, the 429 breaker and
the automation pause) is stubbed, so each state is driven from what the owning modules would have
recorded, and nothing reaches Redis or MySQL.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.linkedin import session_health as sh

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.delenv("LINKEDIN_SESSION_STALE_HOURS", raising=False)
    monkeypatch.delenv("HEALTH_DEEP_SESSION_DEGRADES", raising=False)
    sh.reset_cache()
    yield
    sh.reset_cache()


@contextmanager
def _readers(records=None, cooldowns=None, breaker=0, paused=False, users=(1,),
             client=None):
    """Stub every reader; `records` / `cooldowns` map user id → value."""
    records = records or {}
    cooldowns = cooldowns or {}
    redis = MagicMock() if client is None else client
    with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client", return_value=redis), \
         patch("cqc_lem.utilities.db.get_active_user_ids", return_value=list(users)), \
         patch("cqc_lem.utilities.linkedin.rate_limit.rate_limit_cooldown_remaining",
               return_value=breaker), \
         patch("cqc_lem.utilities.linkedin.rate_limit.is_automation_paused", return_value=paused), \
         patch("cqc_lem.utilities.linkedin.login_status.get_login_status",
               side_effect=lambda uid: records.get(uid)), \
         patch("cqc_lem.utilities.linkedin.login_status.challenge_cooldown_remaining",
               side_effect=lambda uid: cooldowns.get(uid, 0)):
        yield


class TestClassifyUser:
    @pytest.mark.parametrize("state", ["approval_pending", "approval_timed_out"])
    def test_approval_waiting_on_the_owner_is_needs_owner(self, state):
        record = {"state": state, "signed_in_at": (_NOW - timedelta(hours=1)).isoformat()}
        assert sh.classify_user(record, 0, False, 24, _NOW) == sh.SESSION_NEEDS_OWNER

    def test_a_running_challenge_cooldown_is_needs_owner(self):
        record = {"state": "challenge_unsolvable", "signed_in_at": None}
        assert sh.classify_user(record, 3600, False, 24, _NOW) == sh.SESSION_NEEDS_OWNER

    def test_needs_owner_outranks_backing_off(self):
        assert sh.classify_user({"state": "approval_pending"}, 0, True, 24, _NOW) \
            == sh.SESSION_NEEDS_OWNER

    def test_no_record_is_stale(self):
        assert sh.classify_user(None, 0, False, 24, _NOW) == sh.SESSION_STALE

    def test_a_sign_in_older_than_the_window_is_stale(self):
        record = {"state": "signed_in", "signed_in_at": (_NOW - timedelta(hours=25)).isoformat()}
        assert sh.classify_user(record, 0, False, 24, _NOW) == sh.SESSION_STALE

    def test_an_unparseable_sign_in_time_is_stale(self):
        record = {"state": "signed_in", "signed_in_at": "not-a-time"}
        assert sh.classify_user(record, 0, False, 24, _NOW) == sh.SESSION_STALE

    def test_stale_outranks_backing_off(self):
        assert sh.classify_user(None, 0, True, 24, _NOW) == sh.SESSION_STALE

    def test_a_fresh_sign_in_under_the_breaker_is_backing_off(self):
        record = {"state": "signed_in", "signed_in_at": (_NOW - timedelta(hours=1)).isoformat()}
        assert sh.classify_user(record, 0, True, 24, _NOW) == sh.SESSION_BACKING_OFF

    def test_a_fresh_sign_in_is_ok(self):
        record = {"state": "signed_in", "signed_in_at": (_NOW - timedelta(hours=23)).isoformat()}
        assert sh.classify_user(record, 0, False, 24, _NOW) == sh.SESSION_OK

    def test_a_naive_timestamp_is_read_as_utc(self):
        record = {"state": "signed_in", "signed_in_at": "2026-10-10T11:00:00"}
        assert sh.classify_user(record, 0, False, 24, _NOW) == sh.SESSION_OK

    def test_the_window_comes_from_the_env(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_SESSION_STALE_HOURS", "2")
        record = {"state": "signed_in", "signed_in_at": _ago(3)}
        assert sh.classify_user(record, 0, False) == sh.SESSION_STALE

    def test_a_bad_window_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_SESSION_STALE_HOURS", "soon")
        record = {"state": "signed_in", "signed_in_at": _ago(3)}
        assert sh.classify_user(record, 0, False) == sh.SESSION_OK


class TestSessionHealth:
    def test_all_fresh_is_ok(self):
        records = {1: {"state": "signed_in", "signed_in_at": _ago(1)},
                   2: {"state": "signed_in", "signed_in_at": _ago(2)}}
        with _readers(records=records, users=(1, 2)):
            out = sh.session_health()
        assert out == {"linkedin_session": "ok", "session_checked": 2, "session_failing": 0}

    def test_reports_the_worst_state_and_counts_failing(self):
        records = {1: {"state": "signed_in", "signed_in_at": _ago(1)},
                   2: {"state": "approval_pending", "signed_in_at": _ago(1)}}
        with _readers(records=records, users=(1, 2, 3)):
            out = sh.session_health()
        # user 3 has no record (stale), user 2 waits on the owner
        assert out == {"linkedin_session": "needs_owner", "session_checked": 3,
                       "session_failing": 2}

    def test_challenge_cooldown_is_needs_owner(self):
        records = {1: {"state": "challenge_unsolvable", "signed_in_at": _ago(1)}}
        with _readers(records=records, cooldowns={1: 900}):
            assert sh.session_health()["linkedin_session"] == "needs_owner"

    @pytest.mark.parametrize("breaker,paused", [(600, False), (0, True)])
    def test_breaker_or_pause_is_backing_off_and_not_failing(self, breaker, paused):
        records = {1: {"state": "signed_in", "signed_in_at": _ago(1)}}
        with _readers(records=records, breaker=breaker, paused=paused):
            out = sh.session_health()
        assert out == {"linkedin_session": "backing_off", "session_checked": 1,
                       "session_failing": 0}

    def test_no_redis_handle_is_unknown_never_ok(self):
        with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client",
                   return_value=None):
            out = sh.session_health()
        assert out == {"linkedin_session": "unknown", "session_checked": 0, "session_failing": 0}

    def test_unreadable_redis_is_unknown_never_stale(self):
        client = MagicMock()
        client.ping.side_effect = ConnectionError("refused")
        with _readers(client=client):
            out = sh.session_health()
        assert out["linkedin_session"] == "unknown"

    def test_no_active_users_is_unknown(self):
        with _readers(users=()):
            assert sh.session_health()["linkedin_session"] == "unknown"

    def test_users_are_bounded(self):
        with _readers(users=range(1, 50)):
            out = sh.session_health()
        assert out["session_checked"] == 10

    def test_a_crash_is_unknown_warns_once_per_window_and_never_raises(self):
        with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client",
                   side_effect=RuntimeError("boom")), \
             patch.object(sh, "log_warning") as warn:
            first = sh.session_health()
            second = sh.session_health()
        assert first["linkedin_session"] == second["linkedin_session"] == "unknown"
        warn.assert_called_once()

    def test_reading_is_cached(self):
        records = {1: {"state": "signed_in", "signed_in_at": _ago(1)}}
        with _readers(records=records):
            sh.session_health()
        # Readers are gone now; a cached reading must not touch them.
        with patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client",
                   side_effect=AssertionError("re-measured")), \
             patch.object(sh, "log_warning"):
            assert sh.session_health()["linkedin_session"] == "ok"
            sh.reset_cache()
            assert sh.session_health(use_cache=True)["linkedin_session"] == "unknown"

    def test_use_cache_false_re_measures(self):
        with _readers(records={1: {"state": "signed_in", "signed_in_at": _ago(1)}}):
            sh.session_health()
        with _readers(records={}):
            assert sh.session_health(use_cache=False)["linkedin_session"] == "stale"


class TestShouldDegrade:
    @pytest.mark.parametrize("state", ["needs_owner", "stale"])
    def test_off_by_default(self, state):
        assert sh.should_degrade({"linkedin_session": state}) is False

    @pytest.mark.parametrize("state", ["needs_owner", "stale"])
    def test_failing_states_degrade_when_on(self, monkeypatch, state):
        monkeypatch.setenv("HEALTH_DEEP_SESSION_DEGRADES", "true")
        assert sh.should_degrade({"linkedin_session": state}) is True

    @pytest.mark.parametrize("state", ["ok", "backing_off", "unknown"])
    def test_other_states_never_degrade(self, monkeypatch, state):
        monkeypatch.setenv("HEALTH_DEEP_SESSION_DEGRADES", "true")
        assert sh.should_degrade({"linkedin_session": state}) is False


def test_summarize_of_nothing_is_unknown():
    assert sh.summarize([]) == "unknown"
