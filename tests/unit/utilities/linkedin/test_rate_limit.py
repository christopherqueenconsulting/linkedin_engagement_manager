"""Unit tests for the LinkedIn 429 circuit breaker (rate_limit.py)."""

import os
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.linkedin.rate_limit"


@pytest.fixture
def fake_redis():
    client = MagicMock()
    with patch(f"{_MOD}._redis_client", return_value=client):
        yield client


class TestCooldownSeconds:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_RATE_LIMIT_COOLDOWN_SECONDS", raising=False)
        from cqc_lem.utilities.linkedin.rate_limit import _cooldown_seconds
        assert _cooldown_seconds() == 1800

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_COOLDOWN_SECONDS", "60")
        from cqc_lem.utilities.linkedin.rate_limit import _cooldown_seconds
        assert _cooldown_seconds() == 60

    def test_bad_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_COOLDOWN_SECONDS", "not-a-number")
        from cqc_lem.utilities.linkedin.rate_limit import _cooldown_seconds
        assert _cooldown_seconds() == 1800


class TestTripCountGraceSeconds:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_RATE_LIMIT_TRIP_GRACE_SECONDS", raising=False)
        from cqc_lem.utilities.linkedin.rate_limit import _trip_count_grace_seconds
        assert _trip_count_grace_seconds() == 1800

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_TRIP_GRACE_SECONDS", "90")
        from cqc_lem.utilities.linkedin.rate_limit import _trip_count_grace_seconds
        assert _trip_count_grace_seconds() == 90

    def test_bad_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_TRIP_GRACE_SECONDS", "nope")
        from cqc_lem.utilities.linkedin.rate_limit import _trip_count_grace_seconds
        assert _trip_count_grace_seconds() == 1800


