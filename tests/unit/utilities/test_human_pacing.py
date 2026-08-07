"""Unit tests for the human-pacing engine (issue #626).

Everything here is seeded or env-pinned: a pacing value that can't be reproduced is a pacing value
nobody can debug when LinkedIn throttles the account.
"""

import random
from datetime import date
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_HP = "cqc_lem.utilities.human_pacing"

_FRIDAY = date(2026, 7, 24)
_SATURDAY = date(2026, 7, 25)
_SUNDAY = date(2026, 7, 26)


class FakeRedis:
    """Just enough Redis for the budget key + the governor hash."""

    def __init__(self):
        self.strings = {}
        self.hashes = {}
        self.expires = []

    def get(self, key):
        value = self.strings.get(key)
        return None if value is None else str(value).encode()

    def set(self, key, value, ex=None):
        self.strings[key] = value
        if ex is not None:
            self.expires.append((key, ex))
        return True

    def hincrby(self, key, field, amount):
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + amount
        return bucket[field]

    def hget(self, key, field):
        value = self.hashes.get(key, {}).get(field)
        return None if value is None else str(value).encode()

    def expire(self, key, ttl):
        self.expires.append((key, ttl))
        return True


@pytest.fixture(autouse=True)
def _pacing_on(monkeypatch):
    """The suite defaults pacing OFF (tests/conftest.py); this module is what tests it ON."""
    monkeypatch.setenv("HUMAN_PACING_ENABLED", "true")
    monkeypatch.setenv("PACING_SEED", "issue-626")


@pytest.fixture
def fake_redis():
    client = FakeRedis()
    with patch(f"{_HP}.shared_redis_client", return_value=client):
        yield client


@pytest.fixture
def no_redis():
    with patch(f"{_HP}.shared_redis_client", return_value=None):
        yield


# --- 1. read-time delay ------------------------------------------------------------------------

class TestReadDelay:
    def test_even_a_one_line_post_costs_the_floor(self):
        from cqc_lem.utilities.human_pacing import read_delay_seconds
        # The whole point: commenting three seconds after a post rendered is the loudest bot tell.
        assert read_delay_seconds("Hi.", rng=random.Random(1)) >= 45

    def test_delay_scales_with_post_length(self):
        from cqc_lem.utilities.human_pacing import read_delay_seconds
        rng = random.Random(7)
        short = read_delay_seconds(" ".join(["word"] * 60), rng=rng)
        long_ = read_delay_seconds(" ".join(["word"] * 600), rng=random.Random(7))
        assert long_ > short

    def test_delay_is_capped_so_a_wall_of_text_cannot_park_a_worker(self):
        from cqc_lem.utilities.human_pacing import MAX_INLINE_SLEEP_SECONDS, read_delay_seconds
        delay = read_delay_seconds(" ".join(["word"] * 50_000), rng=random.Random(3))
        assert delay <= 240
        assert delay < MAX_INLINE_SLEEP_SECONDS  # acceptance: no worker blocks >5 min sleeping

    def test_a_misconfigured_ceiling_still_cannot_exceed_the_inline_sleep_budget(self, monkeypatch):
        from cqc_lem.utilities.human_pacing import MAX_INLINE_SLEEP_SECONDS, read_delay_seconds
        monkeypatch.setenv("PACING_READ_MAX_SECONDS", "99999")
        delay = read_delay_seconds(" ".join(["word"] * 50_000), rng=random.Random(3))
        assert delay <= MAX_INLINE_SLEEP_SECONDS

    def test_seeded_rng_reproduces_the_same_delay(self):
        from cqc_lem.utilities.human_pacing import read_delay_seconds
        post = " ".join(["word"] * 200)
        assert read_delay_seconds(post, rng=random.Random(42)) == \
               read_delay_seconds(post, rng=random.Random(42))

    def test_browse_time_varies_between_runs(self):
        from cqc_lem.utilities.human_pacing import read_delay_seconds
        post = " ".join(["word"] * 200)
        rng = random.Random(11)
        assert len({read_delay_seconds(post, rng=rng) for _ in range(20)}) > 1

    def test_disabled_adds_nothing(self, monkeypatch):
        from cqc_lem.utilities.human_pacing import read_delay_seconds
        monkeypatch.setenv("HUMAN_PACING_ENABLED", "false")
        assert read_delay_seconds("a post", rng=random.Random(1)) == 0.0

    def test_pace_read_sleeps_for_the_computed_delay(self):
        from cqc_lem.utilities.human_pacing import pace_read
        with patch(f"{_HP}.time.sleep") as slept:
            used = pace_read("a short post")
        assert used >= 45
        slept.assert_called_once()
        assert slept.call_args[0][0] == used

    def test_pace_read_never_sleeps_when_disabled(self, monkeypatch):
        from cqc_lem.utilities.human_pacing import pace_read
        monkeypatch.setenv("HUMAN_PACING_ENABLED", "false")
        with patch(f"{_HP}.time.sleep") as slept:
            assert pace_read("a short post") == 0.0
        slept.assert_not_called()


