"""Unit tests for cqc_lem.utilities.engagement_window (issue #547)."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.engagement_window"

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def mock_redis():
    """A working Redis handle for the marker helpers (the unit lane blocks the real one)."""
    client = MagicMock()
    with patch(f"{_MOD}.shared_redis_client", return_value=client), \
         patch(f"{_MOD}.track_pre_post_engagement"):
        yield client


@pytest.fixture
def mock_track():
    with patch(f"{_MOD}.shared_redis_client", return_value=None), \
         patch(f"{_MOD}.track_pre_post_engagement") as tracker:
        yield tracker


class TestPlanPrePostWindow:
    def test_full_lead_available_starts_lead_minutes_before(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        window = plan_pre_post_window(_NOW + timedelta(hours=1), 15, now=_NOW)

        assert window is not None
        assert window.eta == _NOW + timedelta(minutes=45)
        assert window.duration_seconds == 15 * 60
        assert window.clamped is False

    def test_exactly_one_lead_away_is_not_clamped(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        window = plan_pre_post_window(_NOW + timedelta(minutes=15), 15, now=_NOW)

        assert window.eta == _NOW
        assert window.duration_seconds == 15 * 60
        assert window.clamped is False

    def test_late_pickup_clamps_eta_to_now(self):
        """The stale-eta case: less than the lead left → fire ASAP, never with an eta in the past."""
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        window = plan_pre_post_window(_NOW + timedelta(minutes=6), 15, now=_NOW)

        assert window.eta == _NOW
        assert window.duration_seconds == 6 * 60  # loop ends at the post, not 15 min later
        assert window.clamped is True

    def test_post_at_its_scheduled_time_returns_none(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        assert plan_pre_post_window(_NOW, 15, now=_NOW) is None

    def test_post_past_its_scheduled_time_returns_none(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        assert plan_pre_post_window(_NOW - timedelta(hours=3), 15, now=_NOW) is None

    def test_window_shorter_than_minimum_returns_none(self):
        """60s of runway is a page load, not a warm-up — don't spin up a browser for it."""
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        assert plan_pre_post_window(_NOW + timedelta(seconds=60), 15, now=_NOW) is None

    def test_minimum_window_is_configurable(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        window = plan_pre_post_window(_NOW + timedelta(seconds=60), 15, now=_NOW, min_window_seconds=30)

        assert window is not None
        assert window.duration_seconds == 60

    def test_naive_scheduled_time_is_treated_as_utc(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        naive = (_NOW + timedelta(hours=1)).replace(tzinfo=None)
        window = plan_pre_post_window(naive, 15, now=_NOW)

        assert window.eta == _NOW + timedelta(minutes=45)

    def test_naive_now_is_treated_as_utc(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        window = plan_pre_post_window(_NOW + timedelta(hours=1), 15, now=_NOW.replace(tzinfo=None))

        assert window.eta == _NOW + timedelta(minutes=45)

    def test_defaults_to_current_time(self):
        from cqc_lem.utilities.engagement_window import plan_pre_post_window
        window = plan_pre_post_window(datetime.now(timezone.utc) + timedelta(hours=2), 10)

        assert window is not None
        assert window.duration_seconds == 10 * 60

    def test_viewer_lead_is_shorter_than_comment_lead(self):
        from cqc_lem.utilities.engagement_window import (
            PRE_POST_COMMENT_LEAD_MINUTES, PRE_POST_VIEWER_LEAD_MINUTES)
        assert PRE_POST_VIEWER_LEAD_MINUTES < PRE_POST_COMMENT_LEAD_MINUTES


class TestMarkers:
    def test_scheduled_marker_written_to_redis(self, mock_redis):
        from cqc_lem.utilities.engagement_window import PrePostWindow, record_pre_post_scheduled
        window = PrePostWindow(eta=_NOW, duration_seconds=900, clamped=False)
        record_pre_post_scheduled(11, 7, window)

        key, mapping = mock_redis.hset.call_args[0][0], mock_redis.hset.call_args[1]["mapping"]
        assert key == "engagement:prepost:11:automate_commenting"
        assert mapping["status"] == "scheduled"
        assert mapping["window_seconds"] == "900"
        assert mapping["task_name"] == "automate_commenting"
        mock_redis.expire.assert_called_once()

    def test_each_pre_post_task_gets_its_own_marker(self, mock_redis):
        """Both pre-post tasks fire for the SAME post — the viewer dispatch must not overwrite the
        commenting window's eta/duration (Copilot review, PR #594)."""
        from cqc_lem.utilities.engagement_window import (
            PRE_POST_TASK_COMMENTING, PRE_POST_TASK_VIEWER, PrePostWindow, record_pre_post_scheduled)
        record_pre_post_scheduled(18, 7, PrePostWindow(eta=_NOW, duration_seconds=900, clamped=False))
        record_pre_post_scheduled(18, 7, PrePostWindow(eta=_NOW, duration_seconds=600, clamped=False),
                                  task_name=PRE_POST_TASK_VIEWER)

        writes = {c[0][0]: c[1]["mapping"] for c in mock_redis.hset.call_args_list}
        assert writes[f"engagement:prepost:18:{PRE_POST_TASK_COMMENTING}"]["window_seconds"] == "900"
        assert writes[f"engagement:prepost:18:{PRE_POST_TASK_VIEWER}"]["window_seconds"] == "600"

    def test_run_marker_targets_the_commenting_key(self, mock_redis):
        from cqc_lem.utilities.engagement_window import record_pre_post_run
        record_pre_post_run(19, 7, 2)

        assert mock_redis.hset.call_args[0][0] == "engagement:prepost:19:automate_commenting"
        assert all(c[0][0] == "engagement:prepost:19:automate_commenting"
                   for c in mock_redis.hincrby.call_args_list)

    def test_skipped_marker_records_reason(self, mock_redis):
        from cqc_lem.utilities.engagement_window import record_pre_post_skipped
        record_pre_post_skipped(12, 7, "throttled")

        mapping = mock_redis.hset.call_args[1]["mapping"]
        assert mapping["status"] == "skipped"
        assert mapping["skip_reason"] == "throttled"

    def test_run_marker_accumulates_runs_and_comments(self, mock_redis):
        from cqc_lem.utilities.engagement_window import record_pre_post_run
        record_pre_post_run(13, 7, 3, now=_NOW)

        increments = {c[0][1]: c[0][2] for c in mock_redis.hincrby.call_args_list}
        assert increments == {"runs": 1, "comments": 3}
        assert mock_redis.hset.call_args[1]["mapping"]["last_run_at"] == _NOW.isoformat()

    def test_run_marker_handles_no_comments(self, mock_redis):
        from cqc_lem.utilities.engagement_window import record_pre_post_run
        record_pre_post_run(14, 7, None)

        increments = {c[0][1]: c[0][2] for c in mock_redis.hincrby.call_args_list}
        assert increments == {"runs": 1, "comments": 0}

    def test_markers_fail_open_without_redis(self, mock_track):
        from cqc_lem.utilities.engagement_window import (
            PrePostWindow, record_pre_post_run, record_pre_post_scheduled, record_pre_post_skipped)
        record_pre_post_scheduled(15, 7, PrePostWindow(eta=_NOW, duration_seconds=900, clamped=True))
        record_pre_post_skipped(15, 7, "past_window")
        record_pre_post_run(15, 7, 1)

        # No Redis → no crash, and the PostHog event still carries the observability signal.
        statuses = [c[0][2] for c in mock_track.call_args_list]
        assert statuses == ["scheduled", "skipped", "ran"]

    def test_marker_write_error_is_swallowed(self, mock_redis):
        from cqc_lem.utilities.engagement_window import record_pre_post_skipped
        mock_redis.hset.side_effect = RuntimeError("redis down")

        record_pre_post_skipped(16, 7, "past_window")  # must not raise

    def test_tracks_posthog_event_for_dispatch(self):
        from cqc_lem.utilities.engagement_window import PrePostWindow, record_pre_post_scheduled
        with patch(f"{_MOD}.shared_redis_client", return_value=None), \
             patch(f"{_MOD}.track_pre_post_engagement") as tracker:
            record_pre_post_scheduled(17, 7, PrePostWindow(eta=_NOW, duration_seconds=300, clamped=True))

        args, kwargs = tracker.call_args
        assert args == (17, 7, "scheduled")
        assert kwargs["clamped"] is True
        assert kwargs["window_seconds"] == 300


class TestGetPrePostWindowStat:
    def test_returns_decoded_stat(self, mock_redis):
        from cqc_lem.utilities.engagement_window import get_pre_post_window_stat
        mock_redis.hgetall.return_value = {
            b"status": b"scheduled", b"runs": b"2", b"comments": b"5",
            b"window_seconds": b"900", b"user_id": b"7", b"clamped": b"0",
        }
        stat = get_pre_post_window_stat(21)

        assert stat["status"] == "scheduled"
        assert stat["runs"] == 2
        assert stat["comments"] == 5
        assert stat["user_id"] == 7

    def test_defaults_counters_when_never_run(self, mock_redis):
        from cqc_lem.utilities.engagement_window import get_pre_post_window_stat
        mock_redis.hgetall.return_value = {b"status": b"skipped", b"skip_reason": b"throttled"}
        stat = get_pre_post_window_stat(22)

        assert stat["runs"] == 0
        assert stat["comments"] == 0
        assert stat["skip_reason"] == "throttled"

    def test_non_numeric_counter_is_left_as_text(self, mock_redis):
        from cqc_lem.utilities.engagement_window import get_pre_post_window_stat
        mock_redis.hgetall.return_value = {b"runs": b"n/a"}

        assert get_pre_post_window_stat(23)["runs"] == "n/a"

    def test_reads_the_requested_task_marker(self, mock_redis):
        from cqc_lem.utilities.engagement_window import PRE_POST_TASK_VIEWER, get_pre_post_window_stat
        mock_redis.hgetall.return_value = {b"status": b"scheduled"}
        get_pre_post_window_stat(27, task_name=PRE_POST_TASK_VIEWER)

        assert mock_redis.hgetall.call_args[0][0] == f"engagement:prepost:27:{PRE_POST_TASK_VIEWER}"

    def test_unknown_post_returns_empty_dict(self, mock_redis):
        from cqc_lem.utilities.engagement_window import get_pre_post_window_stat
        mock_redis.hgetall.return_value = {}

        assert get_pre_post_window_stat(24) == {}

    def test_no_redis_returns_empty_dict(self):
        from cqc_lem.utilities.engagement_window import get_pre_post_window_stat
        with patch(f"{_MOD}.shared_redis_client", return_value=None):
            assert get_pre_post_window_stat(25) == {}

    def test_read_error_returns_empty_dict(self, mock_redis):
        from cqc_lem.utilities.engagement_window import get_pre_post_window_stat
        mock_redis.hgetall.side_effect = RuntimeError("redis down")

        assert get_pre_post_window_stat(26) == {}


# ---------------------------------------------------------------------------
# Daily fan-out staggering (issue #554)
# ---------------------------------------------------------------------------

_FANOUT = ("GOLDEN_HOUR", 9, 180)


def _config(**overrides):
    from cqc_lem.utilities.engagement_window import StaggerConfig
    fields = {"name": "GOLDEN_HOUR", "anchor_hour": 9, "window_minutes": 180, "local": True}
    fields.update(overrides)
    return StaggerConfig(**fields)


class TestStaggerOffsetMinutes:
    def test_offset_is_stable_and_inside_the_window(self):
        from cqc_lem.utilities.engagement_window import stagger_offset_minutes
        for user_id in range(1, 60):
            first = stagger_offset_minutes(user_id, 180, "GOLDEN_HOUR")
            assert 0 <= first < 180
            assert stagger_offset_minutes(user_id, 180, "GOLDEN_HOUR") == first

    def test_consecutive_users_do_not_clump(self):
        """user_id % window would put ids 1..50 on 50 consecutive minutes AND on the same minute in
        every window; the salted hash has to spread them instead."""
        from cqc_lem.utilities.engagement_window import stagger_offset_minutes
        offsets = [stagger_offset_minutes(u, 180, "GOLDEN_HOUR") for u in range(1, 51)]
        assert len(set(offsets)) > 30                          # spread, not a 50-minute run
        assert offsets != [stagger_offset_minutes(u, 180, "APPRECIATION_DM") for u in range(1, 51)]

    def test_window_of_one_puts_everyone_on_the_anchor_minute(self):
        from cqc_lem.utilities.engagement_window import stagger_offset_minutes
        assert {stagger_offset_minutes(u, 1, "GOLDEN_HOUR") for u in range(1, 30)} == {0}


class TestStaggerConfig:
    def test_defaults_when_env_is_unset(self, monkeypatch):
        from cqc_lem.utilities.engagement_window import stagger_config
        for key in ("GOLDEN_HOUR_ANCHOR_HOUR", "GOLDEN_HOUR_WINDOW_MINUTES", "GOLDEN_HOUR_ANCHOR_TZ"):
            monkeypatch.delenv(key, raising=False)
        config = stagger_config(_FANOUT)
        assert (config.anchor_hour, config.window_minutes, config.local) == (9, 180, True)

    def test_env_overrides_are_read_at_call_time(self, monkeypatch):
        from cqc_lem.utilities.engagement_window import stagger_config
        monkeypatch.setenv("GOLDEN_HOUR_ANCHOR_HOUR", "14")
        monkeypatch.setenv("GOLDEN_HOUR_WINDOW_MINUTES", "60")
        monkeypatch.setenv("GOLDEN_HOUR_ANCHOR_TZ", "UTC")
        config = stagger_config(_FANOUT)
        assert (config.anchor_hour, config.window_minutes, config.local) == (14, 60, False)

    @pytest.mark.parametrize("bad", ["nine", "24", "-1", ""])
    def test_invalid_anchor_hour_falls_back_to_the_default(self, monkeypatch, bad):
        from cqc_lem.utilities.engagement_window import stagger_config
        monkeypatch.setenv("GOLDEN_HOUR_ANCHOR_HOUR", bad)
        assert stagger_config(_FANOUT).anchor_hour == 9

    def test_invalid_window_falls_back_to_the_default(self, monkeypatch):
        from cqc_lem.utilities.engagement_window import stagger_config
        monkeypatch.setenv("GOLDEN_HOUR_WINDOW_MINUTES", "0")
        assert stagger_config(_FANOUT).window_minutes == 180


class TestPlanDailySlot:
    def test_slot_sits_between_the_anchor_and_the_window_close(self):
        from cqc_lem.utilities.engagement_window import plan_daily_slot
        for user_id in range(1, 40):
            slot = plan_daily_slot(user_id, _config(), "UTC", now=_NOW)
            assert slot.local_at.hour * 60 + slot.local_at.minute >= 9 * 60
            assert slot.local_at.hour * 60 + slot.local_at.minute < 9 * 60 + 180

    def test_not_reached_before_the_anchor(self):
        from cqc_lem.utilities.engagement_window import plan_daily_slot
        before = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)
        slot = plan_daily_slot(7, _config(), "UTC", now=before)
        assert slot.reached is False and slot.in_tick_window is False

    def test_due_on_the_first_tick_after_the_slot_only(self):
        from cqc_lem.utilities.engagement_window import plan_daily_slot
        config = _config()
        slot = plan_daily_slot(7, config, "UTC", now=_NOW)
        at = slot.local_at.replace(tzinfo=timezone.utc)

        assert plan_daily_slot(7, config, "UTC", now=at).in_tick_window is True
        assert plan_daily_slot(7, config, "UTC", now=at + timedelta(minutes=14)).in_tick_window is True
        assert plan_daily_slot(7, config, "UTC", now=at + timedelta(minutes=15)).in_tick_window is False
        assert plan_daily_slot(7, config, "UTC", now=at - timedelta(minutes=1)).in_tick_window is False

    def test_reached_stays_true_after_the_tick_for_catch_up(self):
        from cqc_lem.utilities.engagement_window import plan_daily_slot
        config = _config()
        at = plan_daily_slot(7, config, "UTC", now=_NOW).local_at.replace(tzinfo=timezone.utc)
        late = plan_daily_slot(7, config, "UTC", now=at + timedelta(hours=5))
        assert late.reached is True and late.in_tick_window is False

    def test_local_anchor_uses_the_users_timezone(self):
        """Anchor 9 with a 1-minute window = 09:00 local, which is 13:00 UTC in New York and
        16:00 UTC in Los Angeles — the whole point of anchoring per user."""
        from cqc_lem.utilities.engagement_window import plan_daily_slot
        config = _config(window_minutes=1)
        east = plan_daily_slot(7, config, "America/New_York", now=_NOW)
        west = plan_daily_slot(7, config, "America/Los_Angeles", now=_NOW)
        assert east.at.hour == 13 and west.at.hour == 16
        assert east.local_at.hour == west.local_at.hour == 9

    def test_utc_anchor_ignores_the_user_timezone(self):
        from cqc_lem.utilities.engagement_window import plan_daily_slot
        slot = plan_daily_slot(7, _config(local=False, window_minutes=1), "America/Los_Angeles",
                               now=_NOW)
        assert slot.at.hour == 9

    def test_unknown_timezone_falls_back_to_utc(self):
        from cqc_lem.utilities.engagement_window import plan_daily_slot
        slot = plan_daily_slot(7, _config(window_minutes=1), "Mars/Olympus_Mons", now=_NOW)
        assert slot.at.hour == 9

    def test_window_past_midnight_wraps_instead_of_never_firing(self):
        from cqc_lem.utilities.engagement_window import plan_daily_slot
        config = _config(anchor_hour=23, window_minutes=180)
        late_night = datetime(2026, 7, 25, 23, 59, tzinfo=timezone.utc)
        slots = [plan_daily_slot(u, config, "UTC", now=late_night) for u in range(1, 40)]
        assert any(s.local_at.hour < 2 for s in slots)         # wrapped into the small hours
        assert all(s.local_at.date() == late_night.date() for s in slots)

    def test_every_user_gets_exactly_one_tick_per_day(self):
        """The property the whole design rests on: with the beat ticking every 15 minutes, each
        user's slot matches exactly one tick — no misses, no doubles."""
        from cqc_lem.utilities.engagement_window import plan_daily_slot
        config = _config()
        start = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)
        ticks = [start + timedelta(minutes=15 * i) for i in range(96)]
        for user_id in range(1, 25):
            due = [t for t in ticks if plan_daily_slot(user_id, config, "UTC", now=t).in_tick_window]
            assert len(due) == 1

    def test_dst_change_keeps_the_same_local_minute(self):
        from cqc_lem.utilities.engagement_window import plan_daily_slot
        config = _config()
        winter = plan_daily_slot(7, config, "America/New_York",
                                 now=datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc))
        summer = plan_daily_slot(7, config, "America/New_York",
                                 now=datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc))
        assert (winter.local_at.hour, winter.local_at.minute) == (summer.local_at.hour,
                                                                  summer.local_at.minute)
        assert winter.at.hour == summer.at.hour + 1            # ...which is a different UTC hour