class TestMarkRateLimited:
    def test_sets_key_with_ttl(self, fake_redis, monkeypatch):
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_COOLDOWN_SECONDS", "120")
        fake_redis.incr.return_value = 1  # first trip → base cooldown
        from cqc_lem.utilities.linkedin.rate_limit import mark_rate_limited
        mark_rate_limited("boom")
        fake_redis.set.assert_called_once_with("linkedin:429_cooldown", "boom", ex=120)

    def test_consecutive_trips_escalate_cooldown(self, fake_redis, monkeypatch):
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_COOLDOWN_SECONDS", "100")
        monkeypatch.delenv("LINKEDIN_RATE_LIMIT_MAX_COOLDOWN_SECONDS", raising=False)
        from cqc_lem.utilities.linkedin.rate_limit import mark_rate_limited
        for trip, expected in [(1, 100), (2, 200), (3, 400), (4, 800)]:
            fake_redis.incr.return_value = trip
            mark_rate_limited("x")
            assert fake_redis.set.call_args.kwargs["ex"] == expected

    def test_escalation_capped(self, fake_redis, monkeypatch):
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_COOLDOWN_SECONDS", "1800")
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_MAX_COOLDOWN_SECONDS", "21600")  # 6h
        fake_redis.incr.return_value = 20  # 1800 * 2^19 >> cap
        from cqc_lem.utilities.linkedin.rate_limit import mark_rate_limited
        mark_rate_limited("x")
        assert fake_redis.set.call_args.kwargs["ex"] == 21600

    def test_trip_counter_incremented_and_expired(self, fake_redis):
        fake_redis.incr.return_value = 1
        from cqc_lem.utilities.linkedin.rate_limit import mark_rate_limited
        mark_rate_limited("x")
        fake_redis.incr.assert_called_once_with("linkedin:429_trip_count")
        fake_redis.expire.assert_called_once()

    def test_trip_counter_ttl_is_cooldown_plus_grace(self, fake_redis, monkeypatch):
        # The counter must self-expire once a full cooldown + grace elapses without a new trip, so the
        # escalation resets instead of staying pinned at the cap (the old fixed-24h TTL doom loop).
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_COOLDOWN_SECONDS", "100")
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_TRIP_GRACE_SECONDS", "60")
        monkeypatch.delenv("LINKEDIN_RATE_LIMIT_MAX_COOLDOWN_SECONDS", raising=False)
        fake_redis.incr.return_value = 3  # cooldown = 100 * 2^2 = 400
        from cqc_lem.utilities.linkedin.rate_limit import mark_rate_limited
        mark_rate_limited("x")
        fake_redis.expire.assert_called_once_with("linkedin:429_trip_count", 400 + 60)

    def test_trip_counter_ttl_uses_capped_cooldown_plus_grace(self, fake_redis, monkeypatch):
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_COOLDOWN_SECONDS", "1800")
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_MAX_COOLDOWN_SECONDS", "21600")
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_TRIP_GRACE_SECONDS", "1800")
        fake_redis.incr.return_value = 20  # far past the cap → cooldown clamps to 21600
        from cqc_lem.utilities.linkedin.rate_limit import mark_rate_limited
        mark_rate_limited("x")
        fake_redis.expire.assert_called_once_with("linkedin:429_trip_count", 21600 + 1800)

    def test_defaults_reason(self, fake_redis):
        from cqc_lem.utilities.linkedin.rate_limit import mark_rate_limited
        mark_rate_limited()
        _, kwargs = fake_redis.set.call_args
        args = fake_redis.set.call_args.args
        assert args[1] == "429"

    def test_no_redis_is_noop(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.rate_limit import mark_rate_limited
            mark_rate_limited("x")  # must not raise

    def test_redis_error_is_swallowed(self, fake_redis):
        fake_redis.set.side_effect = RuntimeError("connection lost")
        from cqc_lem.utilities.linkedin.rate_limit import mark_rate_limited
        mark_rate_limited("x")  # must not raise


class TestTripTelemetry:
    """The trip is the only signal that says LinkedIn is throttling us, and the warning it logs
    never reaches PostHog at the default POSTHOG_LOG_LEVEL — so it is emitted as its own event for
    the Health dashboard's 429 tile and its spike alert (issue #650).
    """

    def test_trip_is_tracked_with_cooldown_and_consecutive_count(self, fake_redis, monkeypatch):
        monkeypatch.setenv("LINKEDIN_RATE_LIMIT_COOLDOWN_SECONDS", "100")
        monkeypatch.delenv("LINKEDIN_RATE_LIMIT_MAX_COOLDOWN_SECONDS", raising=False)
        fake_redis.incr.return_value = 3  # cooldown = 100 * 2^2
        with patch("cqc_lem.utilities.observability.track_rate_limit_trip") as tracked:
            from cqc_lem.utilities.linkedin.rate_limit import mark_rate_limited
            mark_rate_limited("auth wall")
        tracked.assert_called_once_with(400, 3, "auth wall")

    def test_tracking_failure_never_breaks_the_breaker(self, fake_redis):
        fake_redis.incr.return_value = 1
        with patch("cqc_lem.utilities.observability.track_rate_limit_trip",
                   side_effect=RuntimeError("posthog down")), \
                patch(f"{_MOD}.log_warning") as warned:
            from cqc_lem.utilities.linkedin.rate_limit import mark_rate_limited
            mark_rate_limited("x")  # must not raise
        fake_redis.set.assert_called_once()  # the cooldown was still written
        # ...and the failure must not be reported as a breaker failure: the breaker IS open, and
        # that message would send whoever is debugging a doom loop after the wrong thing.
        messages = [call.args[0] for call in warned.call_args_list]
        assert not any("Failed to set" in message for message in messages)
        assert any("Failed to track" in message for message in messages)


class TestCooldownRemaining:
    def test_returns_positive_ttl(self, fake_redis):
        fake_redis.ttl.return_value = 42
        from cqc_lem.utilities.linkedin.rate_limit import rate_limit_cooldown_remaining
        assert rate_limit_cooldown_remaining() == 42

    def test_zero_when_key_absent(self, fake_redis):
        fake_redis.ttl.return_value = -2  # redis: key does not exist
        from cqc_lem.utilities.linkedin.rate_limit import rate_limit_cooldown_remaining
        assert rate_limit_cooldown_remaining() == 0

    def test_zero_when_no_redis(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.rate_limit import rate_limit_cooldown_remaining
            assert rate_limit_cooldown_remaining() == 0

    def test_zero_on_redis_error(self, fake_redis):
        fake_redis.ttl.side_effect = RuntimeError("down")
        from cqc_lem.utilities.linkedin.rate_limit import rate_limit_cooldown_remaining
        assert rate_limit_cooldown_remaining() == 0


class TestClearRateLimit:
    def test_deletes_cooldown_and_trip_counter(self, fake_redis):
        from cqc_lem.utilities.linkedin.rate_limit import clear_rate_limit
        clear_rate_limit()
        fake_redis.delete.assert_called_once_with("linkedin:429_cooldown", "linkedin:429_trip_count")

    def test_no_redis_is_noop(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.rate_limit import clear_rate_limit
            clear_rate_limit()  # must not raise

    def test_error_swallowed(self, fake_redis):
        fake_redis.delete.side_effect = RuntimeError("down")
        from cqc_lem.utilities.linkedin.rate_limit import clear_rate_limit
        clear_rate_limit()  # must not raise


class TestRedisClientSelection:
    def test_prefers_redis_broker_url(self, monkeypatch):
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker:6379/0")
        fake = MagicMock()
        with patch.dict("sys.modules", {"redis": MagicMock(Redis=MagicMock(from_url=MagicMock(return_value=fake)))}):
            import importlib
            mod = importlib.import_module(_MOD)
            client = mod._redis_client()
        assert client is fake

    def test_falls_back_to_result_backend_when_broker_is_sqs(self, monkeypatch):
        monkeypatch.setenv("CELERY_BROKER_URL", "sqs://")
        monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://cache:6379/1")
        captured = {}

        def _from_url(url, **kw):
            captured["url"] = url
            return MagicMock()

        with patch.dict("sys.modules", {"redis": MagicMock(Redis=MagicMock(from_url=_from_url))}):
            import importlib
            mod = importlib.import_module(_MOD)
            mod._redis_client()
        assert captured["url"] == "redis://cache:6379/1"

    def test_returns_none_when_redis_missing(self, monkeypatch):
        with patch.dict("sys.modules", {"redis": None}):
            import importlib
            mod = importlib.import_module(_MOD)
            assert mod._redis_client() is None

    def test_shared_client_exposes_the_same_handle(self, fake_redis):
        """Other runtime-state helpers (e.g. the pre-post engagement marker) reuse this handle."""
        from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
        assert shared_redis_client() is fake_redis


class TestAutomationPause:
    def test_pause_sets_key_with_ttl(self, fake_redis):
        from cqc_lem.utilities.linkedin.rate_limit import pause_automation
        assert pause_automation(3600, reason="admin") is True
        fake_redis.set.assert_called_once_with("linkedin:automation_paused", "admin", ex=3600)

    def test_pause_floors_seconds_at_one(self, fake_redis):
        from cqc_lem.utilities.linkedin.rate_limit import pause_automation
        pause_automation(0)
        assert fake_redis.set.call_args.kwargs["ex"] == 1

    def test_pause_no_redis_returns_false(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.rate_limit import pause_automation
            assert pause_automation(60) is False

    def test_pause_logs_info_not_warning(self, fake_redis):
        """A stored pause is a state transition, not a degraded path (issue #917). Warning here made
        the once-per-release deploy pause escalate to ERROR and file a grouped $exception.
        """
        from cqc_lem.utilities.linkedin.rate_limit import pause_automation
        with patch(f"{_MOD}.log_info") as info, patch(f"{_MOD}.log_warning") as warn:
            assert pause_automation(1800, reason="deploy") is True
        warn.assert_not_called()
        info.assert_called_once()
        assert "Automation PAUSED for 1800s (reason: deploy)" in info.call_args.args[0]

    def test_pause_logs_the_reason_it_actually_stored(self, fake_redis):
        """An empty reason falls back to 'manual' — the log must not claim a blank one."""
        from cqc_lem.utilities.linkedin.rate_limit import pause_automation
        with patch(f"{_MOD}.log_info") as info:
            pause_automation(60, reason="")
        fake_redis.set.assert_called_once_with("linkedin:automation_paused", "manual", ex=60)
        assert "reason: manual" in info.call_args.args[0]

    def test_pause_redis_error_still_warns(self, fake_redis):
        """The kill-switch failing to store IS a degraded path — that one keeps its warning."""
        fake_redis.set.side_effect = RuntimeError("redis down")
        from cqc_lem.utilities.linkedin.rate_limit import pause_automation
        with patch(f"{_MOD}.log_warning") as warn, patch(f"{_MOD}.log_info") as info:
            assert pause_automation(60) is False
        info.assert_not_called()
        warn.assert_called_once()

    def test_resume_deletes_key(self, fake_redis):
        from cqc_lem.utilities.linkedin.rate_limit import resume_automation
        assert resume_automation() is True
        fake_redis.delete.assert_called_once_with("linkedin:automation_paused")

    def test_remaining_and_is_paused(self, fake_redis):
        fake_redis.ttl.return_value = 500
        from cqc_lem.utilities.linkedin.rate_limit import automation_pause_remaining, is_automation_paused
        assert automation_pause_remaining() == 500
        assert is_automation_paused() is True

    def test_not_paused_when_key_absent(self, fake_redis):
        fake_redis.ttl.return_value = -2
        from cqc_lem.utilities.linkedin.rate_limit import is_automation_paused
        assert is_automation_paused() is False

    def test_not_paused_when_no_redis(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.rate_limit import is_automation_paused
            assert is_automation_paused() is False


class TestAutomationPauseReason:
    """The reason lets maintenance mode lift only ITS OWN pause (issue #549)."""

    def test_returns_decoded_reason(self, fake_redis):
        fake_redis.get.return_value = b"deploy"
        from cqc_lem.utilities.linkedin.rate_limit import automation_pause_reason
        assert automation_pause_reason() == "deploy"

    def test_returns_str_value_unchanged(self, fake_redis):
        fake_redis.get.return_value = "429"
        from cqc_lem.utilities.linkedin.rate_limit import automation_pause_reason
        assert automation_pause_reason() == "429"

    def test_none_when_not_paused(self, fake_redis):
        fake_redis.get.return_value = None
        from cqc_lem.utilities.linkedin.rate_limit import automation_pause_reason
        assert automation_pause_reason() is None

    def test_none_when_no_redis(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.rate_limit import automation_pause_reason
            assert automation_pause_reason() is None

    def test_none_on_redis_error(self, fake_redis):
        fake_redis.get.side_effect = RuntimeError("boom")
        from cqc_lem.utilities.linkedin.rate_limit import automation_pause_reason
        assert automation_pause_reason() is None


class TestCommentingHold:
    """Issue #628: a per-user hold that stops ONLY feed commenting, so a demotion problem never
    takes posting, replies or DMs down with it. Fails OPEN like the rest of this module.
    """

    def test_hold_sets_key_with_ttl_and_reason(self, fake_redis):
        from cqc_lem.utilities.linkedin.rate_limit import hold_commenting
        assert hold_commenting(7, 3600, "demotion rate 0.6") is True
        fake_redis.set.assert_called_once_with("linkedin:comment_quality_hold:7",
                                               "demotion rate 0.6", ex=3600)

    def test_hold_floors_seconds_at_one(self, fake_redis):
        from cqc_lem.utilities.linkedin.rate_limit import hold_commenting
        hold_commenting(7, 0)
        assert fake_redis.set.call_args.kwargs["ex"] == 1

    def test_hold_falls_back_to_a_default_reason(self, fake_redis):
        from cqc_lem.utilities.linkedin.rate_limit import hold_commenting
        hold_commenting(7, 60, "")
        assert fake_redis.set.call_args.args[1] == "comment quality"

    def test_hold_without_redis_is_false(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.rate_limit import hold_commenting
            assert hold_commenting(7, 60) is False

    def test_hold_swallows_redis_error(self, fake_redis):
        fake_redis.set.side_effect = RuntimeError("boom")
        from cqc_lem.utilities.linkedin.rate_limit import hold_commenting
        assert hold_commenting(7, 60) is False

    def test_release_deletes_the_key(self, fake_redis):
        from cqc_lem.utilities.linkedin.rate_limit import release_commenting_hold
        assert release_commenting_hold(7) is True
        fake_redis.delete.assert_called_once_with("linkedin:comment_quality_hold:7")

    def test_release_without_redis_is_false(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.rate_limit import release_commenting_hold
            assert release_commenting_hold(7) is False

    def test_release_swallows_redis_error(self, fake_redis):
        fake_redis.delete.side_effect = RuntimeError("boom")
        from cqc_lem.utilities.linkedin.rate_limit import release_commenting_hold
        assert release_commenting_hold(7) is False

    def test_remaining_and_is_held(self, fake_redis):
        fake_redis.ttl.return_value = 120
        from cqc_lem.utilities.linkedin.rate_limit import commenting_hold_remaining, is_commenting_held
        assert commenting_hold_remaining(7) == 120
        assert is_commenting_held(7) is True

    def test_not_held_when_key_absent(self, fake_redis):
        fake_redis.ttl.return_value = -2
        from cqc_lem.utilities.linkedin.rate_limit import commenting_hold_remaining, is_commenting_held
        assert commenting_hold_remaining(7) == 0
        assert is_commenting_held(7) is False

    def test_not_held_when_no_redis(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.rate_limit import commenting_hold_remaining, is_commenting_held
            assert commenting_hold_remaining(7) == 0
            assert is_commenting_held(7) is False

    def test_not_held_on_redis_error(self, fake_redis):
        fake_redis.ttl.side_effect = RuntimeError("boom")
        from cqc_lem.utilities.linkedin.rate_limit import commenting_hold_remaining
        assert commenting_hold_remaining(7) == 0

    def test_reason_is_decoded(self, fake_redis):
        fake_redis.get.return_value = b"demotion rate 0.6"
        from cqc_lem.utilities.linkedin.rate_limit import commenting_hold_reason
        assert commenting_hold_reason(7) == "demotion rate 0.6"

    def test_reason_str_value_unchanged(self, fake_redis):
        fake_redis.get.return_value = "comment quality"
        from cqc_lem.utilities.linkedin.rate_limit import commenting_hold_reason
        assert commenting_hold_reason(7) == "comment quality"

    def test_reason_none_when_not_held(self, fake_redis):
        fake_redis.get.return_value = None
        from cqc_lem.utilities.linkedin.rate_limit import commenting_hold_reason
        assert commenting_hold_reason(7) is None

    def test_reason_none_when_no_redis(self):
        with patch(f"{_MOD}._redis_client", return_value=None):
            from cqc_lem.utilities.linkedin.rate_limit import commenting_hold_reason
            assert commenting_hold_reason(7) is None

    def test_reason_none_on_redis_error(self, fake_redis):
        fake_redis.get.side_effect = RuntimeError("boom")
        from cqc_lem.utilities.linkedin.rate_limit import commenting_hold_reason
        assert commenting_hold_reason(7) is None


class TestInviteHoldLogLevel:
    """A stored invite hold is a state transition, not a degraded path (issue #917 precedent).

    The Connect-dialog breakage that trips the miss-streak hold already warns and files its own
    grouped $exception, so warning here too filed a SECOND issue that re-fired while the route
    stayed broken.
    """

    def test_hold_logs_info_not_warning(self, fake_redis):
        from cqc_lem.utilities.linkedin.rate_limit import hold_invites
        with patch(f"{_MOD}.log_info") as info, patch(f"{_MOD}.log_warning") as warn:
            assert hold_invites(7, 3600, reason="weekly limit") is True
        # The hold must actually be STORED, not merely announced: without this a regression that
        # returns True while writing nothing would pass on the log-level assertions alone.
        fake_redis.set.assert_called_once_with("linkedin:invite_hold:7", "weekly limit", ex=3600)
        # log_warning is the ONLY door into log_escalation (logger.py), so "no warning" is the
        # assertion that no second grouped $exception can be filed for a working state transition.
        warn.assert_not_called()
        info.assert_called_once()
        assert "Connection invites HELD for user 7" in info.call_args.args[0]
        assert "weekly limit" in info.call_args.args[0]
        # A demotion is a plausible moment to drop the context that makes the event groupable.
        assert info.call_args.kwargs == {"action_type": "invite_connect", "user_id": 7}

    def test_hold_redis_error_still_warns(self, fake_redis):
        """A hold that failed to store IS a degraded path — that one keeps its warning."""
        fake_redis.set.side_effect = RuntimeError("redis down")
        from cqc_lem.utilities.linkedin.rate_limit import hold_invites
        with patch(f"{_MOD}.log_warning") as warn, patch(f"{_MOD}.log_info") as info:
            assert hold_invites(7, 3600) is False
        info.assert_not_called()
        warn.assert_called_once()
        # Pins the except branch as the source: only there is an exception attached. Without this
        # the test would still pass if the function returned False before ever reaching the write.
        assert isinstance(warn.call_args.kwargs.get("exc"), RuntimeError)
        assert warn.call_args.kwargs.get("user_id") == 7


class TestTheRedisHandleIsCachedPerProcess:
    """`Redis.from_url` builds its own ConnectionPool every call, and the pool disconnects when the
    object is collected — so re-deriving the handle per operation cost a TCP handshake per command,
    on paths that run per Selenium action.
    """

    @staticmethod
    def _fresh():
        from cqc_lem.utilities.linkedin import rate_limit
        rate_limit.reset_redis_client()
        return rate_limit

    def test_a_second_call_reuses_the_first_handle(self, monkeypatch):
        rate_limit = self._fresh()
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker:6379/0")
        client = MagicMock()
        with patch("redis.Redis.from_url", return_value=client) as from_url:
            first = rate_limit._redis_client()
            second = rate_limit._redis_client()
        assert first is second is client
        assert from_url.call_count == 1

    def test_a_changed_url_rebuilds(self, monkeypatch):
        rate_limit = self._fresh()
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://one:6379/0")
        with patch("redis.Redis.from_url", return_value=MagicMock()) as from_url:
            rate_limit._redis_client()
            monkeypatch.setenv("CELERY_BROKER_URL", "redis://two:6379/0")
            rate_limit._redis_client()
        assert from_url.call_count == 2
        assert from_url.call_args[0][0] == "redis://two:6379/0"

    def test_a_failure_is_never_cached(self, monkeypatch):
        """'Redis was down once' must not become 'the breaker is off for this worker's life'."""
        rate_limit = self._fresh()
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker:6379/0")
        with patch("redis.Redis.from_url", side_effect=ConnectionError("down")):
            assert rate_limit._redis_client() is None
        good = MagicMock()
        with patch("redis.Redis.from_url", return_value=good):
            assert rate_limit._redis_client() is good

    def test_a_forked_child_does_not_inherit_the_parents_handle(self, monkeypatch):
        """Celery prefork: a pool built before the fork would share a socket parent/child."""
        rate_limit = self._fresh()
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker:6379/0")
        real_pid = os.getpid()  # captured BEFORE patching, or the lambda calls itself
        with patch("redis.Redis.from_url", return_value=MagicMock()) as from_url:
            rate_limit._redis_client()
            monkeypatch.setattr(rate_limit.os, "getpid", lambda: real_pid + 1)
            rate_limit._redis_client()
        assert from_url.call_count == 2

    def test_content_generation_status_uses_the_same_handle(self, monkeypatch):
        """It used to carry a byte-for-byte copy of the URL resolution, free to drift."""
        rate_limit = self._fresh()
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker:6379/0")
        from cqc_lem.utilities import content_generation_status as cgs
        client = MagicMock()
        with patch("redis.Redis.from_url", return_value=client) as from_url:
            assert rate_limit._redis_client() is client
            assert cgs._redis_client() is client
        assert from_url.call_count == 1
