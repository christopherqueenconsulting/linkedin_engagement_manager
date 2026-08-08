"""The on-demand profile-refresh claim (issue #1076).

Three properties carry the feature: the window actually bounds a repeated press (each one is a
Chrome session out of the fixed pool), a Redis outage does NOT take the button away, and the
read-only peek the SPA renders from never spends the window it is reporting on.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.profile_refresh"


class FakeRedis:
    """Enough of Redis to exercise a fixed-window counter."""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def ttl(self, key):
        return self.ttls.get(key, -2)

    def get(self, key):
        value = self.counts.get(key)
        return None if value is None else str(value).encode()


@pytest.fixture
def redis():
    fake = FakeRedis()
    with patch(f"{_M}.shared_redis_client", return_value=fake):
        yield fake


class TestClaim:
    def test_the_first_press_of_the_window_is_granted(self, redis):
        from cqc_lem.utilities.profile_refresh import REASON_QUEUED, claim_profile_refresh
        claim = claim_profile_refresh(7)
        assert claim.queued is True
        assert claim.reason == REASON_QUEUED
        assert claim.retry_after_seconds == 0

    def test_the_second_press_of_the_window_is_refused_with_the_remaining_wait(self, redis):
        from cqc_lem.utilities.profile_refresh import (
            REASON_ALREADY_REFRESHED_TODAY,
            WINDOW_SECONDS,
            claim_profile_refresh,
        )
        claim_profile_refresh(7)
        claim = claim_profile_refresh(7)
        assert claim.queued is False
        assert claim.reason == REASON_ALREADY_REFRESHED_TODAY
        assert claim.retry_after_seconds == WINDOW_SECONDS

    def test_the_window_is_fixed_not_sliding(self, redis):
        """The TTL is stamped once, on the first increment.

        Re-stamping it on every press would let a user who taps the button all day push their own
        reset further and further out — the window would never expire.
        """
        from cqc_lem.utilities.profile_refresh import WINDOW_SECONDS, claim_profile_refresh
        claim_profile_refresh(7)
        redis.ttls["lem:profile_refresh:7"] = 42          # time has passed
        for _ in range(5):
            claim_profile_refresh(7)
        assert redis.ttls["lem:profile_refresh:7"] == 42
        assert WINDOW_SECONDS == 24 * 60 * 60

    def test_one_user_spending_their_window_never_touches_another(self, redis):
        from cqc_lem.utilities.profile_refresh import claim_profile_refresh
        claim_profile_refresh(7)
        assert claim_profile_refresh(7).queued is False
        assert claim_profile_refresh(8).queued is True

    def test_it_fails_open_when_redis_is_absent(self):
        from cqc_lem.utilities.profile_refresh import claim_profile_refresh
        with patch(f"{_M}.shared_redis_client", return_value=None):
            assert claim_profile_refresh(7).queued is True

    def test_it_fails_open_when_redis_raises(self):
        from cqc_lem.utilities.profile_refresh import claim_profile_refresh
        broken = MagicMock()
        broken.incr.side_effect = RuntimeError("broker restarting")
        with patch(f"{_M}.shared_redis_client", return_value=broken):
            assert claim_profile_refresh(7).queued is True

    def test_a_spent_window_is_a_debug_no_op_never_a_warning(self, redis):
        """A person pressing a button twice is not a defect.

        `log_warning` re-emits at ERROR on repeat and files ONE grouped `$exception`
        (utilities/CLAUDE.md), so warning here would file a GitHub issue against working behaviour.
        """
        from cqc_lem.utilities.profile_refresh import claim_profile_refresh
        claim_profile_refresh(7)
        with patch(f"{_M}.log_warning") as warn, patch(f"{_M}.log_debug") as debug:
            claim_profile_refresh(7)
        warn.assert_not_called()
        debug.assert_called()

    def test_an_unreadable_ttl_reports_the_whole_window_rather_than_zero(self, redis):
        """Telling the SPA to re-enable the button immediately is the one answer certainly wrong."""
        from cqc_lem.utilities.profile_refresh import WINDOW_SECONDS, claim_profile_refresh
        claim_profile_refresh(7)
        redis.ttl = MagicMock(side_effect=RuntimeError("no ttl"))
        assert claim_profile_refresh(7).retry_after_seconds == WINDOW_SECONDS


class TestPeek:
    def test_an_unspent_window_reads_as_zero(self, redis):
        from cqc_lem.utilities.profile_refresh import refresh_claimed_seconds
        assert refresh_claimed_seconds(7) == 0

    def test_a_spent_window_reads_as_the_remaining_wait(self, redis):
        from cqc_lem.utilities.profile_refresh import claim_profile_refresh, refresh_claimed_seconds
        claim_profile_refresh(7)
        redis.ttls["lem:profile_refresh:7"] = 3600
        assert refresh_claimed_seconds(7) == 3600

    def test_the_peek_never_spends_the_window(self, redis):
        """The SPA calls this on every page load.

        If it counted, rendering the button would consume the very refresh it is offering.
        """
        from cqc_lem.utilities.profile_refresh import claim_profile_refresh, refresh_claimed_seconds
        for _ in range(5):
            refresh_claimed_seconds(7)
        assert claim_profile_refresh(7).queued is True

    def test_it_fails_open_when_redis_is_absent(self):
        from cqc_lem.utilities.profile_refresh import refresh_claimed_seconds
        with patch(f"{_M}.shared_redis_client", return_value=None):
            assert refresh_claimed_seconds(7) == 0

    def test_it_fails_open_when_redis_raises(self):
        from cqc_lem.utilities.profile_refresh import refresh_claimed_seconds
        broken = MagicMock()
        broken.get.side_effect = RuntimeError("broker restarting")
        with patch(f"{_M}.shared_redis_client", return_value=broken):
            assert refresh_claimed_seconds(7) == 0
