"""Per-user session windows and timezone (issue #2767, Phase 3)."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from cqc_lem.utilities import engagement_window as ew

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.engagement_window"
_GOOD = [{"start": "09:00", "end": "10:00"}, {"start": "14:00", "end": "14:30"}]


def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


class TestParse:
    def test_valid_windows_are_named_in_order(self):
        assert ew.parse_session_windows(_GOOD) == {"A": (540, 600), "C": (840, 870)}

    def test_json_string_and_bytes_decode(self):
        import json
        assert ew.parse_session_windows(json.dumps(_GOOD)) == ew.parse_session_windows(_GOOD)
        assert ew.parse_session_windows(json.dumps(_GOOD).encode()) == ew.parse_session_windows(_GOOD)

    def test_unsorted_input_is_sorted(self):
        assert list(ew.parse_session_windows(list(reversed(_GOOD))).values()) == [(540, 600), (840, 870)]

    @pytest.mark.parametrize("bad", [
        None, [], "not json", {"start": "09:00"}, ["09:00"],
        [{"start": "10:00", "end": "09:00"}],                      # inverted
        [{"start": "09:00", "end": "09:10"}],                      # too short
        [{"start": "06:00", "end": "07:30"}],                      # before 07:00
        [{"start": "18:00", "end": "19:30"}],                      # past 19:00
        [{"start": "25:00", "end": "26:00"}],                      # out of range
        [{"start": "9:00", "end": "10:00"}],                       # not HH:MM
        [{"start": "09:00", "end": "10:00"}, {"start": "09:30", "end": "10:30"}],  # overlap
        [{"start": f"{h:02d}:00", "end": f"{h:02d}:30"} for h in range(8, 13)],     # five
    ])
    def test_invalid_falls_back_to_defaults(self, bad):
        assert ew.parse_session_windows(bad) is None
        assert ew.normalize_session_windows(bad) is None

    def test_fallback_is_quiet(self):
        with patch(f"{_MOD}.log_warning") as warn:
            ew.parse_session_windows([{"start": "10:00", "end": "09:00"}])
            ew.valid_session_timezone("Not/AZone")
        warn.assert_not_called()

    def test_normalize_round_trips(self):
        assert ew.normalize_session_windows(_GOOD) == _GOOD

    def test_timezone_validation(self):
        assert ew.valid_session_timezone(" Europe/London ") == "Europe/London"
        for bad in (None, "", "  ", "Not/AZone", 5):
            assert ew.valid_session_timezone(bad) is None


class TestBlocks:
    def test_no_windows_is_byte_identical_to_defaults(self):
        for approved in (0, 1, 100, 10_000):
            assert ew.session_blocks(approved, None) == ew.session_blocks(approved)
        assert ew.session_blocks(0) == ew.SESSION_WINDOWS

    def test_custom_windows_replace_defaults(self):
        assert ew.session_blocks(0, ew.parse_session_windows(_GOOD)) == {"A": (540, 600), "C": (840, 870)}

    def test_volume_blocks_start_at_the_custom_starts(self):
        blocks = ew.session_blocks(100, ew.parse_session_windows(_GOOD))
        assert [s for s, _ in blocks.values()][:2] == [540, 840]

    def test_decision_and_eta_use_the_custom_windows(self):
        windows = ew.parse_session_windows(_GOOD)
        # Mon 2026-11-23 09:10 New York = 14:10Z: window A for this user, outside the default A.
        at = _utc(2026, 11, 23, 14, 10)
        assert not ew.session_window_decision(1, "x", at, "America/New_York").allowed
        assert ew.session_window_decision(1, "x", at, "America/New_York", windows=windows).window == "A"
        assert ew.next_window_start(_utc(2026, 11, 23, 16, 0), "America/New_York",
                                    windows=windows) == _utc(2026, 11, 23, 19, 0)  # 14:00 EST

    def test_owner_bans_hold_for_custom_windows(self):
        windows = ew.parse_session_windows(_GOOD)
        assert ew.session_window_decision(1, "x", _utc(2026, 11, 21, 14, 10), "America/New_York",
                                          windows=windows).reason == "weekend"
        assert ew.session_window_decision(1, "x", _utc(2026, 11, 23, 14, 10), "America/New_York",
                                          user_started=True, windows=windows).allowed


class TestGate:
    def _env(self, monkeypatch, mode):
        monkeypatch.setenv("SESSION_WINDOW_MODE", mode)

    def test_off_and_shadow_never_read_the_prefs(self, monkeypatch):
        for mode in ("off", "shadow"):
            self._env(monkeypatch, mode)
            with patch("cqc_lem.utilities.db.get_user_timezone", return_value="America/New_York"), \
                 patch("cqc_lem.utilities.db.get_session_window_prefs") as prefs, \
                 patch(f"{_MOD}.count_session", return_value=1), \
                 patch(f"{_MOD}.track_session_window_shadow"):
                ew.observe_session_window(1, "x", now=_utc(2026, 11, 23, 14, 10))
            prefs.assert_not_called()

    def test_shadow_decision_ignores_a_stored_window(self, monkeypatch):
        self._env(monkeypatch, "shadow")
        # 09:10 New York is outside the DEFAULT windows: shadow still says it would defer.
        with patch("cqc_lem.utilities.db.get_user_timezone", return_value="America/New_York"), \
             patch("cqc_lem.utilities.db.get_session_window_prefs", return_value=(_GOOD, "Asia/Tokyo")), \
             patch(f"{_MOD}.count_session", return_value=1), \
             patch(f"{_MOD}.track_session_window_shadow"):
            d = ew.observe_session_window(1, "x", now=_utc(2026, 11, 23, 14, 10))
        assert not d.allowed and d.reason == "outside_window"

    def _enforce(self, monkeypatch, stored, now, tz="America/New_York"):
        self._env(monkeypatch, "enforce")
        with patch("cqc_lem.utilities.db.get_user_timezone", return_value=tz), \
             patch("cqc_lem.utilities.db.get_session_window_prefs", return_value=stored), \
             patch("cqc_lem.utilities.db.get_approved_comment_candidates", return_value=[]), \
             patch("cqc_lem.utilities.db.get_scheduled_post_times", return_value=[]), \
             patch(f"{_MOD}.peek_session_count", return_value=0), \
             patch(f"{_MOD}.count_session"), patch(f"{_MOD}.track_session_window_shadow"):
            try:
                return ew.observe_session_window(1, "x", now=now)
            except ew.SessionDeferred as exc:
                return exc

    def test_enforce_uses_the_stored_windows(self, monkeypatch):
        assert self._enforce(monkeypatch, (_GOOD, None), _utc(2026, 11, 23, 14, 10)).allowed

    def test_enforce_defers_to_the_stored_next_window(self, monkeypatch):
        exc = self._enforce(monkeypatch, (_GOOD, None), _utc(2026, 11, 23, 16, 0))
        assert isinstance(exc, ew.SessionDeferred) and exc.eta == _utc(2026, 11, 23, 19, 0)

    def test_enforce_timezone_override_wins_over_the_account_zone(self, monkeypatch):
        # Mon 09:10 in Tokyo is Mon 00:10Z; the account zone (New York) would call that night.
        assert self._enforce(monkeypatch, (_GOOD, "Asia/Tokyo"), _utc(2026, 11, 23, 0, 10)).allowed

    def test_enforce_bad_stored_values_fall_back_to_the_defaults(self, monkeypatch):
        bad = [{"start": "10:00", "end": "09:00"}]
        # Default window A, account zone: 08:10 New York is allowed.
        assert self._enforce(monkeypatch, (bad, "Not/AZone"), _utc(2026, 11, 23, 13, 10)).allowed
        exc = self._enforce(monkeypatch, (bad, "Not/AZone"), _utc(2026, 11, 23, 14, 10))
        assert isinstance(exc, ew.SessionDeferred) and exc.reason == "outside_window"

    def test_enforce_null_prefs_match_the_defaults(self, monkeypatch):
        assert self._enforce(monkeypatch, (None, None), _utc(2026, 11, 23, 13, 10)).allowed