# --- 2. schedule jitter ------------------------------------------------------------------------

class TestDispatchJitter:
    def test_standard_jitter_lands_in_the_configured_band(self):
        from cqc_lem.utilities.human_pacing import dispatch_jitter_seconds
        rng = random.Random(5)
        values = [dispatch_jitter_seconds(rng=rng) for _ in range(200)]
        assert all(30 * 60 <= v <= 90 * 60 for v in values)
        assert len(set(values)) > 50  # a constant offset is not jitter

    def test_responsive_jitter_keeps_own_post_replies_fast(self):
        from cqc_lem.utilities.human_pacing import PACE_RESPONSIVE, dispatch_jitter_seconds
        rng = random.Random(5)
        values = [dispatch_jitter_seconds(PACE_RESPONSIVE, rng=rng) for _ in range(200)]
        # G7 exemption: replying to comments on YOUR OWN post is human, so no tens-of-minutes delay.
        assert all(0 <= v <= 180 for v in values)
        assert len(set(values)) > 20

    def test_band_is_env_tunable(self, monkeypatch):
        from cqc_lem.utilities.human_pacing import dispatch_jitter_seconds
        monkeypatch.setenv("PACING_JITTER_MIN_MINUTES", "5")
        monkeypatch.setenv("PACING_JITTER_MAX_MINUTES", "10")
        rng = random.Random(2)
        assert all(300 <= dispatch_jitter_seconds(rng=rng) <= 600 for _ in range(50))

    def test_inverted_band_does_not_produce_a_negative_countdown(self, monkeypatch):
        from cqc_lem.utilities.human_pacing import dispatch_jitter_seconds
        monkeypatch.setenv("PACING_JITTER_MIN_MINUTES", "90")
        monkeypatch.setenv("PACING_JITTER_MAX_MINUTES", "30")
        assert dispatch_jitter_seconds(rng=random.Random(1)) == 90 * 60

    def test_disabled_is_no_countdown(self, monkeypatch):
        from cqc_lem.utilities.human_pacing import dispatch_jitter_seconds
        monkeypatch.setenv("HUMAN_PACING_ENABLED", "false")
        assert dispatch_jitter_seconds() == 0


# --- 3. variable daily volume ------------------------------------------------------------------

