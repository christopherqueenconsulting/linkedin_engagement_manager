"""Comment-volume ramp (#2647 Phase 5): transitions, gates, clamp and fail-closed reads."""

from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import comment_ramp as ramp

NOW = 1_000_000_000.0
WEEK = ramp.DWELL_SECONDS
CLEAR = {"reply_rate": 0.30, "reply_sample": 20, "demotion": "ok",
         "rate_limited": False, "tripped": False}


def _state(step=20, aged=True, baseline=0.25):
    return {"step": step, "since": NOW - WEEK - 1 if aged else NOW - 60, "baseline": baseline}


class TestDecideMove:
    @pytest.mark.parametrize("step,nxt", [(20, 40), (40, 70), (70, 100)])
    def test_each_step_up(self, step, nxt):
        move, _ = ramp.decide_move(_state(step), CLEAR, NOW)
        assert move == ramp.MOVE_UP
        assert ramp.STEPS[ramp.STEPS.index(step) + 1] == nxt

    def test_top_step_holds(self):
        assert ramp.decide_move(_state(100), CLEAR, NOW)[0] == ramp.MOVE_HOLD

    def test_dwell_required(self):
        assert ramp.decide_move(_state(20, aged=False), CLEAR, NOW)[0] == ramp.MOVE_HOLD

    def test_missing_since_holds(self):
        state = {"step": 20, "since": None, "baseline": None}
        assert ramp.decide_move(state, CLEAR, NOW)[0] == ramp.MOVE_HOLD

    def test_reply_rate_fall_blocks_up(self):
        signals = {**CLEAR, "reply_rate": 0.10}
        assert ramp.decide_move(_state(baseline=0.25), signals, NOW)[0] == ramp.MOVE_HOLD

    def test_no_baseline_allows_up(self):
        assert ramp.decide_move(_state(baseline=None), CLEAR, NOW)[0] == ramp.MOVE_UP

    @pytest.mark.parametrize("key", ["reply_rate", "demotion", "rate_limited", "tripped"])
    def test_unknown_gate_never_steps_up(self, key):
        move, reason = ramp.decide_move(_state(), {**CLEAR, key: None}, NOW)
        assert move == ramp.MOVE_UNKNOWN and "unreadable" in reason

    @pytest.mark.parametrize("verdict", ["watch", "unknown"])
    def test_only_an_ok_demotion_verdict_steps_up(self, verdict):
        assert ramp.decide_move(_state(), {**CLEAR, "demotion": verdict}, NOW)[0] in (
            ramp.MOVE_HOLD, ramp.MOVE_UNKNOWN)

    @pytest.mark.parametrize("breach", [{"demotion": "hold"}, {"rate_limited": True}, {"tripped": True}])
    def test_breach_steps_down_even_when_young(self, breach):
        move, _ = ramp.decide_move(_state(70, aged=False), {**CLEAR, **breach}, NOW)
        assert move == ramp.MOVE_DOWN

    def test_breach_at_first_step_holds(self):
        assert ramp.decide_move(_state(20), {**CLEAR, "tripped": True}, NOW)[0] == ramp.MOVE_HOLD


class TestClamp:
    @pytest.mark.parametrize("raw,want", [(5, 5), (100, 100), (250, 100), (-3, 0), ("40", 40),
                                          ("x", 20), (None, 20)])
    def test_clamp(self, raw, want):
        assert ramp.clamp_comments_per_day(raw) == want


class TestState:
    def test_no_redis_is_unreadable(self):
        with patch.object(ramp, "_client", return_value=None):
            assert ramp.read_state(1) is None
            assert ramp.current_step(1) == 20

    def test_redis_error_is_unreadable(self):
        client = MagicMock()
        client.hgetall.side_effect = RuntimeError("down")
        with patch.object(ramp, "_client", return_value=client):
            assert ramp.read_state(1) is None

    def test_empty_state_is_first_step(self):
        client = MagicMock()
        client.hgetall.return_value = {}
        with patch.object(ramp, "_client", return_value=client):
            assert ramp.read_state(1) == {"step": 20, "since": None, "baseline": None}

    def test_stored_state_decodes(self):
        client = MagicMock()
        client.hgetall.return_value = {b"step": b"70", b"since": b"5.0", b"baseline": b"0.3"}
        with patch.object(ramp, "_client", return_value=client):
            assert ramp.read_state(1) == {"step": 70, "since": 5.0, "baseline": 0.3}

    def test_unknown_step_value_falls_to_first(self):
        client = MagicMock()
        client.hgetall.return_value = {b"step": b"55", b"since": b"5.0"}
        with patch.object(ramp, "_client", return_value=client):
            assert ramp.read_state(1)["step"] == 20

    def test_write_failure_reports_false(self):
        client = MagicMock()
        client.hset.side_effect = RuntimeError("down")
        with patch.object(ramp, "_client", return_value=client):
            assert ramp._write_state(1, 40, None, NOW) is False
        with patch.object(ramp, "_client", return_value=None):
            assert ramp._write_state(1, 40, None, NOW) is False


