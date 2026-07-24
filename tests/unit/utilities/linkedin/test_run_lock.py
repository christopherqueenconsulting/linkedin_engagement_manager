"""Unit tests for the single-flight run lock — issue #474 (feed-commenting concurrency gate)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

MOD = "cqc_lem.utilities.linkedin.rate_limit"


class TestAcquireRunLock:
    def test_acquires_when_free(self):
        from cqc_lem.utilities.linkedin.rate_limit import acquire_run_lock
        client = MagicMock()
        client.set.return_value = True
        with patch(f"{MOD}._redis_client", return_value=client):
            token = acquire_run_lock("automate_commenting:1", ttl_seconds=1800)
        assert token and token not in ("no-redis", "lock-error")
        # NX + EX so it's atomic and self-expiring
        _, kwargs = client.set.call_args
        assert kwargs.get("nx") is True and kwargs.get("ex") == 1800

    def test_returns_none_when_held(self):
        from cqc_lem.utilities.linkedin.rate_limit import acquire_run_lock
        client = MagicMock()
        client.set.return_value = None  # NX failed — someone else holds it
        with patch(f"{MOD}._redis_client", return_value=client):
            assert acquire_run_lock("automate_commenting:1") is None

    def test_fails_open_without_redis(self):
        from cqc_lem.utilities.linkedin.rate_limit import acquire_run_lock
        with patch(f"{MOD}._redis_client", return_value=None):
            assert acquire_run_lock("x") == "no-redis"

    def test_fails_open_on_redis_error(self):
        from cqc_lem.utilities.linkedin.rate_limit import acquire_run_lock
        client = MagicMock()
        client.set.side_effect = RuntimeError("boom")
        with patch(f"{MOD}._redis_client", return_value=client):
            assert acquire_run_lock("x") == "lock-error"


class TestReleaseRunLock:
    def test_deletes_only_when_token_matches(self):
        from cqc_lem.utilities.linkedin.rate_limit import release_run_lock
        client = MagicMock()
        client.get.return_value = b"tok-123"
        with patch(f"{MOD}._redis_client", return_value=client):
            release_run_lock("job", "tok-123")
        client.delete.assert_called_once()

    def test_does_not_delete_another_holders_lock(self):
        from cqc_lem.utilities.linkedin.rate_limit import release_run_lock
        client = MagicMock()
        client.get.return_value = b"someone-else"
        with patch(f"{MOD}._redis_client", return_value=client):
            release_run_lock("job", "tok-123")
        client.delete.assert_not_called()

    def test_noop_for_failopen_sentinels(self):
        from cqc_lem.utilities.linkedin.rate_limit import release_run_lock
        client = MagicMock()
        with patch(f"{MOD}._redis_client", return_value=client):
            release_run_lock("job", "no-redis")
            release_run_lock("job", None)
        client.get.assert_not_called()
        client.delete.assert_not_called()