class TestDailyBudget:
    def test_budget_is_a_draw_inside_the_configured_ratio_band(self, monkeypatch, no_redis):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, daily_budget
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        budgets = [daily_budget(uid, ACTION_COMMENT, 20, _FRIDAY) for uid in range(1, 60)]
        assert all(8 <= b <= 20 for b in budgets)          # 40%-100% of a cap of 20
        assert len(set(budgets)) > 3                       # not the same number for everyone

    def test_the_same_day_always_re_derives_the_same_budget(self, monkeypatch, no_redis):
        """Acceptance: a retry (or a second worker, or a Redis outage) must not re-roll the day's
        budget into a fresh, possibly larger one.
        """
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, daily_budget
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        first = daily_budget(3, ACTION_COMMENT, 20, _FRIDAY)
        assert all(daily_budget(3, ACTION_COMMENT, 20, _FRIDAY) == first for _ in range(5))

    def test_different_days_draw_different_budgets(self, monkeypatch, no_redis):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, daily_budget
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        days = [date(2026, 7, d) for d in range(1, 25)]
        assert len({daily_budget(3, ACTION_COMMENT, 20, d) for d in days}) > 3

    def test_the_persisted_budget_survives_a_mid_day_cap_edit(self, fake_redis, monkeypatch):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, daily_budget
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        first = daily_budget(3, ACTION_COMMENT, 20, _FRIDAY)
        assert daily_budget(3, ACTION_COMMENT, 200, _FRIDAY) == first  # read back, not re-drawn
        assert any(ttl > 24 * 60 * 60 for _, ttl in fake_redis.expires)

    def test_lowering_the_cap_mid_day_takes_effect_immediately(self, fake_redis, monkeypatch):
        """Stability must never make us MORE aggressive than the user's own setting: someone who
        drops their cap because they're worried about the account has to be obeyed now, not
        tomorrow.
        """
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, daily_budget
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        monkeypatch.setenv("PACING_BUDGET_MIN_RATIO", "1")
        monkeypatch.setenv("PACING_BUDGET_MAX_RATIO", "1")
        assert daily_budget(3, ACTION_COMMENT, 20, _FRIDAY) == 20   # persists 20 for the day
        assert daily_budget(3, ACTION_COMMENT, 3, _FRIDAY) == 3     # ...clamped to the new cap

    def test_a_corrupt_stored_budget_is_re_derived(self, fake_redis, monkeypatch):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, daily_budget
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        key = f"pacing:budget:{ACTION_COMMENT}:3:{_FRIDAY.isoformat()}"
        fake_redis.strings[key] = "not-a-number"
        assert daily_budget(3, ACTION_COMMENT, 20, _FRIDAY) > 0

    @pytest.mark.parametrize("weekend_day", [_SATURDAY, _SUNDAY])
    def test_weekends_are_quieter_than_weekdays(self, monkeypatch, no_redis, weekend_day):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, daily_budget
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        # Pin the ratio draw so only the weekend factor moves.
        monkeypatch.setenv("PACING_BUDGET_MIN_RATIO", "1")
        monkeypatch.setenv("PACING_BUDGET_MAX_RATIO", "1")
        assert daily_budget(3, ACTION_COMMENT, 20, _FRIDAY) == 20
        assert daily_budget(3, ACTION_COMMENT, 20, weekend_day) == 11  # 20 * 0.55

    def test_a_rest_day_spends_nothing(self, monkeypatch, no_redis):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, daily_budget, is_rest_day
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "1")
        assert is_rest_day(3, _FRIDAY) is True
        assert daily_budget(3, ACTION_COMMENT, 20, _FRIDAY) == 0

    def test_rest_days_are_occasional_not_common(self, monkeypatch, no_redis):
        """The distribution has to actually land near the configured rate — a rest day that never
        fires is no cover, and one that fires constantly silently stops the product.
        """
        from cqc_lem.utilities.human_pacing import is_rest_day
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0.1")
        rests = sum(1 for uid in range(2000) if is_rest_day(uid, _FRIDAY))
        assert 0.07 <= rests / 2000 <= 0.13

    def test_a_rest_day_is_account_wide_not_per_lane(self, monkeypatch, no_redis):
        from cqc_lem.utilities.human_pacing import ENGAGEMENT_ACTIONS, daily_budget
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "1")
        assert all(daily_budget(3, a, 20, _FRIDAY) == 0 for a in ENGAGEMENT_ACTIONS)

    def test_an_enabled_lane_is_never_silently_zeroed(self, monkeypatch, no_redis):
        """Only a rest day may produce 0 — a tiny cap must still allow one action."""
        from cqc_lem.utilities.human_pacing import ACTION_DM, daily_budget
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        assert all(daily_budget(uid, ACTION_DM, 1, _FRIDAY) == 1 for uid in range(1, 40))

    def test_a_zero_cap_stays_zero(self, no_redis):
        from cqc_lem.utilities.human_pacing import ACTION_INVITE, daily_budget
        assert daily_budget(3, ACTION_INVITE, 0, _FRIDAY) == 0

    def test_disabled_returns_the_raw_cap(self, monkeypatch, no_redis):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, daily_budget
        monkeypatch.setenv("HUMAN_PACING_ENABLED", "false")
        assert daily_budget(3, ACTION_COMMENT, 20, _SATURDAY) == 20