class TestEvaluate:
    def test_flag_off_is_a_noop(self):
        with patch.object(ramp, "ramp_enabled", return_value=False), \
                patch.object(ramp, "read_state") as read:
            assert ramp.evaluate_ramp(1, NOW)["move"] == ramp.MOVE_DISABLED
            read.assert_not_called()

    def test_unreadable_state_holds_and_writes_nothing(self):
        with patch.object(ramp, "ramp_enabled", return_value=True), \
                patch.object(ramp, "read_state", return_value=None), \
                patch.object(ramp, "_write_state") as write:
            out = ramp.evaluate_ramp(1, NOW)
        assert out["move"] == ramp.MOVE_UNKNOWN and out["step"] == 20
        write.assert_not_called()

    def test_step_up_persists_with_new_baseline(self):
        with patch.object(ramp, "ramp_enabled", return_value=True), \
                patch.object(ramp, "read_state", return_value=_state(20)), \
                patch.object(ramp, "read_signals", return_value=CLEAR), \
                patch.object(ramp, "_write_state", return_value=True) as write:
            out = ramp.evaluate_ramp(1, NOW)
        assert out["move"] == ramp.MOVE_UP and out["step"] == 40
        write.assert_called_once_with(1, 40, 0.30, NOW)

    def test_breach_steps_down_in_one_run(self):
        with patch.object(ramp, "ramp_enabled", return_value=True), \
                patch.object(ramp, "read_state", return_value=_state(70, aged=False)), \
                patch.object(ramp, "read_signals", return_value={**CLEAR, "rate_limited": True}), \
                patch.object(ramp, "_write_state", return_value=True):
            out = ramp.evaluate_ramp(1, NOW)
        assert out["move"] == ramp.MOVE_DOWN and out["step"] == 40

    def test_failed_write_keeps_the_step(self):
        with patch.object(ramp, "ramp_enabled", return_value=True), \
                patch.object(ramp, "read_state", return_value=_state(20)), \
                patch.object(ramp, "read_signals", return_value=CLEAR), \
                patch.object(ramp, "_write_state", return_value=False):
            out = ramp.evaluate_ramp(1, NOW)
        assert out["move"] == ramp.MOVE_UNKNOWN and out["step"] == 20

    def test_fresh_state_is_initialised(self):
        fresh = {"step": 20, "since": None, "baseline": None}
        with patch.object(ramp, "ramp_enabled", return_value=True), \
                patch.object(ramp, "read_state", return_value=fresh), \
                patch.object(ramp, "read_signals", return_value=CLEAR), \
                patch.object(ramp, "_write_state", return_value=True) as write:
            out = ramp.evaluate_ramp(1, NOW)
        assert out["move"] == ramp.MOVE_HOLD
        write.assert_called_once_with(1, 20, 0.30, NOW)

    def test_ceiling_is_first_step_when_flag_off(self):
        with patch.object(ramp, "ramp_enabled", return_value=False):
            assert ramp.ramp_comment_ceiling(1) == 20

    def test_ceiling_follows_stored_step(self):
        with patch.object(ramp, "ramp_enabled", return_value=True), \
                patch.object(ramp, "read_state", return_value=_state(70)):
            assert ramp.ramp_comment_ceiling(1) == 70


