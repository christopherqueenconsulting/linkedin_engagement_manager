"""The soft daily AI spend cap for free-trial users (issue #2378).

Covers the acceptance list — under the cap, at the cap (paused + event), the reset at the UTC day
boundary, a non-trial user being unaffected, an unreadable spend allowing generation and warning
ONCE — plus the wiring that makes it real: spend accrues from `track_llm_call`, the shared client
refuses before anything is sent, an admitted pipeline finishes, and a refusal is never filed as an
error-tracking issue.
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

from cqc_lem.utilities import ai_spend_cap as cap

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.ai_spend_cap"
_DAY1 = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
_DAY1_LATE = datetime(2026, 10, 10, 23, 59, 59, tzinfo=timezone.utc)
_DAY2 = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)


class FakeRedis:
    """Just the commands the cap and the cost rollup use."""

    def __init__(self):
        self.kv: dict = {}
        self.hashes: dict = {}

    def incrbyfloat(self, key, amount):
        self.kv[key] = float(self.kv.get(key, 0.0)) + float(amount)
        return self.kv[key]

    def hincrbyfloat(self, key, field, amount):
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0.0) + float(amount)

    def expire(self, key, ttl):
        return True

    def get(self, key):
        value = self.kv.get(key)
        return None if value is None else str(value).encode()

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv(cap.CAP_ENV, raising=False)
    cap._TIER_CACHE.clear()
    cap._UNKNOWN_WARNED.clear()
    cap._REACHED_LOCAL.clear()
    cap._BAD_CAP_WARNED.clear()
    yield
    cap._TIER_CACHE.clear()
    cap._UNKNOWN_WARNED.clear()
    cap._REACHED_LOCAL.clear()


@pytest.fixture
def redis():
    fake = FakeRedis()
    with patch(f"{_MOD}._redis", return_value=fake):
        yield fake


def _tier(tier):
    return patch("cqc_lem.utilities.db.get_user_subscription_info",
                 return_value={"subscription_tier": tier, "subscription_status": "trial"})


@pytest.fixture
def trial():
    with _tier("free_trial") as info:
        yield info


@pytest.fixture
def event():
    with patch("cqc_lem.utilities.observability.track_ai_daily_cap_reached") as tracked:
        yield tracked


class TestCapValue:
    def test_owner_set_default(self):
        assert cap.daily_cap_usd() == 0.50

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv(cap.CAP_ENV, "1.25")
        assert cap.daily_cap_usd() == 1.25

    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_zero_or_below_turns_the_cap_off(self, monkeypatch, raw):
        monkeypatch.setenv(cap.CAP_ENV, raw)
        assert cap.daily_cap_usd() is None

    def test_a_typo_keeps_the_default_rather_than_removing_the_control(self, monkeypatch):
        monkeypatch.setenv(cap.CAP_ENV, "fifty cents")
        with patch(f"{_MOD}.log_warning") as warn:
            assert cap.daily_cap_usd() == cap.DEFAULT_CAP_USD
            assert cap.daily_cap_usd() == cap.DEFAULT_CAP_USD
        warn.assert_called_once()


class TestAcceptance:
    def test_under_the_cap_is_not_paused(self, redis, trial, event):
        cap.record_user_spend(7, 0.20, now=_DAY1)
        assert cap.cap_status(7, now=_DAY1)[0] == cap.STATUS_UNDER
        assert cap.generation_paused(7, now=_DAY1) is False
        event.assert_not_called()

    def test_at_the_cap_pauses_and_records_the_transition_once(self, redis, trial, event):
        cap.record_user_spend(7, 0.30, now=_DAY1)
        cap.record_user_spend(7, 0.20, now=_DAY1)  # exactly the $0.50 default
        with patch(f"{_MOD}.log_info") as info:
            assert cap.generation_paused(7, surface="background", now=_DAY1) is True
            assert cap.generation_paused(7, surface="background", now=_DAY1) is True
        event.assert_called_once()
        kwargs = event.call_args.kwargs
        assert kwargs["user_id"] == 7
        assert kwargs["spend_usd"] == pytest.approx(0.50)
        assert kwargs["cap_usd"] == 0.50
        assert kwargs["day"] == "2026-10-10"
        assert kwargs["surface"] == "background"
        info.assert_called_once()

    def test_enforce_raises_with_the_reset_time(self, redis, trial, event):
        cap.record_user_spend(7, 0.75, now=_DAY1)
        with pytest.raises(cap.DailyAICapReached) as refused:
            cap.enforce(7, now=_DAY1)
        assert refused.value.resets_at == _DAY2
        assert refused.value.expected_refusal is True
        assert "Daily AI limit reached" in str(refused.value)

    def test_resets_at_the_utc_day_boundary(self, redis, trial, event):
        cap.record_user_spend(7, 0.60, now=_DAY1_LATE)
        assert cap.generation_paused(7, now=_DAY1_LATE) is True
        assert cap.generation_paused(7, now=_DAY2) is False
        assert cap.cap_status(7, now=_DAY2) == (cap.STATUS_UNDER, 0.0, 0.50)
        assert cap.next_reset(_DAY1_LATE) == _DAY2

    def test_a_non_trial_user_is_unaffected(self, redis, event):
        cap.record_user_spend(8, 5.00, now=_DAY1)
        with _tier("professional"):
            assert cap.cap_status(8, now=_DAY1)[0] == cap.STATUS_EXEMPT
            assert cap.generation_paused(8, now=_DAY1) is False
            cap.enforce(8, now=_DAY1)
        event.assert_not_called()

    def test_unreadable_spend_allows_and_warns_once(self, trial, event):
        with patch(f"{_MOD}._redis", return_value=None), patch(f"{_MOD}.log_warning") as warn:
            for _ in range(5):
                assert cap.generation_paused(7, now=_DAY1) is False
                cap.enforce(7, now=_DAY1)
            assert cap.cap_status(7, now=_DAY1)[0] == cap.STATUS_UNKNOWN
        warn.assert_called_once()
        event.assert_not_called()

    def test_a_redis_error_reads_as_unknown_not_as_zero(self, trial):
        class Broken(FakeRedis):
            def get(self, key):
                raise ConnectionError("down")
        with patch(f"{_MOD}._redis", return_value=Broken()), patch(f"{_MOD}.log_warning"):
            assert cap.cap_status(7, now=_DAY1)[0] == cap.STATUS_UNKNOWN


class TestExemptions:
    def test_no_user_or_the_system_sentinel_is_never_capped(self, redis):
        assert cap.cap_status(None)[0] == cap.STATUS_EXEMPT
        assert cap.cap_status("system")[0] == cap.STATUS_EXEMPT

    def test_an_unreadable_tier_is_not_evidence_of_a_trial(self, redis):
        cap.record_user_spend(7, 9.0, now=_DAY1)
        with patch("cqc_lem.utilities.db.get_user_subscription_info", return_value=None):
            assert cap.generation_paused(7, now=_DAY1) is False

    def test_a_disabled_cap_pauses_nobody(self, redis, trial, monkeypatch):
        monkeypatch.setenv(cap.CAP_ENV, "0")
        cap.record_user_spend(7, 9.0, now=_DAY1)
        assert cap.generation_paused(7, now=_DAY1) is False

    @pytest.mark.parametrize("model", sorted(cap.EXEMPT_MODEL_TIERS))
    def test_essential_tiers_pass_the_request_gate(self, redis, trial, event, model):
        cap.record_user_spend(7, 9.0)
        cap.check_request(7, model)

    def test_a_generative_tier_is_refused(self, redis, trial, event):
        cap.record_user_spend(7, 9.0)
        with pytest.raises(cap.DailyAICapReached):
            cap.check_request(7, "lem-complex")

    def test_an_admitted_pipeline_finishes_after_crossing_the_cap(self, redis, trial, event):
        with cap.admit_pipeline(7):
            cap.record_user_spend(7, 9.0)
            cap.check_request(7, "lem-complex")  # mid-pipeline: allowed
        with pytest.raises(cap.DailyAICapReached):
            cap.check_request(7, "lem-complex")  # the next pipeline is not
        with pytest.raises(cap.DailyAICapReached):
            with cap.admit_pipeline(7):
                pass

    def test_tier_reads_are_cached(self, redis):
        with _tier("free_trial") as info:
            cap.cap_status(7)
            cap.cap_status(7)
        info.assert_called_once_with(7)


class TestSpendIsReadFromTrackLlmCall:
    def test_track_llm_call_accrues_the_per_user_day_total(self, redis):
        from cqc_lem.utilities import observability
        with patch("cqc_lem.utilities.linkedin.rate_limit._redis_client", return_value=redis), \
                patch.object(observability, "_emit"):
            observability.track_llm_call("lem-complex", 10, 10, 5, user_id=7, response_cost=0.3)
            observability.track_llm_call("lem-complex", 10, 10, 5, user_id=7, response_cost=0.2)
            observability.track_llm_call("lem-complex", 10, 10, 5, user_id=None, response_cost=1.0)
        assert cap._read_spend(7, cap.utc_day()) == pytest.approx(0.5)

    def test_a_cache_hit_costs_the_cap_nothing(self, redis):
        from cqc_lem.utilities import observability
        with patch("cqc_lem.utilities.linkedin.rate_limit._redis_client", return_value=redis), \
                patch.object(observability, "_emit"):
            observability.track_llm_call("lem-complex", 10, 10, 5, user_id=7, cached=True)
        assert cap._read_spend(7, cap.utc_day()) == 0.0


class TestEvent:
    def test_the_event_is_registered_and_its_breakdowns_are_strings(self):
        from cqc_lem.utilities import observability
        with patch.object(observability.posthog, "capture") as capture, \
                patch.object(observability.posthog, "disabled", False), \
                patch.object(observability, "telemetry_muted", return_value=False):
            observability.track_ai_daily_cap_reached(user_id=7, spend_usd=0.51, cap_usd=0.5,
                                                     day="2026-10-10", surface="background")
        assert "ai_daily_cap_reached" in observability.EVENTS
        props = capture.call_args.kwargs["properties"]
        assert props["surface"] == "background" and props["day"] == "2026-10-10"
        assert props["cap_usd"] == 0.5 and props["user_id"] == 7

    def test_a_refusal_is_never_filed_as_an_error_tracking_issue(self):
        from cqc_lem.utilities import observability
        exc = cap.DailyAICapReached(7, 0.5, 0.5, _DAY2)
        with patch.object(observability.posthog, "capture_exception") as captured, \
                patch.object(observability.posthog, "disabled", False), \
                patch.object(observability, "telemetry_muted", return_value=False):
            observability.capture_exception(exc, user_id=7)
        captured.assert_not_called()


_CHAT_RESPONSE = {
    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                 "finish_reason": "stop"}],
}


class _Recorder(httpx.BaseTransport):
    def __init__(self):
        self.bodies = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_CHAT_RESPONSE)


def _client():
    from cqc_lem.utilities.ai.client import AttributedOpenAI
    recorder = _Recorder()
    return AttributedOpenAI(api_key="k", base_url="http://litellm:4000", max_retries=0,
                            http_client=httpx.Client(transport=recorder)), recorder


def _chat(client, model="lem-complex", **kwargs):
    return client.chat.completions.create(model=model, messages=[{"role": "user", "content": "hi"}],
                                          **kwargs)


class TestSharedClientGate:
    def test_a_capped_user_is_refused_before_anything_is_sent(self, redis, trial, event):
        cap.record_user_spend(7, 9.0)
        client, recorder = _client()
        with patch("cqc_lem.utilities.observability.current_llm_attribution",
                   return_value=(7, "content")):
            with pytest.raises(cap.DailyAICapReached):
                _chat(client)
        assert recorder.bodies == []

    def test_the_user_an_explicit_caller_stamped_wins(self, redis, trial, event):
        cap.record_user_spend(7, 9.0)
        client, recorder = _client()
        with patch("cqc_lem.utilities.observability.current_llm_attribution",
                   return_value=(None, None)):
            with pytest.raises(cap.DailyAICapReached):
                _chat(client, extra_body={"metadata": {"user_id": "7", "feature": "content"}})
        assert recorder.bodies == []

    def test_under_the_cap_the_call_goes_out(self, redis, trial):
        client, recorder = _client()
        with patch("cqc_lem.utilities.observability.current_llm_attribution",
                   return_value=(7, "content")):
            _chat(client)
        assert len(recorder.bodies) == 1

    def test_a_broken_gate_fails_open(self):
        client, recorder = _client()
        with patch(f"{_MOD}.check_request", side_effect=RuntimeError("boom")):
            _chat(client)
        assert len(recorder.bodies) == 1

    def test_a_pipeline_is_refused_at_entry_and_an_admitted_one_finishes(self, redis, trial, event):
        from cqc_lem.utilities.observability import llm_trace
        client, recorder = _client()
        with llm_trace("post", user_id=7, feature="content"):
            _chat(client)
            cap.record_user_spend(7, 9.0)  # the cap is crossed part-way
            _chat(client)
        assert len(recorder.bodies) == 2
        with pytest.raises(cap.DailyAICapReached):
            with llm_trace("post", user_id=7, feature="content"):
                _chat(client)
        assert len(recorder.bodies) == 2


class TestCallLlm:
    def test_a_refusal_is_not_logged_or_counted_as_a_failed_call(self, redis, trial, event):
        from cqc_lem.utilities.ai import ai_helper
        refusal = cap.DailyAICapReached(7, 0.5, 0.5, _DAY2)
        with patch.object(ai_helper.client.chat.completions, "create", side_effect=refusal), \
                patch.object(ai_helper, "log_error") as logged, \
                patch("cqc_lem.utilities.observability.track_llm_call") as tracked:
            with pytest.raises(cap.DailyAICapReached):
                ai_helper._call_llm(model="lem-simple", messages=[], _track_user_id=7)
        logged.assert_not_called()
        tracked.assert_not_called()


class TestApiAnswer:
    def test_a_user_request_gets_a_clear_429(self):
        from cqc_lem.api.main import daily_ai_cap_handler
        resets = datetime.now(timezone.utc).replace(microsecond=0)
        resets = cap.next_reset(resets)
        response = asyncio.run(daily_ai_cap_handler(None, cap.DailyAICapReached(7, 0.5, 0.5, resets)))
        assert response.status_code == 429
        body = json.loads(response.body)
        assert body["reason"] == "daily_ai_limit"
        assert body["detail"].startswith("Daily AI limit reached")
        assert 0 < int(response.headers["Retry-After"]) <= 86400


class TestMediaSpendCounts:
    def test_a_provider_render_counts_toward_the_cap(self, redis):
        from cqc_lem.utilities import observability
        with patch.object(observability, "_emit"), patch.object(observability, "_write_cost_ledger"):
            observability.track_media_cost("image", "openai", 0.21, user_id=7)
            observability.track_media_cost("video", "runway", 0.25, user_id=7)
        assert cap._read_spend(7, cap.utc_day()) == pytest.approx(0.46)

    def test_local_compute_is_not_ai_spend(self, redis):
        from cqc_lem.utilities import observability
        with patch.object(observability, "_emit"), patch.object(observability, "_write_cost_ledger"):
            observability.track_media_cost("video", "local-ffmpeg", 0.01, user_id=7)
        assert cap._read_spend(7, cap.utc_day()) == 0.0

    def test_a_render_from_the_attribution_scope_counts_too(self, redis):
        from cqc_lem.utilities import observability
        with patch.object(observability, "_emit"), patch.object(observability, "_write_cost_ledger"), \
                observability.llm_attribution(user_id=9):
            observability.track_media_cost("image", "replicate", 0.04)
        assert cap._read_spend(9, cap.utc_day()) == pytest.approx(0.04)


class TestTierReadFailure:
    def test_a_failed_tier_read_is_cached_briefly(self, redis):
        with patch("cqc_lem.utilities.db.get_user_subscription_info", return_value=None) as info:
            for _ in range(5):
                assert cap.cap_status(7)[0] == cap.STATUS_EXEMPT
        info.assert_called_once_with(7)

    def test_and_is_read_again_after_the_short_window(self, redis):
        later = 100.0 + cap._TIER_FAILURE_TTL_SECONDS + 1
        with patch("cqc_lem.utilities.db.get_user_subscription_info", return_value=None) as info, \
                patch(f"{_MOD}.time.monotonic", side_effect=[100.0, later]):
            cap.cap_status(7)
            cap.cap_status(7)
        assert info.call_count == 2


class TestCeleryOutcome:
    """A task the cap stops is a designed stop — `no_op`, never Celery FAILURE (task_outcome.py)."""

    def test_a_refused_task_ends_success_and_postrun_records_success(self):
        from cqc_lem.app import my_celery

        @my_celery.app.task(name="tests.ai_spend_cap.refused_task")
        def refused_task(user_id: int):
            raise cap.DailyAICapReached(user_id, 0.6, 0.5, _DAY2)

        with patch.object(my_celery, "track_task") as tracked, \
                patch.object(my_celery, "capture_exception") as captured:
            result = refused_task.apply(kwargs={"user_id": 7})
        assert result.state == "SUCCESS"
        assert "Paused by the free-trial daily AI cap" in result.result
        assert tracked.call_args.kwargs["state"] == "SUCCESS"
        assert tracked.call_args.kwargs["success"] is True
        captured.assert_not_called()

    def test_any_other_exception_still_fails_the_task(self):
        from cqc_lem.app import my_celery

        @my_celery.app.task(name="tests.ai_spend_cap.broken_task")
        def broken_task():
            raise RuntimeError("a real fault")

        with patch.object(my_celery, "track_task") as tracked, \
                patch.object(my_celery, "capture_exception"):
            result = broken_task.apply()
        assert result.state == "FAILURE"
        assert tracked.call_args.kwargs["state"] == "FAILURE"

    def test_queue_once_tasks_get_the_same_boundary(self):
        from cqc_lem.app.queue_once import QueueOnce
        from cqc_lem.app.task_outcome import DailyCapAwareTask
        assert issubclass(QueueOnce, DailyCapAwareTask)

    def test_every_registered_lem_task_has_the_boundary(self):
        from cqc_lem.app import my_celery
        from cqc_lem.app.task_outcome import DailyCapAwareTask
        my_celery.app.loader.import_default_modules()
        lem_tasks = {name: task for name, task in my_celery.app.tasks.items()
                     if name.startswith("cqc_lem.")}
        assert lem_tasks, "no LEM task registered — the check would pass vacuously"
        assert [n for n, t in lem_tasks.items() if not isinstance(t, DailyCapAwareTask)] == []