# --- the account-level governor ----------------------------------------------------------------

class TestGovernor:
    def _caps(self):
        from cqc_lem.utilities.human_pacing import engagement_caps_from_prefs
        return engagement_caps_from_prefs({"max_comments_per_day": 20, "max_dms_per_day": 20,
                                           "max_invites_per_day": 10})

    def test_caps_are_read_off_engagement_preferences(self):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, ACTION_DM, ACTION_INVITE, engagement_caps_from_prefs
        caps = engagement_caps_from_prefs({"max_comments_per_day": 20, "max_dms_per_day": 5,
                                           "max_invites_per_day": None})
        # No reply entry: replies share the comment cap, so counting them would double it.
        assert caps == {ACTION_COMMENT: 20, ACTION_DM: 5, ACTION_INVITE: 0}

    def test_garbage_caps_degrade_to_zero_rather_than_raising(self):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, engagement_caps_from_prefs
        assert engagement_caps_from_prefs({"max_comments_per_day": "lots"})[ACTION_COMMENT] == 0

    def test_actions_are_counted_per_lane_and_in_total(self, fake_redis):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, ACTION_DM, actions_used_today, record_action
        record_action(1, ACTION_COMMENT, 3)
        record_action(1, ACTION_DM)
        assert actions_used_today(1, ACTION_COMMENT) == 3
        assert actions_used_today(1, ACTION_DM) == 1
        assert actions_used_today(1) == 4

    def test_replies_are_tracked_but_do_not_spend_the_outbound_envelope(self, fake_redis):
        """Going quiet on people who commented on the user's OWN post to protect a commenting
        budget would be worse than the cadence risk — so replies count in their own field only.
        """
        from cqc_lem.utilities.human_pacing import ACTION_REPLY, ENVELOPE_ACTIONS, actions_used_today, record_action
        assert ACTION_REPLY not in ENVELOPE_ACTIONS
        record_action(1, ACTION_REPLY, 12)
        assert actions_used_today(1, ACTION_REPLY) == 12
        assert actions_used_today(1) == 0            # the outbound envelope is untouched

    def test_recording_nothing_is_a_no_op(self, fake_redis):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, record_action
        record_action(1, ACTION_COMMENT, 0)
        assert fake_redis.hashes == {}

    def test_the_envelope_is_the_sum_of_every_lanes_budget(self, monkeypatch, no_redis):
        from cqc_lem.utilities.human_pacing import account_envelope, daily_budget
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        caps = self._caps()
        assert account_envelope(1, caps, _FRIDAY) == \
               sum(daily_budget(1, a, c, _FRIDAY) for a, c in caps.items())

    def test_remaining_is_the_lane_budget_minus_what_the_db_says_was_spent(self, monkeypatch, no_redis):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, daily_budget, remaining_actions
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        budget = daily_budget(1, ACTION_COMMENT, 20, _FRIDAY)
        assert remaining_actions(1, ACTION_COMMENT, 20, 2, day=_FRIDAY) == budget - 2
        assert remaining_actions(1, ACTION_COMMENT, 20, budget + 5, day=_FRIDAY) == 0

    def test_the_combined_envelope_stops_a_lane_that_still_has_its_own_budget(self, fake_redis,
                                                                             monkeypatch):
        """The point of an ACCOUNT-level governor: commenting, DMs and invites each check their own
        cap, so without a shared envelope the day's combined traffic is the sum of all three.
        """
        from cqc_lem.utilities.human_pacing import (
            ACTION_COMMENT,
            ACTION_DM,
            account_envelope,
            record_action,
            remaining_actions,
        )
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        caps = self._caps()
        record_action(1, ACTION_DM, account_envelope(1, caps))   # other lanes ate the whole envelope
        assert remaining_actions(1, ACTION_COMMENT, 20, 0, caps=caps) == 0
        # ...and without the caps argument the call still degrades to the per-lane budget.
        assert remaining_actions(1, ACTION_COMMENT, 20, 0) > 0

    def test_disabling_pacing_removes_the_envelope_too(self, fake_redis, monkeypatch):
        """HUMAN_PACING_ENABLED=false has to restore the PRE-#626 behaviour exactly. Pre-#626 each
        lane spent its own cap and there was no shared pool, so a busy DM day must not be able to
        shut commenting down through an envelope the switch is supposed to have turned off.
        """
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, ACTION_DM, record_action, remaining_actions
        record_action(1, ACTION_DM, 500)  # far past any envelope
        monkeypatch.setenv("HUMAN_PACING_ENABLED", "false")
        assert remaining_actions(1, ACTION_COMMENT, 20, 5, caps=self._caps()) == 15

    def test_without_redis_the_governor_fails_open(self, no_redis, monkeypatch):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, daily_budget, remaining_actions
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        assert remaining_actions(1, ACTION_COMMENT, 20, 0, caps=self._caps()) == \
               daily_budget(1, ACTION_COMMENT, 20)

    def test_a_redis_error_never_propagates(self, monkeypatch):
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, actions_used_today, record_action

        class Broken:
            def __getattr__(self, _name):
                def boom(*a, **k):
                    raise RuntimeError("redis down")
                return boom

        with patch(f"{_HP}.shared_redis_client", return_value=Broken()), \
             patch(f"{_HP}.log_warning"):
            record_action(1, ACTION_COMMENT)
            assert actions_used_today(1) == 0


