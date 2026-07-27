"""Unit tests for the weekly content-generation progress store (issue #545)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.content_generation_status"


class FakeRedis:
    """Minimal in-memory stand-in — just the get/set/delete the store uses."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def fake_redis():
    client = FakeRedis()
    with patch(f"{_MOD}._redis_client", return_value=client):
        yield client


@pytest.fixture
def no_redis():
    with patch(f"{_MOD}._redis_client", return_value=None):
        yield


class TestTtlSeconds:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("CONTENT_GENERATION_STATUS_TTL_SECONDS", raising=False)
        from cqc_lem.utilities.content_generation_status import _ttl_seconds
        assert _ttl_seconds() == 86400

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CONTENT_GENERATION_STATUS_TTL_SECONDS", "60")
        from cqc_lem.utilities.content_generation_status import _ttl_seconds
        assert _ttl_seconds() == 60

    def test_bad_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CONTENT_GENERATION_STATUS_TTL_SECONDS", "soon")
        from cqc_lem.utilities.content_generation_status import _ttl_seconds
        assert _ttl_seconds() == 86400


class TestResultTtlSeconds:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("CONTENT_GENERATION_RESULT_TTL_SECONDS", raising=False)
        from cqc_lem.utilities.content_generation_status import _result_ttl_seconds
        assert _result_ttl_seconds() == 3600

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CONTENT_GENERATION_RESULT_TTL_SECONDS", "30")
        from cqc_lem.utilities.content_generation_status import _result_ttl_seconds
        assert _result_ttl_seconds() == 30

    def test_bad_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CONTENT_GENERATION_RESULT_TTL_SECONDS", "later")
        from cqc_lem.utilities.content_generation_status import _result_ttl_seconds
        assert _result_ttl_seconds() == 3600


class TestRedisClient:
    def test_prefers_redis_broker_url(self, monkeypatch):
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker:6379/0")
        from cqc_lem.utilities.content_generation_status import _redis_client
        with patch("redis.Redis.from_url") as from_url:
            _redis_client()
        assert from_url.call_args[0][0] == "redis://broker:6379/0"

    def test_falls_back_to_result_backend_when_broker_not_redis(self, monkeypatch):
        monkeypatch.setenv("CELERY_BROKER_URL", "sqs://")
        monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://backend:6379/1")
        from cqc_lem.utilities.content_generation_status import _redis_client
        with patch("redis.Redis.from_url") as from_url:
            _redis_client()
        assert from_url.call_args[0][0] == "redis://backend:6379/1"

    def test_returns_none_when_connection_fails(self, monkeypatch):
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker:6379/0")
        from cqc_lem.utilities.content_generation_status import _redis_client
        with patch("redis.Redis.from_url", side_effect=ConnectionError("down")):
            assert _redis_client() is None


