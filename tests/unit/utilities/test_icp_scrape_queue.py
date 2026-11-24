"""Hold-for-scrape queue: window, daily cap, attempt markers (issue #2655)."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from cqc_lem.utilities import icp_scrape_queue as q

pytestmark = pytest.mark.unit

_URL = "https://www.linkedin.com/in/jane-doe/"


class FakeRedis:
    def __init__(self):
        self.sets, self.kv = {}, {}

    def exists(self, key):
        return key in self.kv

    def sadd(self, key, value):
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.add(value)
        return len(bucket) - before

    def spop(self, key):
        bucket = self.sets.get(key) or set()
        return bucket.pop() if bucket else None

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = value

    def incrby(self, key, n):
        self.kv[key] = int(self.kv.get(key) or 0) + n

    def decrby(self, key, n):
        self.kv[key] = int(self.kv.get(key) or 0) - n

    def expire(self, *_):
        pass

    def delete(self, key):
        self.kv.pop(key, None)


@pytest.fixture
def redis():
    fake = FakeRedis()
    with patch.object(q, "shared_redis_client", return_value=fake):
        yield fake


class TestWindow:
    def test_weekday_midday_is_open(self):
        assert q.in_scrape_window(datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc))

    @pytest.mark.parametrize("hour", [0, 6, 8, 17, 19, 23])
    def test_night_and_edges_are_closed(self, hour):
        assert not q.in_scrape_window(datetime(2026, 11, 24, hour, 0, tzinfo=timezone.utc))

    def test_weekend_is_closed(self):
        assert not q.in_scrape_window(datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc))

    def test_uses_the_user_timezone(self):
        # 12:00 UTC is 04:00 in Los Angeles: closed there, open in UTC.
        now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        assert not q.in_scrape_window(now, "America/Los_Angeles")

    def test_unknown_timezone_falls_back_to_utc(self):
        assert q.in_scrape_window(datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc), "Not/AZone")


class TestCap:
    def test_default_and_env(self, monkeypatch):
        monkeypatch.delenv("ICP_SCRAPE_DAILY_CAP", raising=False)
        assert q.daily_cap() == q.DEFAULT_DAILY_CAP
        monkeypatch.setenv("ICP_SCRAPE_DAILY_CAP", "3")
        assert q.daily_cap() == 3
        monkeypatch.setenv("ICP_SCRAPE_DAILY_CAP", "junk")
        assert q.daily_cap() == q.DEFAULT_DAILY_CAP


class TestQueue:
    def test_enqueue_then_claim_respects_cap(self, redis):
        urls = [f"https://www.linkedin.com/in/p{i}/" for i in range(5)]
        assert q.enqueue_held(1, urls) == 5
        now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
        assert len(q.claim_batch(1, now, cap=3)) == 3
        assert q.claim_batch(1, now, cap=3) == []  # budget spent today
        assert len(q.claim_batch(1, datetime(2026, 11, 25, 12, tzinfo=timezone.utc), cap=3)) == 2

    def test_attempted_profile_is_not_requeued(self, redis):
        q.enqueue_held(1, [_URL])
        assert q.claim_batch(1, datetime(2026, 11, 24, 12, tzinfo=timezone.utc), cap=5) == [_URL]
        assert q.enqueue_held(1, [_URL]) == 0

    def test_requeue_refunds_budget_and_attempt(self, redis):
        q.enqueue_held(1, [_URL])
        now = datetime.now(timezone.utc)
        q.claim_batch(1, now, cap=1)
        q.requeue(1, [_URL])
        assert q.claim_batch(1, now, cap=1) == [_URL]

    def test_zero_cap_claims_nothing(self, redis):
        q.enqueue_held(1, [_URL])
        assert q.claim_batch(1, datetime.now(timezone.utc), cap=0) == []

    def test_no_redis_fails_closed(self):
        with patch.object(q, "shared_redis_client", return_value=None):
            assert q.enqueue_held(1, [_URL]) == 0
            assert q.claim_batch(1, datetime.now(timezone.utc), cap=5) == []
            q.requeue(1, [_URL])  # no raise

    def test_redis_error_claims_nothing(self, redis):
        with patch.object(redis, "get", side_effect=RuntimeError("down")):
            assert q.claim_batch(1, datetime.now(timezone.utc), cap=5) == []