class TestFollowLane:
    """The roster auto-follow lane (issue #962)."""

    def test_the_follow_lane_has_its_own_cap_preference(self):
        from cqc_lem.utilities.human_pacing import ACTION_CAP_PREF, ACTION_FOLLOW
        assert ACTION_CAP_PREF[ACTION_FOLLOW] == "max_follows_per_day"

    def test_following_never_enlarges_the_account_envelope(self, monkeypatch, no_redis):
        """A follow is bounded BY the envelope, it does not add to it — putting it in
        ENVELOPE_ACTIONS would hand every account its follow cap in extra daily allowance.
        """
        from cqc_lem.utilities.human_pacing import (
            ACTION_FOLLOW,
            ENVELOPE_ACTIONS,
            account_envelope,
            engagement_caps_from_prefs,
        )
        assert ACTION_FOLLOW not in ENVELOPE_ACTIONS
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        prefs = {"max_comments_per_day": 20, "max_dms_per_day": 10, "max_invites_per_day": 10,
                 "max_follows_per_day": 5}
        caps = engagement_caps_from_prefs(prefs)
        assert ACTION_FOLLOW not in caps
        assert account_envelope(1, caps, _FRIDAY) == \
               account_envelope(1, engagement_caps_from_prefs({**prefs, "max_follows_per_day": 50}),
                                _FRIDAY)

    def test_the_follow_budget_is_drawn_under_its_own_key(self, monkeypatch, no_redis):
        # Sharing another lane's key would let whichever lane ran first that day write the other's
        # budget — the ACTION_COMPANY_INVITE lesson.
        from cqc_lem.utilities.human_pacing import ACTION_COMMENT, ACTION_FOLLOW, daily_budget
        monkeypatch.setenv("PACING_REST_DAY_CHANCE", "0")
        assert daily_budget(1, ACTION_FOLLOW, 3, _FRIDAY) <= 3
        assert daily_budget(1, ACTION_COMMENT, 20, _FRIDAY) > 3