class TestLifecycle:
    def test_queued_then_in_progress_then_done(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import (
            ContentGenerationState, get_generation_status, mark_finished, mark_in_progress,
            mark_queued, record_post_generated)

        mark_queued(3)
        status = get_generation_status(3)
        assert status["state"] == ContentGenerationState.QUEUED
        assert status["total"] == 0
        assert status["started_at"]

        mark_in_progress(3, [11, 12])
        status = get_generation_status(3)
        assert status["state"] == ContentGenerationState.IN_PROGRESS
        assert status["total"] == 2
        assert status["post_ids"] == [11, 12]

        record_post_generated(3, 11)
        assert get_generation_status(3)["completed"] == 1

        record_post_generated(3, 12)
        final = mark_finished(3)
        assert final["state"] == ContentGenerationState.DONE
        assert final["completed"] == 2
        assert final["ready_post_ids"] == [11, 12]
        assert final["finished_at"]

    def test_in_progress_keeps_original_started_at(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import (
            get_generation_status, mark_in_progress, mark_queued)
        mark_queued(3)
        queued_at = get_generation_status(3)["started_at"]
        mark_in_progress(3, [1])
        assert get_generation_status(3)["started_at"] == queued_at

    def test_partial_failure_still_done(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import (
            ContentGenerationState, mark_finished, mark_in_progress, record_post_failed,
            record_post_generated)
        mark_in_progress(4, [1, 2])
        record_post_generated(4, 1)
        record_post_failed(4, 2)
        final = mark_finished(4)
        assert final["state"] == ContentGenerationState.DONE
        assert final["completed"] == 1
        assert final["failed"] == 1
        assert final["failed_post_ids"] == [2]

    def test_total_failure_is_failed(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import (
            ContentGenerationState, mark_finished, mark_in_progress, record_post_failed)
        mark_in_progress(5, [9])
        record_post_failed(5, 9)
        assert mark_finished(5)["state"] == ContentGenerationState.FAILED

    def test_empty_run_finishes_done(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import (
            ContentGenerationState, mark_finished, mark_in_progress)
        mark_in_progress(6, [])
        final = mark_finished(6)
        assert final["state"] == ContentGenerationState.DONE
        assert final["total"] == 0

    def test_recording_the_same_post_twice_counts_once(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import (
            get_generation_status, mark_in_progress, record_post_generated)
        mark_in_progress(7, [1])
        record_post_generated(7, 1)
        record_post_generated(7, 1)
        assert get_generation_status(7)["completed"] == 1

    def test_status_is_per_user(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import (
            get_generation_status, mark_in_progress)
        mark_in_progress(1, [10])
        mark_in_progress(2, [20, 21])
        assert get_generation_status(1)["total"] == 1
        assert get_generation_status(2)["total"] == 2

    def test_clear_removes_status(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import (
            clear_generation_status, get_generation_status, mark_queued)
        mark_queued(8)
        clear_generation_status(8)
        assert get_generation_status(8) is None

    def test_ttl_applied_on_write(self, fake_redis, monkeypatch):
        monkeypatch.setenv("CONTENT_GENERATION_STATUS_TTL_SECONDS", "120")
        from cqc_lem.utilities.content_generation_status import mark_queued
        mark_queued(9)
        assert fake_redis.ttls["content_generation:status:9"] == 120

    def test_finished_run_gets_the_shorter_result_ttl(self, fake_redis, monkeypatch):
        monkeypatch.setenv("CONTENT_GENERATION_STATUS_TTL_SECONDS", "120")
        monkeypatch.setenv("CONTENT_GENERATION_RESULT_TTL_SECONDS", "30")
        from cqc_lem.utilities.content_generation_status import mark_finished, mark_in_progress
        mark_in_progress(9, [1])
        assert fake_redis.ttls["content_generation:status:9"] == 120
        mark_finished(9)
        assert fake_redis.ttls["content_generation:status:9"] == 30


class TestMarkEmpty:
    """A run that generated nothing must say WHY, not just report 0 posts (issue #719)."""

    def test_records_reason_and_detail(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import (
            ContentGenerationEmptyReason, ContentGenerationState, get_generation_status,
            mark_empty, mark_queued)
        mark_queued(1)
        queued_at = get_generation_status(1)["started_at"]

        returned = mark_empty(1, ContentGenerationEmptyReason.NO_PLANNED_SLOTS,
                              next_planned_at=datetime(2026, 8, 3, 13, 30), buffer_days=5)
        status = get_generation_status(1)
        assert returned == status
        assert status["state"] == ContentGenerationState.DONE
        assert status["total"] == 0 and status["completed"] == 0 and status["failed"] == 0
        assert status["reason"] == "no_planned_slots"
        assert status["reason_detail"]["next_planned_at"] == "2026-08-03T13:30:00+00:00"
        assert status["reason_detail"]["buffer_days"] == 5
        assert status["started_at"] == queued_at  # same run the SPA has been polling
        assert status["finished_at"]

    def test_buffer_full_carries_the_counts(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import (
            ContentGenerationEmptyReason, get_generation_status, mark_empty)
        mark_empty(2, ContentGenerationEmptyReason.BUFFER_FULL, buffer_days=5,
                   ready_count=5, buffer_max=5)
        detail = get_generation_status(2)["reason_detail"]
        assert detail == {"buffer_days": 5, "ready_count": 5, "buffer_max": 5}

    def test_unknown_values_are_omitted_not_zeroed(self, fake_redis):
        """None means 'we don't know', which must never render as 0 posts ready."""
        from cqc_lem.utilities.content_generation_status import (
            ContentGenerationEmptyReason, get_generation_status, mark_empty)
        mark_empty(3, ContentGenerationEmptyReason.ALREADY_RUNNING)
        assert get_generation_status(3)["reason_detail"] == {}

    def test_aware_next_planned_is_normalized_to_utc(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import (
            ContentGenerationEmptyReason, get_generation_status, mark_empty)
        aware = datetime(2026, 8, 3, 9, 0, tzinfo=timezone(timedelta(hours=-4)))
        mark_empty(4, ContentGenerationEmptyReason.BUFFER_FULL, next_planned_at=aware)
        assert get_generation_status(4)["reason_detail"]["next_planned_at"] == \
            "2026-08-03T13:00:00+00:00"

    def test_gets_the_shorter_result_ttl(self, fake_redis, monkeypatch):
        monkeypatch.setenv("CONTENT_GENERATION_RESULT_TTL_SECONDS", "30")
        from cqc_lem.utilities.content_generation_status import (
            ContentGenerationEmptyReason, mark_empty)
        mark_empty(5, ContentGenerationEmptyReason.BUFFER_FULL)
        assert fake_redis.ttls["content_generation:status:5"] == 30

    def test_noops_without_redis(self, no_redis):
        from cqc_lem.utilities.content_generation_status import (
            ContentGenerationEmptyReason, mark_empty)
        assert mark_empty(1, ContentGenerationEmptyReason.BUFFER_FULL) is None


class TestFailsOpen:
    def test_all_writes_noop_without_redis(self, no_redis):
        from cqc_lem.utilities.content_generation_status import (
            clear_generation_status, get_generation_status, mark_finished, mark_in_progress,
            mark_queued, record_post_failed, record_post_generated)
        mark_queued(1)
        mark_in_progress(1, [1, 2])
        record_post_generated(1, 1)
        record_post_failed(1, 2)
        clear_generation_status(1)
        assert mark_finished(1) is None
        assert get_generation_status(1) is None

    def test_record_and_finish_ignore_missing_status(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import (
            get_generation_status, mark_finished, record_post_generated)
        record_post_generated(2, 5)
        assert get_generation_status(2) is None
        assert mark_finished(2) is None

    def test_unreadable_payload_is_treated_as_missing(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import get_generation_status
        fake_redis.store["content_generation:status:3"] = "not json"
        assert get_generation_status(3) is None

    def test_non_dict_payload_is_treated_as_missing(self, fake_redis):
        from cqc_lem.utilities.content_generation_status import get_generation_status
        fake_redis.store["content_generation:status:3"] = json.dumps([1, 2])
        assert get_generation_status(3) is None

    def test_read_error_is_swallowed(self):
        client = MagicMock()
        client.get.side_effect = RuntimeError("redis exploded")
        with patch(f"{_MOD}._redis_client", return_value=client):
            from cqc_lem.utilities.content_generation_status import get_generation_status
            assert get_generation_status(1) is None

    def test_write_error_is_swallowed(self):
        client = MagicMock()
        client.set.side_effect = RuntimeError("redis exploded")
        with patch(f"{_MOD}._redis_client", return_value=client):
            from cqc_lem.utilities.content_generation_status import mark_queued
            mark_queued(1)  # must not raise

    def test_delete_error_is_swallowed(self):
        client = MagicMock()
        client.delete.side_effect = RuntimeError("redis exploded")
        with patch(f"{_MOD}._redis_client", return_value=client):
            from cqc_lem.utilities.content_generation_status import clear_generation_status
            clear_generation_status(1)  # must not raise