class TestClaimDailySlot:
    def test_returns_none_without_redis(self):
        from cqc_lem.utilities.engagement_window import claim_daily_slot
        with patch(f"{_MOD}.shared_redis_client", return_value=None):
            assert claim_daily_slot(7, "GOLDEN_HOUR", "2026-07-25") is None

    def test_claims_once_per_day(self):
        from cqc_lem.utilities.engagement_window import claim_daily_slot
        client = MagicMock()
        client.set.side_effect = [True, None]                  # nx=True: second caller loses
        with patch(f"{_MOD}.shared_redis_client", return_value=client):
            assert claim_daily_slot(7, "GOLDEN_HOUR", "2026-07-25") is True
            assert claim_daily_slot(7, "GOLDEN_HOUR", "2026-07-25") is False
        key, value = client.set.call_args[0]
        assert key == "engagement:slot:GOLDEN_HOUR:7:2026-07-25"
        assert client.set.call_args[1]["nx"] is True
        assert client.set.call_args[1]["ex"] > 24 * 60 * 60    # outlasts the day it was claimed on

    def test_a_late_claim_cannot_block_the_next_days_slot(self):
        """A catch-up dispatched late in the day writes a claim that would still be alive at
        tomorrow's slot under a flat TTL — the local date in the key is what prevents that."""
        from cqc_lem.utilities.engagement_window import claim_daily_slot
        client = MagicMock()
        client.set.return_value = True
        with patch(f"{_MOD}.shared_redis_client", return_value=client):
            claim_daily_slot(7, "GOLDEN_HOUR", "2026-07-25")
            claim_daily_slot(7, "GOLDEN_HOUR", "2026-07-26")
        assert client.set.call_args_list[0][0][0] != client.set.call_args_list[1][0][0]

    def test_redis_error_degrades_to_unknown(self):
        from cqc_lem.utilities.engagement_window import claim_daily_slot
        client = MagicMock()
        client.set.side_effect = RuntimeError("redis down")
        with patch(f"{_MOD}.shared_redis_client", return_value=client):
            assert claim_daily_slot(7, "GOLDEN_HOUR", "2026-07-25") is None


