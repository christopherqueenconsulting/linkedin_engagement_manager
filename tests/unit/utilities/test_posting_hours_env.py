"""Configurability-audit tests for DEFAULT_POSTING_HOURS: the weekday→hour default posting model
is env-overridable per deploy, and any invalid value falls back to the built-in 2026 peak model.
"""

from datetime import date, time

import pytest

pytestmark = pytest.mark.unit

# 2026-07-06 is a Monday .. 2026-07-12 a Sunday.
_MONDAY = date(2026, 7, 6)
_SUNDAY = date(2026, 7, 12)


class TestDefaultPostingHoursEnv:
    def test_builtin_model_when_env_unset(self, monkeypatch):
        from cqc_lem.utilities.utils import get_best_posting_time
        monkeypatch.delenv("DEFAULT_POSTING_HOURS", raising=False)
        assert get_best_posting_time(_MONDAY) == time(16, 0)
        assert get_best_posting_time(_SUNDAY) == time(18, 0)

    def test_env_override_wins(self, monkeypatch):
        from cqc_lem.utilities.utils import get_best_posting_time
        monkeypatch.setenv("DEFAULT_POSTING_HOURS", "9,10,11,12,13,14,15")
        assert get_best_posting_time(_MONDAY) == time(9, 0)
        assert get_best_posting_time(_SUNDAY) == time(15, 0)

    @pytest.mark.parametrize("bad", [
        "9,10,11",              # wrong count
        "9,10,11,12,13,14,25",  # hour out of range
        "a,b,c,d,e,f,g",        # not ints
    ])
    def test_invalid_override_falls_back(self, monkeypatch, bad):
        from cqc_lem.utilities.utils import get_best_posting_time
        monkeypatch.setenv("DEFAULT_POSTING_HOURS", bad)
        assert get_best_posting_time(_MONDAY) == time(16, 0)
