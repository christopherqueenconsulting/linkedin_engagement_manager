"""Integration test for the suppression-tripwire endpoints (issue #629).

Drives the real GET /api/user/automation-status and POST /api/user/automation-resume handlers
through the real db.py SQL, the real `build_engagement_trend`/`evaluate_suppression` chain and a
stateful fake Redis standing in for the breaker store — so the whole stack that turns raw
`post_stats` rows into "we paused your automation, here's why" is exercised end to end, and the
re-enable button provably clears the Redis record it claims to.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_M = "cqc_lem.api.main"
_RL = "cqc_lem.utilities.linkedin.rate_limit"
_START = datetime(2026, 7, 1, 9, 0)


class _FakeCursor:
    """Cursor over an in-memory posts/post_stats join — enough for the two SELECTs the endpoint runs."""

    def __init__(self, rows, dictionary=False):
        self.rows = rows
        self.dictionary = dictionary
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT p.id, p.scheduled_time"):
            self._result = list(self.rows)
        elif "FROM comment_outcomes" in s:
            self._result = []
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self, dictionary=False, **kw):
        return _FakeCursor(self.rows, dictionary=dictionary)

    def commit(self):
        pass

    def close(self):
        pass


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, **kw):
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)
        return True

    def ttl(self, key):
        return 1000 if key in self.store else -2


def _stat_row(offset: int, impressions: int) -> tuple:
    # (post_id, scheduled_time, reactions, comments, reposts, impressions, saves, + attribution)
    return (100 + offset, _START + timedelta(days=offset), 10, 1, 0, impressions, 0,
            None, None, None, None, None)


def _history(baseline: int, recent: int) -> list:
    return ([_stat_row(i, baseline) for i in range(10)]
            + [_stat_row(10 + i, recent) for i in range(3)])


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from cqc_lem.api.main import app
    return TestClient(app, raise_server_exceptions=False)


def _run(client, rows, redis, method="get"):
    with patch(f"{_M}.get_session_user_id", return_value=42), \
         patch("cqc_lem.utilities.db.get_db_connection", return_value=_FakeConnection(rows)), \
         patch(f"{_RL}._redis_client", return_value=redis):
        if method == "get":
            response = client.get("/api/user/automation-status?session_token=t")
        else:
            response = client.post("/api/user/automation-resume", json={"session_token": "t"})
    assert response.status_code == 200
    return response.json()["detail"]


def test_healthy_history_reports_no_trip(client):
    detail = _run(client, _history(8500, 8400), _FakeRedis())
    assert detail["tripped"] is False
    assert detail["current"]["status"] == "ok"
    assert detail["engagement_paused"] is False


def test_a_collapse_is_detected_from_raw_post_stats(client):
    detail = _run(client, _history(8500, 340), _FakeRedis())
    assert detail["current"]["tripped"] is True
    reach = next(s for s in detail["current"]["signals"] if s["name"] == "reach_collapse")
    assert reach["baseline"] == 8500


def test_re_enabling_clears_the_trip_and_lifts_only_our_pause(client):
    from cqc_lem.utilities.linkedin import rate_limit as rl
    redis = _FakeRedis()
    with patch(f"{_RL}._redis_client", return_value=redis):
        rl.record_suppression_trip(42, "reach collapse", detail={"status": "tripped"})
        rl.pause_automation(600, reason=rl.suppression_pause_reason(42))

    tripped = _run(client, _history(8500, 340), redis)
    assert tripped["tripped"] is True and tripped["pause_by_tripwire"] is True

    resumed = _run(client, _history(8500, 8400), redis, method="post")
    assert resumed["cleared"] is True and resumed["resumed"] is True
    assert resumed["tripped"] is False
    assert "linkedin:suppression_trip:42" not in redis.store
    assert "linkedin:automation_paused" not in redis.store


def test_re_enabling_leaves_a_foreign_pause_alone(client):
    from cqc_lem.utilities.linkedin import rate_limit as rl
    redis = _FakeRedis()
    with patch(f"{_RL}._redis_client", return_value=redis):
        rl.record_suppression_trip(42, "reach collapse")
        rl.pause_automation(600, reason="maintenance")

    detail = _run(client, _history(8500, 8400), redis, method="post")
    assert detail["cleared"] is True and detail["resumed"] is False
    assert redis.store["linkedin:automation_paused"] == "maintenance"


def test_requires_a_valid_session(client):
    with patch(f"{_M}.get_session_user_id", return_value=None):
        assert client.get("/api/user/automation-status?session_token=x").status_code == 401
        assert client.post("/api/user/automation-resume",
                           json={"session_token": "x"}).status_code == 401