class TestApprovedFanoutDefaults:
    """The three shipped windows are a product decision the owner signed off on in PR #607
    (`1A 2A 3A`): golden hour 09:00 + 3h, appreciation DMs 08:00 + 2h, groups 12:00 + 2h — each
    anchored on the USER's clock, not UTC. The rest of the suite exercises a local literal, so
    without this the constants could drift away from the approved values silently. Retuning is an
    env change (`<NAME>_ANCHOR_HOUR` / `_WINDOW_MINUTES` / `_ANCHOR_TZ`); changing these numbers is
    changing the default every deployment inherits, so it needs the same sign-off again.
    """

    @pytest.mark.parametrize("fanout,expected", [
        ("STAGGER_GOLDEN_HOUR", ("GOLDEN_HOUR", 9, 180)),
        ("STAGGER_APPRECIATION_DM", ("APPRECIATION_DM", 8, 120)),
        ("STAGGER_GROUP_ENGAGEMENT", ("GROUP_ENGAGEMENT", 12, 120)),
    ])
    def test_anchor_hour_and_window_match_the_approved_decision(self, fanout, expected):
        import cqc_lem.utilities.engagement_window as mod
        assert getattr(mod, fanout) == expected

    @pytest.mark.parametrize("fanout", ["STAGGER_GOLDEN_HOUR", "STAGGER_APPRECIATION_DM",
                                        "STAGGER_GROUP_ENGAGEMENT"])
    def test_each_fanout_anchors_on_the_users_own_clock(self, monkeypatch, fanout):
        import cqc_lem.utilities.engagement_window as mod
        name = getattr(mod, fanout)[0]
        for suffix in ("_ANCHOR_HOUR", "_WINDOW_MINUTES", "_ANCHOR_TZ"):
            monkeypatch.delenv(f"{name}{suffix}", raising=False)
        assert mod.stagger_config(getattr(mod, fanout)).local is True

    def test_every_window_is_wide_enough_to_stagger(self):
        """A window narrower than the beat cadence would put the whole fleet back on one tick."""
        import cqc_lem.utilities.engagement_window as mod
        for fanout in (mod.STAGGER_GOLDEN_HOUR, mod.STAGGER_APPRECIATION_DM,
                       mod.STAGGER_GROUP_ENGAGEMENT):
            assert fanout[2] >= mod.STAGGER_TICK_MINUTES
