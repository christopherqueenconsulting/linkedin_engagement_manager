"""Auth endpoint rate limiting (issue #745, phase 2b).

The two properties that matter: the counters actually bound a walk of the PIN space, and a Redis
outage does NOT stop people signing in.
"""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.auth_rate_limit"


class FakeRedis:
    """Enough of Redis to exercise a fixed-window counter."""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.deleted: list[str] = []

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def ttl(self, key):
        return self.ttls.get(key, -1)

    def delete(self, key):
        self.deleted.append(key)
        self.counts.pop(key, None)


@pytest.fixture
def redis():
    fake = FakeRedis()
    with patch(f"{_M}.shared_redis_client", return_value=fake):
        yield fake


class TestInitLimit:
    def test_allows_up_to_the_limit_then_blocks(self, redis):
        from cqc_lem.utilities.auth_rate_limit import check_auth_init
        from cqc_lem.utilities.env_constants import AUTH_INIT_MAX_PER_HOUR

        for _ in range(AUTH_INIT_MAX_PER_HOUR):
            assert check_auth_init("user@example.com", "1.2.3.4").allowed is True
        blocked = check_auth_init("user@example.com", "1.2.3.4")
        assert blocked.allowed is False
        assert blocked.scope == "init_email"

    def test_the_window_ttl_is_set_once_not_extended_per_request(self, redis):
        from cqc_lem.utilities.auth_rate_limit import check_auth_init

        check_auth_init("user@example.com")
        check_auth_init("user@example.com")
        key = next(k for k in redis.ttls if "init_email" in k)
        # A sliding TTL would let a steady drip of requests keep the window open forever.
        assert redis.ttls[key] == 3600

    def test_the_email_is_case_and_space_insensitive(self, redis):
        from cqc_lem.utilities.auth_rate_limit import check_auth_init

        check_auth_init("  USER@example.com ")
        check_auth_init("user@example.com")
        email_keys = [k for k in redis.counts if "init_email" in k]
        assert len(email_keys) == 1
        assert redis.counts[email_keys[0]] == 2

    def test_a_second_address_from_one_ip_still_hits_the_ip_bucket(self, redis):
        from cqc_lem.utilities.auth_rate_limit import check_auth_init
        from cqc_lem.utilities.env_constants import AUTH_IP_MAX_PER_HOUR

        allowed = 0
        for i in range(AUTH_IP_MAX_PER_HOUR + 5):
            if check_auth_init(f"user{i}@example.com", "9.9.9.9").allowed:
                allowed += 1
        assert allowed == AUTH_IP_MAX_PER_HOUR

    def test_the_ip_never_reaches_redis_in_the_clear(self, redis):
        from cqc_lem.utilities.auth_rate_limit import check_auth_init

        check_auth_init("user@example.com", "203.0.113.9")
        assert all("203.0.113.9" not in k for k in redis.counts)


class TestVerifyLimit:
    def test_blocks_after_the_verify_limit(self, redis):
        from cqc_lem.utilities.auth_rate_limit import check_auth_verify
        from cqc_lem.utilities.env_constants import AUTH_VERIFY_MAX_PER_HOUR

        for _ in range(AUTH_VERIFY_MAX_PER_HOUR):
            assert check_auth_verify("user@example.com", "1.2.3.4").allowed is True
        assert check_auth_verify("user@example.com", "1.2.3.4").allowed is False

    def test_retry_after_reports_the_remaining_window(self, redis):
        from cqc_lem.utilities.auth_rate_limit import check_auth_verify
        from cqc_lem.utilities.env_constants import AUTH_VERIFY_MAX_PER_HOUR

        for _ in range(AUTH_VERIFY_MAX_PER_HOUR):
            check_auth_verify("user@example.com")
        key = next(k for k in redis.ttls if "verify_email" in k)
        redis.ttls[key] = 120
        assert check_auth_verify("user@example.com").retry_after_seconds == 120


class TestFailOpen:
    def test_no_redis_allows_the_request(self):
        from cqc_lem.utilities.auth_rate_limit import check_auth_init, check_auth_verify

        with patch(f"{_M}.shared_redis_client", return_value=None):
            assert check_auth_init("user@example.com", "1.2.3.4").allowed is True
            assert check_auth_verify("user@example.com", "1.2.3.4").allowed is True

    def test_a_broken_redis_allows_the_request(self):
        from cqc_lem.utilities.auth_rate_limit import check_auth_init

        broken = MagicMock()
        broken.incr.side_effect = ConnectionError("redis down")
        with patch(f"{_M}.shared_redis_client", return_value=broken):
            assert check_auth_init("user@example.com").allowed is True

    def test_clear_is_a_no_op_without_redis(self):
        from cqc_lem.utilities.auth_rate_limit import clear_auth_limits

        with patch(f"{_M}.shared_redis_client", return_value=None):
            clear_auth_limits("user@example.com", "1.2.3.4")  # must not raise


class TestClearOnSuccess:
    def test_a_successful_login_frees_both_buckets(self, redis):
        from cqc_lem.utilities.auth_rate_limit import check_auth_verify, clear_auth_limits
        from cqc_lem.utilities.env_constants import AUTH_VERIFY_MAX_PER_HOUR

        for _ in range(AUTH_VERIFY_MAX_PER_HOUR):
            check_auth_verify("user@example.com", "1.2.3.4")
        clear_auth_limits("user@example.com", "1.2.3.4")
        assert check_auth_verify("user@example.com", "1.2.3.4").allowed is True
        assert any("verify_email" in k for k in redis.deleted)
        assert any("verify_ip" in k for k in redis.deleted)