class TestReadSignals:
    def _patch(self, rows, since, redis=object(), tripped=False):
        return (patch("cqc_lem.utilities.db.get_comment_outcomes", return_value=rows),
                patch("cqc_lem.utilities.linkedin.rate_limit.shared_redis_client", return_value=redis),
                patch("cqc_lem.utilities.linkedin.rate_limit.seconds_since_last_rate_limit",
                      return_value=since),
                patch("cqc_lem.utilities.linkedin.rate_limit.is_suppression_tripped",
                      return_value=tripped))

    def _rows(self, n=12, replied=4):
        return [{"status": "checked", "reply_count": 1 if i < replied else 0,
                 "author_replied": i < replied, "like_count": 0, "visible_most_relevant": True}
                for i in range(n)]

    def test_all_known_and_clear(self):
        a, b, c, d = self._patch(self._rows(), None)
        with a, b, c, d:
            sig = ramp.read_signals(1, NOW)
        assert sig["reply_rate"] == pytest.approx(4 / 12, abs=1e-3)
        assert sig["demotion"] == "ok" and sig["rate_limited"] is False and sig["tripped"] is False

    def test_recent_429_and_trip(self):
        a, b, c, d = self._patch(self._rows(), 3600, tripped=True)
        with a, b, c, d:
            sig = ramp.read_signals(1, NOW)
        assert sig["rate_limited"] is True and sig["tripped"] is True

    def test_old_429_is_clear(self):
        a, b, c, d = self._patch(self._rows(), ramp.RATE_LIMIT_LOOKBACK_SECONDS + 5)
        with a, b, c, d:
            assert ramp.read_signals(1, NOW)["rate_limited"] is False

    def test_no_redis_leaves_redis_gates_unknown(self):
        a, b, c, d = self._patch(self._rows(), None, redis=None)
        with a, b, c, d:
            sig = ramp.read_signals(1, NOW)
        assert sig["rate_limited"] is None and sig["tripped"] is None

    def test_thin_sample_leaves_reply_rate_unknown(self):
        a, b, c, d = self._patch(self._rows(n=3, replied=1), None)
        with a, b, c, d:
            assert ramp.read_signals(1, NOW)["reply_rate"] is None

    def test_no_rows_is_unknown(self):
        a, b, c, d = self._patch([], None)
        with a, b, c, d:
            sig = ramp.read_signals(1, NOW)
        assert sig["reply_rate"] is None and sig["demotion"] is None

    def test_outcome_read_error_is_unknown(self):
        a, b, c, d = self._patch(None, None)
        with patch("cqc_lem.utilities.db.get_comment_outcomes", side_effect=RuntimeError("x")), b, c, d:
            assert ramp.read_signals(1, NOW)["reply_rate"] is None


class TestBrandCeiling:
    def test_raw_pref_never_lifts_ceiling(self):
        from cqc_lem.utilities import brand_account as ba
        with patch.object(ramp, "ramp_enabled", return_value=False):
            assert ba.brand_cap_ceiling("max_comments_per_day") == 20
            assert ba.brand_preference_overrides({"max_comments_per_day": 60}, "P0",
                                                 configured=True)["max_comments_per_day"] == 20

    def test_ramp_step_lifts_only_comments(self):
        from cqc_lem.utilities import brand_account as ba
        with patch.object(ramp, "ramp_comment_ceiling", return_value=70):
            assert ba.brand_cap_ceiling("max_comments_per_day") == 70
            assert ba.brand_cap_ceiling("max_dms_per_day") == 20
            out = ba.brand_preference_overrides({"max_comments_per_day": 90}, "P0", configured=True)
        assert out["max_comments_per_day"] == 70

    def test_ramp_fault_keeps_base(self):
        from cqc_lem.utilities import brand_account as ba
        with patch.object(ramp, "ramp_comment_ceiling", side_effect=RuntimeError("x")):
            assert ba.brand_cap_ceiling("max_comments_per_day") == 20


class TestRateLimitStamp:
    def test_unreadable_and_absent_are_none(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        with patch.object(rl, "_redis_client", return_value=None):
            assert rl.seconds_since_last_rate_limit() is None
        client = MagicMock()
        client.get.return_value = None
        with patch.object(rl, "_redis_client", return_value=client):
            assert rl.seconds_since_last_rate_limit() is None
        client.get.side_effect = RuntimeError("x")
        with patch.object(rl, "_redis_client", return_value=client):
            assert rl.seconds_since_last_rate_limit() is None

    def test_reads_age(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        client = MagicMock()
        client.get.return_value = b"100"
        with patch.object(rl, "_redis_client", return_value=client), \
                patch.object(rl.time, "time", return_value=160.0):
            assert rl.seconds_since_last_rate_limit() == 60

    def test_trip_stamps_the_time(self):
        from cqc_lem.utilities.linkedin import rate_limit as rl
        client = MagicMock()
        client.incr.return_value = 1
        with patch.object(rl, "_redis_client", return_value=client), \
                patch("cqc_lem.utilities.observability.track_rate_limit_trip"):
            rl.mark_rate_limited("t")
        keys = [c.args[0] for c in client.setex.call_args_list]
        assert rl._LAST_TRIP_KEY in keys
